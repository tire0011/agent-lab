"""Local shadow replay (#637): rerun merged paid experiments on the local coder.

Every merged experiment that a paid coder (codex, claude) implemented is a
free benchmark case: its task text is stored, its base commit is known, and
the merged commit carries reviewed tests. :func:`replay` reruns such a task
with :class:`~modules.local_coding_worker.LocalCodingWorker` and scores the
local result against the merged tests:

1. A throwaway detached git worktree is made at the experiment's base commit.
2. The coder's task text is rebuilt from the stored goal
   (:func:`~modules.experiment_service.prompts._build_direct_implementation_prompt`).
3. Only the local worker runs: no paid fallback (the worker is called
   directly, never through the experiment pipeline) and no free tier (its
   subtask planner fallback is replaced by one that never calls a model).
4. After the worker, and only then, the test files of the merged diff are
   brought in: the paths under ``tests/`` that the merged commit added,
   changed, renamed or deleted. Source files of the merged diff never are.
5. The merged test files are run on the local result; counts go to the
   ``local_replays`` table, one row per (experiment, prompt variant).
6. The worktree is always removed, also when the worker raises.

The replay never touches main, a branch or GitHub: the worktree is detached,
nothing is committed, and the merged commit is only read from the local
object store (no fetch).

The prompt variant label comes from ``NEXUS_SHADOW_REPLAY_VARIANT`` (default
``baseline``). Change the prompt/context (code or env such as
``LOCAL_TASK_SPLIT``), set a new label, and the sweep replays the same
experiments again under that label, so variants can be compared on the same
corpus. A replay of an experiment already stored for the variant is skipped.

:func:`replay_next` is the nexus_tick sweep: at most one replay per call, only
when the local llama-server is reachable and idle (fail closed otherwise).
All entry points share one process-identified SQLite lease.  It covers the
idle check, candidate claim, worktree lifecycle and worker/test execution.
After a crash, takeover requires both an expired lease and proof that its exact
owner process is dead; only that lease's recorded worktree may be cleaned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.llama_cpp_client import LlamaCppClient
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

ENV_VARIANT = "NEXUS_SHADOW_REPLAY_VARIANT"
ENV_UNIT = "NEXUS_SHADOW_REPLAY_UNIT"
DEFAULT_VARIANT = "baseline"
ENV_ENABLED = "NEXUS_SHADOW_REPLAY"
SETTING_ENABLED = "nexus.shadow_replay_enabled"
ENV_WORKER_TIMEOUT = "NEXUS_SHADOW_REPLAY_TIMEOUT_SECONDS"
ENV_TEST_TIMEOUT = "NEXUS_SHADOW_REPLAY_TEST_TIMEOUT_SECONDS"
ENV_LEASE = "NEXUS_SHADOW_REPLAY_LEASE_SECONDS"
ENV_WORKTREE_DIR = "NEXUS_SHADOW_REPLAY_DIR"
DEFAULT_WORKER_TIMEOUT_SECONDS = 1800.0
DEFAULT_TEST_TIMEOUT_SECONDS = 600.0
_COLLECT_TIMEOUT_SECONDS = 120.0
_GIT_TIMEOUT_SECONDS = 60.0
_BUSY_TIMEOUT_MS = 10_000
_MAX_ERROR_CHARS = 2000
_MAX_VARIANT_CHARS = 64

# Coders that cost money; only their merged work is replayed.
PAID_PROVIDERS = ("codex", "claude")
MERGED_ADOPTION_STATUS = "adopted"
WORKTREE_PREFIX = "nexus-shadow-replay-"
TESTS_DIR = "tests/"

STATUS_PENDING = "pending"
STATUS_SCORED = "scored"
STATUS_NO_TESTS = "no_tests"
STATUS_ERROR = "error"
# #667: the worker refused the call, the measured rates cannot afford a patch.
STATUS_LOCAL_TOO_SLOW = "local_too_slow"

# Experiment states in which a local coding worker may be talking to the
# server directly (it does not go through the local_llm_jobs queue).
_ACTIVE_EXPERIMENT_STATUSES = ("selecting", "implementing", "verifying")
_TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(^.*_test\.py$)")


class ReplayError(RuntimeError):
    """The replay could not be set up (missing commit, bad experiment, ...)."""


@dataclass(frozen=True)
class GlobalLease:
    token: str
    orphaned_worktree: str | None = None


# ---- configuration -----------------------------------------------------------

def prompt_variant() -> str:
    """Variant label including the local model's edit-format treatment."""
    value = " ".join(os.environ.get(ENV_VARIANT, "").split())[:_MAX_VARIANT_CHARS]
    value = value or DEFAULT_VARIANT
    from modules.local_coding_worker import local_edit_format
    # The historical unsuffixed treatment is unified diff; name the new arm so
    # reports naturally compare ``baseline+sr`` against ``baseline``.
    if local_edit_format() == "diff" or value.endswith("+sr"):
        return value
    return value[:_MAX_VARIANT_CHARS - len("+sr")] + "+sr"


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def worker_timeout_seconds() -> float:
    return _float_env(ENV_WORKER_TIMEOUT, DEFAULT_WORKER_TIMEOUT_SECONDS)


def tests_timeout_seconds() -> float:
    return _float_env(ENV_TEST_TIMEOUT, DEFAULT_TEST_TIMEOUT_SECONDS)


def lease_seconds() -> float:
    """Minimum stale age for claims/leases; a live global owner is never preempted."""
    default = worker_timeout_seconds() + tests_timeout_seconds() + 900.0
    return _float_env(ENV_LEASE, default)


def _setting(db_path: str, key: str) -> str | None:
    try:
        conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]).strip() if row and row[0] is not None else None


def enabled(db_path: str = DEFAULT_DB_PATH) -> bool:
    """On unless ``NEXUS_SHADOW_REPLAY=0``, the mailbox setting
    ``shadow_replay_enabled`` = 0, or Nexus is paused."""
    if os.environ.get(ENV_ENABLED, "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if _setting(db_path, SETTING_ENABLED) == "0":
        return False
    return _setting(db_path, "nexus.paused") != "1"


# ---- time and claims ---------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = init_db(db_path)
    conn.isolation_level = None  # autocommit; claims use explicit IMMEDIATE transactions
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def _claim(conn: sqlite3.Connection, experiment_id: str, variant: str, token: str,
           lease: float) -> str:
    """``claimed``, ``done`` (already stored for the variant) or ``busy`` (live claim).

    BEGIN IMMEDIATE takes the write lock before reading, so two callers can
    never both claim the same (experiment, variant). A claim whose lease ran
    out belonged to a caller that stopped and is taken over.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, lease_expires_at FROM local_replays "
            "WHERE experiment_id=? AND prompt_variant=?",
            (experiment_id, variant),
        ).fetchone()
        now = _utcnow()
        deadline = _iso(now + timedelta(seconds=lease))
        if row is not None and row["status"] != STATUS_PENDING:
            conn.execute("COMMIT")
            return "done"
        if row is not None and row["lease_expires_at"] and row["lease_expires_at"] > _iso(now):
            conn.execute("COMMIT")
            return "busy"
        if row is None:
            conn.execute(
                "INSERT INTO local_replays (experiment_id, prompt_variant, status, created_at, "
                "claim_token, lease_expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (experiment_id, variant, STATUS_PENDING, _iso(now), token, deadline),
            )
        else:
            logger.warning("local replay: taking over stale claim %s/%s", experiment_id, variant)
            conn.execute(
                "UPDATE local_replays SET created_at=?, claim_token=?, lease_expires_at=? "
                "WHERE experiment_id=? AND prompt_variant=? AND status=?",
                (_iso(now), token, deadline, experiment_id, variant, STATUS_PENDING),
            )
        conn.execute("COMMIT")
        return "claimed"
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@dataclass
class ReplayOutcome:
    status: str = STATUS_ERROR
    issue: int | None = None
    model: str = ""
    patch_applied: bool = False
    compiled: bool = False
    tests_total: int = 0
    tests_passed: int = 0
    rounds: int = 0
    tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    base_commit: str | None = None
    merged_commit: str | None = None
    test_files: list[str] = field(default_factory=list)
    # #639: the worker's ``local_run_metrics`` (sampling and cost).
    metrics: dict[str, Any] | None = None


def _metric(metrics: dict[str, Any] | None, name: str) -> Any:
    value = (metrics or {}).get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _finalize(conn: sqlite3.Connection, experiment_id: str, variant: str, token: str,
              outcome: ReplayOutcome) -> bool:
    error = outcome.error[:_MAX_ERROR_CHARS] if outcome.error else None
    return conn.execute(
        """UPDATE local_replays
           SET status=?, issue=?, model=?, patch_applied=?, compiled=?, tests_total=?,
               tests_passed=?, rounds=?, tokens=?, seconds=?, error=?, base_commit=?,
               merged_commit=?, test_files_json=?, finished_at=?,
               samples=?, samples_requested=?, samples_passed=?, cpu_seconds=?,
               load_average=?, peak_rss_kb=?, tokens_per_second=?, metrics_json=?,
               claim_token=NULL, lease_expires_at=NULL
           WHERE experiment_id=? AND prompt_variant=? AND claim_token=? AND status=?""",
        (
            outcome.status, outcome.issue, outcome.model, int(outcome.patch_applied),
            int(outcome.compiled), int(outcome.tests_total), int(outcome.tests_passed),
            int(outcome.rounds), int(outcome.tokens), round(float(outcome.seconds), 3), error,
            outcome.base_commit, outcome.merged_commit, json.dumps(outcome.test_files),
            _iso(_utcnow()),
            int(_metric(outcome.metrics, "samples_setting") or 1),
            _metric(outcome.metrics, "samples_requested"), _metric(outcome.metrics, "samples_passed"),
            _metric(outcome.metrics, "llama_server_cpu_seconds"), _metric(outcome.metrics, "load_average"),
            _metric(outcome.metrics, "llama_server_peak_rss_kb"),
            _metric(outcome.metrics, "tokens_per_second"),
            json.dumps(outcome.metrics, default=str) if outcome.metrics is not None else None,
            experiment_id, variant, token, STATUS_PENDING,
        ),
    ).rowcount == 1


def _release(conn: sqlite3.Connection, experiment_id: str, variant: str, token: str) -> None:
    conn.execute(
        "DELETE FROM local_replays WHERE experiment_id=? AND prompt_variant=? "
        "AND claim_token=? AND status=?",
        (experiment_id, variant, token, STATUS_PENDING),
    )


def _process_started(pid: int) -> str | None:
    """Linux process start ticks, used with PID to avoid PID-reuse mistakes."""
    try:
        # Field 22 follows a parenthesized comm which may contain spaces.
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _owner_is_dead(host: str, pid: int, started: str) -> bool:
    """True only when this host can prove that the exact owner no longer exists."""
    if host != socket.gethostname():
        return False
    actual = _process_started(pid)
    return actual is None or actual != started


def _acquire_global_lease(conn: sqlite3.Connection, duration: float) -> GlobalLease | None:
    """Acquire the one replay lease, reclaiming only an expired, provably dead owner."""
    token = uuid.uuid4().hex
    host, pid = socket.gethostname(), os.getpid()
    started = _process_started(pid)
    if started is None:
        raise ReplayError("cannot identify the local replay owner process")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM local_replay_lease WHERE singleton=1").fetchone()
        now = _utcnow()
        orphan = None
        if row is not None:
            expired = str(row["lease_expires_at"]) <= _iso(now)
            dead = _owner_is_dead(str(row["owner_host"]), int(row["owner_pid"]),
                                  str(row["owner_started"]))
            if not (expired and dead):
                conn.execute("COMMIT")
                return None
            orphan = str(row["worktree_path"]) if row["worktree_path"] else None
        conn.execute(
            "INSERT OR REPLACE INTO local_replay_lease "
            "(singleton, token, owner_host, owner_pid, owner_started, acquired_at, "
            "lease_expires_at, worktree_path) VALUES (1, ?, ?, ?, ?, ?, ?, NULL)",
            (token, host, pid, started, _iso(now),
             _iso(now + timedelta(seconds=duration))),
        )
        conn.execute("COMMIT")
        return GlobalLease(token, orphan)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _release_global_lease(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM local_replay_lease WHERE singleton=1 AND token=?", (token,))


def _set_lease_worktree(db_path: str, token: str, path: str | None) -> bool:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "UPDATE local_replay_lease SET worktree_path=? WHERE singleton=1 AND token=?",
            (path, token),
        ).rowcount == 1
    finally:
        conn.close()


# ---- git ---------------------------------------------------------------------

def _git(repo: str, args: list[str], *, check: bool = True, text: bool = True,
         timeout: float = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=text, timeout=timeout)
    if check and done.returncode != 0:
        stderr = done.stderr if text else done.stderr.decode(errors="replace")
        raise ReplayError(f"git {' '.join(args[:3])} failed: {stderr.strip()[:400]}")
    return done


def _commit_exists(repo: str, sha: str | None, suffix: str = "") -> bool:
    """*sha* (a hex object name, optionally with a ``^1``-style *suffix*) is a local commit."""
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha.strip()):
        return False
    return _git(repo, ["rev-parse", "--verify", "--quiet", f"{sha.strip()}{suffix}^{{commit}}"],
                check=False).returncode == 0


def merged_range(repo: str, integrated_sha: str | None, experiment_sha: str | None,
                 base_sha: str) -> tuple[str, str]:
    """``(old, new)`` commits whose diff is the merged change.

    The commit on main (the squash/merge commit) against its first parent;
    without it locally, the experiment's own commit against its base.
    """
    if _commit_exists(repo, integrated_sha) and _commit_exists(repo, integrated_sha, "^1"):
        merged = str(integrated_sha).strip()
        parent = _git(repo, ["rev-parse", f"{merged}^1"]).stdout.strip()
        return parent, merged
    if _commit_exists(repo, experiment_sha):
        return base_sha, str(experiment_sha).strip()
    raise ReplayError("the merged commit is not available in the local repository")


@dataclass(frozen=True)
class MergedTestChange:
    """One merged change under ``tests/``: write *path* from the merged commit, or delete it."""

    __test__ = False  # not a pytest test class

    path: str
    delete: bool


def _source_at(repo: str, commit: str, path: str) -> str:
    shown = _git(repo, ["show", f"{commit}:{path}"], check=False)
    return shown.stdout if shown.returncode == 0 else ""


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return path.startswith(TESTS_DIR) or bool(_TEST_FILE_RE.match(name))


def _safe_test_path(path: str) -> bool:
    parts = path.split("/")
    return (path.startswith(TESTS_DIR) and not path.startswith("/") and ".." not in parts
            and "" not in parts and "\\" not in path)


def merged_test_changes(repo: str, old: str, new: str) -> list[MergedTestChange]:
    """The test-file side of the merged diff; never a path outside ``tests/``.

    A renamed test deletes its old path and writes its new one; a file renamed
    out of ``tests/`` is only deleted there, one renamed into it only written.
    """
    out = _git(repo, ["diff", "--name-status", "-z", "-M", "--no-ext-diff", old, new]).stdout
    tokens = out.split("\0")
    changes: dict[str, MergedTestChange] = {}
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        kind = status[0]
        if kind in "RC":
            old_path, new_path = tokens[i + 1], tokens[i + 2]
            i += 3
            if kind == "R" and _safe_test_path(old_path):
                changes.setdefault(old_path, MergedTestChange(old_path, delete=True))
            if _safe_test_path(new_path):
                changes[new_path] = MergedTestChange(new_path, delete=False)
            continue
        path = tokens[i + 1] if i + 1 < len(tokens) else ""
        i += 2
        if not _safe_test_path(path):
            continue
        changes[path] = MergedTestChange(path, delete=(kind == "D"))
    return sorted(changes.values(), key=lambda c: c.path)


def _inside(root: Path, rel: str) -> Path | None:
    full = root / rel
    try:
        resolved = full.parent.resolve() / full.name
    except OSError:
        return None
    return full if root in resolved.parents else None


def import_merged_tests(repo: str, worktree: str, new: str,
                        changes: list[MergedTestChange]) -> list[str]:
    """Write/delete the merged test paths in *worktree*; the test files to run."""
    root = Path(worktree).resolve()
    to_run: list[str] = []
    for change in changes:
        target = _inside(root, change.path)
        if target is None or not _safe_test_path(change.path):
            raise ReplayError(f"refusing test path outside the worktree: {change.path}")
        if change.delete:
            if target.is_symlink() or target.is_file():
                target.unlink()
            continue
        content = _git(repo, ["show", f"{new}:{change.path}"], text=False).stdout
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_dir():
            raise ReplayError(f"merged test path is not a file in the worktree: {change.path}")
        target.write_bytes(content)
        if _TEST_FILE_RE.match(target.name):
            to_run.append(change.path)
    return to_run


def _create_worktree(repo: str, base: str, *, db_path: str | None = None,
                     lease_token: str | None = None) -> str:
    parent = os.environ.get(ENV_WORKTREE_DIR) or tempfile.gettempdir()
    Path(parent).mkdir(parents=True, exist_ok=True)
    if lease_token:
        path = str(Path(parent) / f"{WORKTREE_PREFIX}{lease_token}")
        if db_path is None or not _set_lease_worktree(db_path, lease_token, path):
            raise ReplayError("lost the global replay lease before creating the worktree")
        Path(path).mkdir()
    else:
        path = tempfile.mkdtemp(prefix=WORKTREE_PREFIX, dir=parent)
    try:
        _git(repo, ["worktree", "add", "--detach", path, base], timeout=300)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def remove_worktree(repo: str, path: str) -> None:
    """Remove the throwaway worktree and its registration; never raises."""
    try:
        _git(repo, ["worktree", "remove", "--force", "--force", path], check=False, timeout=120)
    except Exception:  # noqa: BLE001 - fall through to the filesystem cleanup
        logger.warning("local replay: git worktree remove failed for %s", path, exc_info=True)
    shutil.rmtree(path, ignore_errors=True)
    try:
        _git(repo, ["worktree", "prune"], check=False)
    except Exception:  # noqa: BLE001
        logger.warning("local replay: git worktree prune failed", exc_info=True)


def cleanup_orphaned_worktree(repo: str, path: str | None) -> list[str]:
    """Remove only the path recorded by a lease whose dead owner was reclaimed."""
    if not path or not Path(path).name.startswith(WORKTREE_PREFIX):
        return []
    remove_worktree(repo, path)
    return [path]


def _changed_paths(worktree: str) -> list[str]:
    out = _git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
    paths: list[str] = []
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            i += 1  # the rename source follows
    return paths


def _compiles(worktree: str, paths: list[str]) -> bool:
    from modules.local_coding_worker import _compile_check

    root = Path(worktree)
    py = [p for p in paths if p.endswith(".py") and (root / p).is_file()]
    return _compile_check(worktree, py, _GIT_TIMEOUT_SECONDS * 2) is None if py else True


# ---- running the merged tests ------------------------------------------------

@dataclass
class MergedTestRun:
    __test__ = False  # not a pytest test class
    total: int
    passed: int
    error: str | None = None


def _test_env(tmp: str) -> dict[str, str]:
    from modules.verification_env import build_verification_test_env

    env = build_verification_test_env()
    # The tests must never write the production database.
    env["AGENT_DB_PATH"] = str(Path(tmp) / "agent.db")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_bounded(cmd: list[str], cwd: str, env: dict[str, str], timeout: float) -> tuple[int | None, str]:
    """``(returncode, output)``; ``returncode`` is ``None`` after a timeout (group killed)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        return None, out or ""


def parse_junit(path: str) -> tuple[int, int]:
    """``(total, passed)`` from a pytest JUnit XML; skipped tests are not counted."""
    root = ET.parse(path).getroot()
    total = passed = 0
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if "skipped" in tags:
            continue
        total += 1
        if not tags & {"failure", "error"}:
            passed += 1
    return total, passed


def run_merged_tests(worktree: str, tests: list[str], timeout: float) -> MergedTestRun:
    """Run *tests* in *worktree* with pytest; a collection error counts as a failed test."""
    if not tests:
        return MergedTestRun(0, 0, "no test files to run")
    with tempfile.TemporaryDirectory(prefix="nexus-shadow-replay-tests-") as tmp:
        env = _test_env(tmp)
        report = str(Path(tmp) / "junit.xml")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               f"--junitxml={report}", *tests]
        code, output = _run_bounded(cmd, worktree, env, timeout)
        if code is None:
            collect_code, listing = _run_bounded(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only", *tests],
                worktree, env, _COLLECT_TIMEOUT_SECONDS,
            )
            total = sum(1 for line in listing.splitlines() if "::" in line) if collect_code is not None else 0
            return MergedTestRun(total, 0, f"merged tests timed out after {timeout:g}s")
        if not Path(report).is_file():
            return MergedTestRun(0, 0, f"pytest exited {code} without a report: {output.strip()[-500:]}")
        try:
            total, passed = parse_junit(report)
        except ET.ParseError as exc:
            return MergedTestRun(0, 0, f"unreadable pytest report: {exc}")
        return MergedTestRun(total, passed, None)


# ---- the local worker --------------------------------------------------------

def _no_free_tier(_prompt: str) -> None:
    """Planner fallback that never calls a model: the replay is local-only."""
    return None


class _UsageCountingClient(LlamaCppClient):
    """The production client, counting the tokens the server reports."""

    tokens_used = 0

    async def chat_completion(self, messages, temperature=0.0, max_tokens=1024, **kwargs):  # type: ignore[override]
        data = await super().chat_completion(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if not isinstance(total, int):
                total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            self.tokens_used += max(0, int(total))
        return data


def default_worker_factory(client: Any) -> Any:
    """The production local worker: subtask splitting as configured, no free tier."""
    from modules.local_coding_worker import (
        LocalCodingWorker, local_samples, sample_temperatures, task_split_enabled,
    )

    return LocalCodingWorker(client=client, split_tasks=task_split_enabled(), planner=_no_free_tier,
                             samples=local_samples(), sample_temperatures=sample_temperatures())


class _RoundCounter:
    """An experiment logger that counts the worker's patch rounds and keeps its run metrics (#639)."""

    def __init__(self) -> None:
        self.rounds = 0
        self.metrics: dict[str, Any] | None = None

    def _record(self, _phase: str, event: str, _message: str = "", **fields: Any) -> None:
        if event == "local_patch_round":
            self.rounds += 1
        elif event == "local_run_metrics":
            self.metrics = dict(fields)

    info = warning = error = _record


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    if isinstance(usage.get("total_tokens"), int):
        return int(usage["total_tokens"])
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))


def _task_text(goal: str) -> str:
    from modules.experiment_service.prompts import _build_direct_implementation_prompt

    return _build_direct_implementation_prompt(goal)


# ---- the replay --------------------------------------------------------------

def _issue_number(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    for table in ("github_experiment_links", "github_work_links"):
        try:
            found = conn.execute(
                f"SELECT issue_number FROM {table} WHERE experiment_id=?", (row["id"],)
            ).fetchone()
        except sqlite3.Error:
            continue
        if found and found[0]:
            return int(found[0])
    if (row["source_type"] or "") == "issue" and str(row["source_id"] or "").isdigit():
        return int(row["source_id"])
    match = re.search(r"GitHub Issue #(\d+)", str(row["goal"] or ""))
    return int(match.group(1)) if match else None


def _load(conn: sqlite3.Connection, experiment_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    row = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown experiment: {experiment_id}")
    adoption = conn.execute(
        "SELECT * FROM experiment_adoptions WHERE experiment_id=?", (experiment_id,)
    ).fetchone()
    if adoption is None or adoption["status"] != MERGED_ADOPTION_STATUS:
        raise ValueError(f"experiment {experiment_id} is not merged")
    if row["implementation_provider"] not in PAID_PROVIDERS:
        raise ValueError(f"experiment {experiment_id} was not implemented by a paid coder")
    if not (row["base_commit_sha"] or "").strip():
        raise ValueError(f"experiment {experiment_id} has no base commit")
    return row, adoption


def _run_replay(repo: str, row: sqlite3.Row, adoption: sqlite3.Row, outcome: ReplayOutcome,
                client: Any, worker_factory: Callable[[Any], Any],
                test_runner: Callable[[str, list[str], float], MergedTestRun], *,
                db_path: str | None = None, lease_token: str | None = None) -> None:
    base = str(row["base_commit_sha"]).strip()
    outcome.base_commit = base
    if not _commit_exists(repo, base):
        raise ReplayError(f"base commit {base[:12]} is not in the local repository")
    old, new = merged_range(repo, adoption["integrated_commit_sha"], adoption["experiment_commit_sha"], base)
    outcome.merged_commit = new
    changes = merged_test_changes(repo, old, new)
    if not any(not c.delete and _TEST_FILE_RE.match(Path(c.path).name) for c in changes):
        outcome.status = STATUS_NO_TESTS
        outcome.error = "the merged diff adds or changes no test file under tests/"
        outcome.test_files = [c.path for c in changes]
        return

    worktree = _create_worktree(repo, base, db_path=db_path, lease_token=lease_token)
    try:
        counter = _RoundCounter()
        worker = worker_factory(client)
        started = time.monotonic()
        try:
            result = worker.run(
                task=_task_text(str(row["goal"])), working_dir=worktree, sandbox="workspace-write",
                timeout_seconds=worker_timeout_seconds(), experiment_logger=counter,
                phase="local_shadow_replay",
            )
        except Exception as exc:
            outcome.seconds = time.monotonic() - started
            outcome.rounds = counter.rounds
            outcome.metrics = counter.metrics
            raise ReplayError(f"local worker raised {type(exc).__name__}: {exc}") from exc
        outcome.seconds = time.monotonic() - started
        outcome.rounds = counter.rounds
        outcome.metrics = counter.metrics
        outcome.tokens = int(getattr(client, "tokens_used", 0) or 0) or _usage_tokens(getattr(result, "usage", None))
        worker_error = None if getattr(result, "success", False) else (
            str(getattr(result, "error", None) or "local worker did not succeed"))
        if worker_error and worker_error.startswith(STATUS_LOCAL_TOO_SLOW):
            # The model was never called: nothing to apply or test (#667).
            outcome.status = STATUS_LOCAL_TOO_SLOW
            outcome.error = worker_error
            return

        changed = _changed_paths(worktree)
        outcome.patch_applied = bool(changed)
        outcome.compiled = bool(changed) and _compiles(worktree, changed)

        # Only now, after the worker, the merged tests come in: the model never sees them.
        tests = import_merged_tests(repo, worktree, new, changes)
        outcome.test_files = tests
        run = test_runner(worktree, tests, tests_timeout_seconds())
        outcome.tests_total, outcome.tests_passed = int(run.total), int(run.passed)
        outcome.status = STATUS_SCORED
        errors = [e for e in (worker_error, run.error) if e]
        outcome.error = "; ".join(errors) if errors else None
    finally:
        remove_worktree(repo, worktree)
        if db_path and lease_token:
            _set_lease_worktree(db_path, lease_token, None)


def replay(
    experiment_id: str,
    *,
    repo_root: str,
    db_path: str = DEFAULT_DB_PATH,
    variant: str | None = None,
    client: Any | None = None,
    worker_factory: Callable[[Any], Any] | None = None,
    test_runner: Callable[[str, list[str], float], MergedTestRun] | None = None,
    unit: str = "whole",
    max_units: int | None = None,
    _global_lease: GlobalLease | None = None,
) -> dict[str, Any] | None:
    """Replay one merged paid experiment locally and store one ``local_replays`` row.

    Returns the stored row, or ``None`` when the experiment is already stored
    for this variant (or another live caller is replaying it). Raises
    ``ValueError`` for an experiment that is unknown, not merged, not paid or
    without base commit. Set-up failures are stored as status ``error``.
    ``unit="hunk"`` runs :func:`modules.local_shadow_replay_hunks.replay_hunks`
    (at most *max_units* new units) and returns its summary dict.
    """
    if unit == "hunk":
        # #649: one merged symbol at a time; returns the experiment's unit summary.
        from modules.local_shadow_replay_hunks import replay_hunks

        return replay_hunks(
            experiment_id, repo_root=repo_root, db_path=db_path, variant=variant,
            client=client, worker_factory=worker_factory, test_runner=test_runner,
            max_units=max_units, _global_lease=_global_lease,
        )
    if unit != "whole":
        raise ValueError("unit must be 'whole' or 'hunk'")
    variant = variant or prompt_variant()
    conn = _connect(db_path)
    owned_lease = _global_lease is None
    global_lease = _global_lease or _acquire_global_lease(conn, lease_seconds())
    if global_lease is None:
        conn.close()
        return None
    try:
        cleanup_orphaned_worktree(repo_root, global_lease.orphaned_worktree)
        row, adoption = _load(conn, experiment_id)
        token = uuid.uuid4().hex
        if _claim(conn, experiment_id, variant, token, lease_seconds()) != "claimed":
            return None
        finalized = False
        try:
            client = client if client is not None else _UsageCountingClient()
            outcome = ReplayOutcome(issue=_issue_number(conn, row),
                                    model=str(getattr(client, "model", "") or ""))
            try:
                _run_replay(repo_root, row, adoption, outcome, client,
                            worker_factory or default_worker_factory, test_runner or run_merged_tests,
                            db_path=db_path, lease_token=global_lease.token)
            except Exception as exc:  # stored, so a broken case is not retried every tick
                outcome.status = STATUS_ERROR
                outcome.error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ReplayError) else str(exc)
                logger.warning("local replay of %s failed: %s", experiment_id, outcome.error)
            finalized = _finalize(conn, experiment_id, variant, token, outcome)
        finally:
            if not finalized:
                try:
                    _release(conn, experiment_id, variant, token)
                except Exception:  # noqa: BLE001 - the lease expires anyway
                    logger.exception("local replay: could not release claim %s", experiment_id)
        return get_replay(experiment_id, variant, db_path=db_path)
    finally:
        if owned_lease:
            _release_global_lease(conn, global_lease.token)
        conn.close()


# ---- the sweep ---------------------------------------------------------------

def next_candidate(conn: sqlite3.Connection, variant: str) -> str | None:
    """The oldest merged paid experiment not yet replayed for *variant*.

    Oldest first, so every variant replays the same experiments in the same order.
    """
    found = conn.execute(
        f"""SELECT e.id FROM experiments AS e
            JOIN experiment_adoptions AS a ON a.experiment_id = e.id
            WHERE a.status = ?
              AND e.implementation_provider IN ({",".join("?" for _ in PAID_PROVIDERS)})
              AND TRIM(COALESCE(e.base_commit_sha, '')) != ''
              AND NOT EXISTS (
                  SELECT 1 FROM local_replays AS r
                  WHERE r.experiment_id = e.id AND r.prompt_variant = ?
                    AND (r.status != ? OR COALESCE(r.lease_expires_at, '') > ?))
            ORDER BY COALESCE(a.ended_at, a.updated_at, e.created_at), e.id
            LIMIT 1""",
        (MERGED_ADOPTION_STATUS, *PAID_PROVIDERS, variant, STATUS_PENDING, _iso(_utcnow())),
    ).fetchone()
    return str(found[0]) if found else None


def server_state(client: Any, db_path: str) -> tuple[bool, str]:
    """``(idle, reason)``. Fail closed: unreachable, unknown errors or any sign
    of local-model work count as not idle."""
    try:
        reachable = asyncio.run(client.is_available())
    except Exception as exc:  # noqa: BLE001
        return False, f"llama-server unreachable: {type(exc).__name__}: {exc}"
    if not reachable:
        return False, "llama-server unreachable"
    try:
        conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            checks = (
                ("SELECT COUNT(*) FROM local_llm_jobs WHERE status IN ('queued', 'running')",
                 (), "local model jobs are queued or running"),
                (f"SELECT COUNT(*) FROM experiments WHERE implementation_provider = 'local' "
                 f"AND status IN ({','.join('?' for _ in _ACTIVE_EXPERIMENT_STATUSES)})",
                 _ACTIVE_EXPERIMENT_STATUSES, "a local experiment is running"),
                ("SELECT COUNT(*) FROM local_replays WHERE status = ? AND lease_expires_at > ?",
                 (STATUS_PENDING, _iso(_utcnow())), "another local replay is running"),
            )
            for sql, params, reason in checks:
                try:
                    count = conn.execute(sql, params).fetchone()[0]
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc):
                        continue
                    raise
                if count:
                    return False, f"llama-server busy: {reason}"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"cannot tell whether the llama-server is idle: {exc}"
    probe = getattr(client, "busy_slots", None)
    if callable(probe):
        try:
            busy = asyncio.run(probe())
        except Exception as exc:  # noqa: BLE001
            return False, f"llama-server unreachable: {type(exc).__name__}: {exc}"
        if busy:
            return False, f"llama-server busy: {busy} slot(s) processing"
    return True, "idle"


def replay_next(
    *,
    repo_root: str,
    db_path: str = DEFAULT_DB_PATH,
    variant: str | None = None,
    client: Any | None = None,
    worker_factory: Callable[[Any], Any] | None = None,
    test_runner: Callable[[str, list[str], float], MergedTestRun] | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """The tick sweep: at most one replay, only on an idle reachable server.

    Returns ``{"ran": bool, "reason": str, "experiment_id": ..., "replay": row}``.
    With ``unit="hunk"`` (or ``NEXUS_SHADOW_REPLAY_UNIT=hunk``) one tick scores
    at most one unit (#649), so a tick stays bounded by one unit's time limit;
    the next tick resumes the same experiment until it is finished.
    """
    variant = variant or prompt_variant()
    unit = unit or os.environ.get(ENV_UNIT, "whole").strip().lower()
    if unit not in {"whole", "hunk"}:
        return {"ran": False, "reason": "unit must be whole or hunk",
                "experiment_id": None, "replay": None}
    if not enabled(db_path):
        return {"ran": False, "reason": "disabled", "experiment_id": None, "replay": None}
    conn = _connect(db_path)
    lease = _acquire_global_lease(conn, lease_seconds())
    if lease is None:
        conn.close()
        return {"ran": False, "reason": "another local replay sweep is running",
                "experiment_id": None, "replay": None}
    try:
        if unit == "hunk":
            from modules.local_shadow_replay_hunks import next_hunk_candidate

            experiment_id = next_hunk_candidate(conn, variant)
        else:
            experiment_id = next_candidate(conn, variant)
        if experiment_id is None:
            return {"ran": False, "reason": "no merged paid experiment left to replay",
                    "experiment_id": None, "replay": None}
        client = client if client is not None else _UsageCountingClient()
        idle, reason = server_state(client, db_path)
        if not idle:
            return {"ran": False, "reason": reason, "experiment_id": experiment_id, "replay": None}
        cleanup_orphaned_worktree(repo_root, lease.orphaned_worktree)
        stored = replay(experiment_id, repo_root=repo_root, db_path=db_path, variant=variant,
                        client=client, worker_factory=worker_factory, test_runner=test_runner,
                        unit=unit, max_units=1 if unit == "hunk" else None, _global_lease=lease)
        if stored is None:
            return {"ran": False, "reason": "already replayed or claimed",
                    "experiment_id": experiment_id, "replay": None}
        return {"ran": True, "reason": stored["status"], "experiment_id": experiment_id,
                "replay": stored}
    finally:
        _release_global_lease(conn, lease.token)
        conn.close()


# ---- read side ---------------------------------------------------------------

_READ_COLUMNS = (
    "experiment_id, issue, model, prompt_variant, status, patch_applied, compiled, "
    "tests_total, tests_passed, rounds, tokens, seconds, error, base_commit, "
    "merged_commit, created_at, finished_at, samples, samples_requested, samples_passed, "
    "cpu_seconds, load_average, peak_rss_kb, tokens_per_second"
)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["patch_applied"] = bool(data["patch_applied"])
    data["compiled"] = bool(data["compiled"])
    data["full_pass"] = is_full_pass(data)
    return data


def is_full_pass(row: dict[str, Any]) -> bool:
    """Every merged test passed on a clean local run; zero-test and errored runs never count."""
    return (row.get("status") == STATUS_SCORED and not row.get("error")
            and int(row.get("tests_total") or 0) > 0
            and int(row.get("tests_passed") or 0) == int(row.get("tests_total") or 0))


def _read_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def get_replay(experiment_id: str, variant: str, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    conn = _read_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT {_READ_COLUMNS} FROM local_replays WHERE experiment_id=? AND prompt_variant=? "
            "AND status != ?",
            (experiment_id, variant, STATUS_PENDING),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return _row_dict(row) if row is not None else None


def list_replays(limit: int = 20, *, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Up to *limit* finished replays, newest first; a missing table means none."""
    if limit <= 0:
        return []
    conn = _read_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_READ_COLUMNS} FROM local_replays WHERE status != ? "
            "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC LIMIT ?",
            (STATUS_PENDING, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [_row_dict(row) for row in rows]


def _stats(variant: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r["status"] == STATUS_SCORED]
    total = sum(int(r["tests_total"]) for r in scored)
    passed = sum(int(r["tests_passed"]) for r in scored)
    full = sum(1 for r in scored if is_full_pass(r))
    seconds = [float(r["seconds"] or 0) for r in scored]
    cpu = [float(r["cpu_seconds"]) for r in scored if r.get("cpu_seconds") is not None]
    rss = [int(r["peak_rss_kb"]) for r in scored if r.get("peak_rss_kb") is not None]
    return {
        "prompt_variant": variant,
        "replays": len(rows),
        "scored": len(scored),
        "no_tests": sum(1 for r in rows if r["status"] == STATUS_NO_TESTS),
        "errors": sum(1 for r in rows if r["status"] == STATUS_ERROR),
        "local_too_slow": sum(1 for r in rows if r["status"] == STATUS_LOCAL_TOO_SLOW),
        "tests_total": total,
        "tests_passed": passed,
        "test_pass_rate": round(passed / total, 4) if total else None,
        "full_passes": full,
        "full_pass_rate": round(full / len(scored), 4) if scored else None,
        # #639: the cost side of a variant (median local seconds per scored replay).
        "median_seconds": round(statistics.median(seconds), 3) if seconds else None,
        "median_cpu_seconds": round(statistics.median(cpu), 3) if cpu else None,
        "peak_rss_kb": max(rss) if rss else None,
        "samples": sorted({int(r.get("samples") or 1) for r in rows}),
    }


def variant_summary(*, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Local pass rates per prompt variant, over all finished replays.

    ``variants``: every row of each variant. ``common``: only the experiments
    scored under every variant, the fair comparison when there is more than one.
    ``test_pass_rate`` is tests_passed/tests_total over scored replays;
    ``full_pass_rate`` the share of scored replays in which every test passed.
    """
    conn = _read_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_READ_COLUMNS} FROM local_replays WHERE status != ?", (STATUS_PENDING,)
        ).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_variant.setdefault(row["prompt_variant"], []).append(row)
    variants = [_stats(name, by_variant[name]) for name in sorted(by_variant)]
    common_ids: set[str] = set()
    if len(by_variant) > 1:
        sets = [{r["experiment_id"] for r in group if r["status"] == STATUS_SCORED}
                for group in by_variant.values()]
        common_ids = set.intersection(*sets)
    common = [
        _stats(name, [r for r in by_variant[name] if r["experiment_id"] in common_ids])
        for name in sorted(by_variant)
    ] if common_ids else []
    return {"variants": variants, "common_experiments": len(common_ids), "common": common,
            "unit_variants": _unit_variants(db_path)}


def _unit_variants(db_path: str) -> list[dict[str, Any]]:
    from modules.local_shadow_replay_hunks import unit_variant_summary

    return unit_variant_summary(db_path=db_path)
