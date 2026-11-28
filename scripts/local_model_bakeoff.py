#!/usr/bin/env python3
"""Compare local GGUF models on speed and ten fixed hunk replays (#690).

The script never keeps the winning model active. It restores the model that
was active at startup even when a candidate fails, an exception escapes, or
the process is interrupted.

Two hosts can produce bake-off rows, kept apart by the ``host`` column
(#857): ``vm`` (the default path below) and ``home-gpu``. ``--via home-gpu``
submits one ``bakeoff_prompt`` job per fixture case through
:func:`modules.home_gpu.submit_job` and returns at once without writing any
rows; ``--collect`` later reads the answered ``home_gpu_jobs`` rows, scores
each answer with the same checker the VM replay uses
(:func:`modules.local_shadow_replay_hunks.check_unit_text`) and writes the
normal bake-off rows with ``host='home-gpu'``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import home_gpu  # noqa: E402
from modules import local_shadow_replay as lsr  # noqa: E402
from modules import local_shadow_replay_hunks as lsh  # noqa: E402
from modules.home_gpu.collect import _ensure_schema, collect_replies  # noqa: E402
from modules.home_gpu.jobs import DEFAULT_DEADLINE_S as HOME_GPU_DEADLINE_S  # noqa: E402
from modules.local_model_bakeoff import (  # noqa: E402
    choose_winner,
    insert_bakeoff_row,
    passing_hunks_per_hour,
)
from modules.local_shadow_replay_hunks import replay_hunks  # noqa: E402
from modules.local_model_rates import LocalModelRates  # noqa: E402
from modules.run_store import DEFAULT_DB_PATH, init_db  # noqa: E402

from scripts import bench_local_speed  # noqa: E402

MODEL_COMMAND = ("sudo", "-n", "/usr/local/sbin/nexus-llama-model")
DEFAULT_FIXTURE = ROOT / "benchmarks" / "bakeoff_hunks.json"

#: Bake-off rows are tagged with the host that produced them (#857) so VM and
#: home-PC numbers are never mixed; rows written before the column existed
#: read as ``vm``.
HOST_VM = "vm"
HOST_HOME_GPU = "home-gpu"
#: The ``home_gpu_jobs`` kind one bake-off case is submitted as.
BAKEOFF_KIND = "bakeoff_prompt"


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)


def current_model(run_command: Callable[[list[str]], Any] = command) -> str:
    result = run_command([*MODEL_COMMAND, "list"])
    if int(getattr(result, "returncode", 1)) != 0:
        raise RuntimeError(getattr(result, "stderr", "model list failed").strip())
    match = re.search(r"^\s*MODEL=([^\s]+)\s*$", str(result.stdout), re.MULTILINE)
    if not match:
        raise RuntimeError("nexus-llama-model list did not report MODEL")
    return match.group(1)


def switch_model(model: str, run_command: Callable[[list[str]], Any] = command) -> None:
    result = run_command([*MODEL_COMMAND, "switch", f"MODEL={model}"])
    if int(getattr(result, "returncode", 1)) != 0:
        message = str(getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
        raise RuntimeError(message or f"model switch exited {result.returncode}")


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:60]


def _units(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((summary or {}).get("units") or [])


def fixture_experiment_id(item: dict[str, Any], _db_path: str) -> str:
    """Return the immutable experiment identity stored in the fixture.

    In particular, never resolve an issue number to its newest retry: that made
    previous bake-offs silently compare different work over time.
    """
    value = item.get("experiment_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("fixture entry needs a non-empty fixed experiment_id")
    return value.strip()


class SeededReplayClient(lsr._UsageCountingClient):
    """Production replay client pinned to the candidate and bake-off seed."""

    def __init__(self, *, model: str, seed: int, timeout: float) -> None:
        super().__init__(model=model, timeout=timeout)
        self._bakeoff_seed = seed

    async def chat_completion(self, messages, temperature=0.0, max_tokens=1024, **kwargs):
        kwargs["seed"] = self._bakeoff_seed
        return await super().chat_completion(
            messages, temperature=temperature, max_tokens=max_tokens, **kwargs)


def _record_benchmark_rates(model: str, speed: dict[str, Any], rates_path: str | None) -> None:
    """Make hunk admission use this candidate's measured rates immediately."""
    rates = (speed.get("summary") or {}).get("all") or {}
    prompt = rates.get("prompt_tokens_per_second")
    output = rates.get("output_tokens_per_second")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               for v in (prompt, output)):
        raise RuntimeError("speed benchmark did not produce positive prompt/output rates")
    # LocalModelRates consumes llama-server timing fields. One second of
    # synthetic timing exactly preserves each aggregate measured rate.
    store = LocalModelRates(rates_path, model=model)
    stored = store.record({
        "prompt_n": float(prompt), "prompt_ms": 1000.0,
        "predicted_n": float(output), "predicted_ms": 1000.0,
    })
    measured = store.medians()
    if not stored or any(value is None for value in measured):
        raise RuntimeError("could not store candidate-specific benchmark rates")


@contextlib.contextmanager
def _replay_environment(timeout: float, rates_path: str | None):
    changes = {lsh.ENV_UNIT_TIMEOUT: str(timeout)}
    if rates_path:
        changes["LOCAL_MODEL_RATES_PATH"] = rates_path
    old = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _replay_when_available(replay_fn: Callable[..., dict[str, Any] | None],
                           experiment_id: str, *, lease_wait_seconds: float,
                           sleep_fn: Callable[[float], None] = time.sleep,
                           monotonic_fn: Callable[[], float] = time.monotonic,
                           **kwargs: Any) -> dict[str, Any]:
    deadline = monotonic_fn() + max(0.0, lease_wait_seconds)
    while True:
        result = replay_fn(experiment_id, **kwargs)
        if result is not None:
            return result
        if monotonic_fn() >= deadline:
            raise TimeoutError("shadow replay global lease remained busy")
        sleep_fn(min(1.0, max(0.0, deadline - monotonic_fn())))


def score_candidate(
    model: str,
    fixture: list[dict[str, Any]],
    *,
    repo_root: str,
    db_path: str,
    url: str,
    seed: int,
    timeout: float,
    run_id: str,
    bench_fn: Callable[..., dict[str, Any]] = bench_local_speed.bench,
    replay_fn: Callable[..., dict[str, Any] | None] = replay_hunks,
    client: Any | None = None,
    rates_path: str | None = None,
    lease_wait_seconds: float = 60.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    speed = bench_fn(url, seed=seed, timeout=timeout)
    _record_benchmark_rates(model, speed, rates_path)
    # ``timeout`` becomes the hunk unit timeout in _replay_environment below.
    call_timeout = min(lsr.offline_local_call_timeout_seconds(), timeout)
    replay_client = client or SeededReplayClient(model=model, seed=seed, timeout=call_timeout)
    # Test doubles and injected transports are supported, but admission must
    # still key rates by the candidate that was actually switched in.
    replay_client.model = model
    all_speed = (speed.get("summary") or {}).get("all") or {}
    outcomes: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, item in enumerate(fixture):
        variant = f"bakeoff-{run_id[:8]}-{_slug(model)}-{index:02d}"
        try:
            experiment_id = fixture_experiment_id(item, db_path)
            with _replay_environment(timeout, rates_path):
                summary = _replay_when_available(
                    replay_fn, experiment_id, lease_wait_seconds=lease_wait_seconds,
                    sleep_fn=sleep_fn, monotonic_fn=monotonic_fn,
                    repo_root=repo_root, db_path=db_path, variant=variant,
                    client=replay_client, max_units=1,
                )
        except TimeoutError as exc:
            # Contention is a run condition, never evidence that the model
            # failed a hunk. Preserve it distinctly and stop this candidate.
            outcomes.append({"status": "lease_busy", "error": str(exc)})
            break
        except (ValueError, sqlite3.Error) as exc:
            outcomes.append({"status": "fixture_error", "error": f"{type(exc).__name__}: {exc}"})
            continue
        units = _units(summary)
        if units:
            # A fresh variant and max_units=1 yield exactly one scored unit.
            outcomes.append(units[-1])
        else:
            outcomes.append({"status": (summary or {}).get("status", "error")})
    seconds = sum(float(u.get("seconds") or 0) for u in outcomes)
    if seconds <= 0:
        seconds = time.monotonic() - started
    row: dict[str, Any] = {
        "model": model,
        "status": "lease_busy" if any(u.get("status") == "lease_busy" for u in outcomes)
                  else "completed",
        "prompt_tokens_per_second": all_speed.get("prompt_tokens_per_second"),
        "output_tokens_per_second": all_speed.get("output_tokens_per_second"),
        "hunks_total": len(outcomes),
        "hunks_applied": sum(bool(u.get("applied")) for u in outcomes),
        "hunks_compiled": sum(bool(u.get("compiled")) for u in outcomes),
        "passing_hunks": sum(bool(u.get("tests_passed")) for u in outcomes),
        "local_too_slow": sum(u.get("status") == "local_too_slow" for u in outcomes),
        "seconds": round(seconds, 3),
        "details": {"speed": speed, "hunks": outcomes, "seed": seed,
                    "timeout_seconds": timeout, "model": model},
    }
    row["passing_hunks_per_hour"] = round(passing_hunks_per_hour(row), 3)
    return row


def _load_fixture(fixture_path: Path, db_path: str) -> list[dict[str, Any]]:
    """The bake-off cases: exactly 10 fixed ``experiment_id`` fixture entries."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if (not isinstance(fixture, list) or len(fixture) != 10
            or any(not isinstance(item, dict) or not isinstance(item.get("experiment_id"), str)
                   or not item["experiment_id"].strip() for item in fixture)):
        raise ValueError("bakeoff fixture must contain exactly 10 fixed experiment_id entries")
    return fixture


# ---- home GPU (#857) ----------------------------------------------------------

#: Appended to the VM micro-task: the PC answers in one shot, not with tool
#: calls, so it has to return the whole file.
ANSWER_INSTRUCTIONS = (
    "\n\nThe current content of {file} is given as file_text. Reply with the complete "
    "new content of {file} and nothing else - no explanation, no diff."
)
CASE_SUBMITTING = "submitting"
CASE_SUBMITTED = "submitted"
CASE_FALLBACK = "fallback"
CASE_COLLECTED = "collected"
_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n?```\s*\Z", re.DOTALL)


def _now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def resolve_case(experiment_id: str, *, repo_root: str, db_path: str) -> dict[str, Any]:
    """The unit the VM bake-off replays for *experiment_id*, with its context.

    The VM path runs :func:`replay_hunks` on a fresh variant with
    ``max_units=1``, which scores ``hunk_units(...)[0]``; this resolves the
    same unit, the issue title and commit summary of its micro-task and the
    merged tests that touch its module. Raises ``ValueError`` for an unknown
    or unmerged experiment and for a diff without non-test Python code.
    """
    conn = lsr._connect(db_path)
    try:
        row, adoption = lsr._load(conn, experiment_id)
        goal = str(row["goal"] or "")
        base = str(row["base_commit_sha"]).strip()
        integrated = adoption["integrated_commit_sha"]
        experiment_sha = adoption["experiment_commit_sha"]
    finally:
        conn.close()
    old, new = lsr.merged_range(repo_root, integrated, experiment_sha, base)
    units = lsh.hunk_units(repo_root, old, new)
    if not units:
        raise ValueError("the merged diff changes no non-test Python code")
    unit = units[0]
    summary = lsr._git(repo_root, ["show", "-s", "--format=%s", new]).stdout.strip()
    test_changes = lsr.merged_test_changes(repo_root, old, new)
    return {
        "unit": unit, "old": old, "new": new, "title": lsh.issue_title(goal),
        "summary": summary,
        "tests": lsh.unit_test_files(repo_root, new, test_changes, unit.file),
    }


def bakeoff_case_payload(item: dict[str, Any], index: int, *, repo_root: str,
                         db_path: str) -> dict[str, Any]:
    """The ``bakeoff_prompt`` payload for fixture case *index*.

    It carries everything the PC needs and nothing private: the VM
    micro-task as ``prompt``, the file the VM worker would start from as
    ``file_text`` (for a symbol unit in a large file the reduced text, exactly
    :func:`modules.local_shadow_replay_hunks.worker_start_text`) and the
    repo-relative ``file``/``symbol``/``commit`` that identify the unit.
    ``--collect`` scores the answer against this very ``file_text``.
    """
    experiment_id = fixture_experiment_id(item, db_path)
    case = resolve_case(experiment_id, repo_root=repo_root, db_path=db_path)
    unit = case["unit"]
    start_text, reduced = lsh.worker_start_text(unit)
    prompt = (lsh.micro_task(case["title"], case["summary"], unit, reduced=reduced)
              + ANSWER_INSTRUCTIONS.format(file=unit.file))
    return {
        "bakeoff": {"experiment_id": experiment_id, "index": index},
        "commit": case["new"],
        "file": unit.file,
        "symbol": unit.symbol,
        "reduced": reduced,
        "prompt": prompt,
        "file_text": start_text,
    }


def answer_file_text(answer: str) -> str:
    """The file text in a PC answer: one fence around the whole answer is dropped.

    Both the VM-scorer parity check and ``--collect`` score this same text.
    """
    match = _FENCE.match(answer)
    text = match.group(1) if match else answer
    return text if text.endswith("\n") or not text else text + "\n"


def _pending_case(conn: sqlite3.Connection, digest: str) -> sqlite3.Row | None:
    """The newest uncollected case for *digest* that is still in flight, if any.

    In flight means: its job is ``queued`` or ``answered`` (waiting for
    ``--collect``), or the submission never finished (status ``submitting``).
    Expired and fallback jobs are not in flight - that case may be sent again.
    """
    return conn.execute(
        """
        SELECT c.job_id, c.run_id, c.status AS case_status, j.status AS job_status
        FROM local_model_bakeoff_home_gpu_cases AS c
        LEFT JOIN home_gpu_jobs AS j ON j.job_id = c.job_id
        WHERE c.input_sha256 = ? AND c.collected_at IS NULL
          AND (j.status IN ('queued', 'answered')
               OR (j.job_id IS NULL AND c.status = ?))
        ORDER BY c.created_at DESC, c.job_id DESC LIMIT 1
        """,
        (digest, CASE_SUBMITTING),
    ).fetchone()


def _reconcile_submitting(conn: sqlite3.Connection) -> None:
    """Settle cases a crash left in ``submitting`` although ``submit_job`` finished.

    A job that fell back to the VM (or was dropped) or already expired must
    not be re-submitted under its id: the fallback would queue the VM work a
    second time. Only cases without a job row, or with a still ``queued`` one
    (the mailbox file may be missing), stay ``submitting`` and are resumed.
    """
    with conn:
        conn.execute(
            f"""
            UPDATE local_model_bakeoff_home_gpu_cases
            SET status = CASE (SELECT j.status FROM home_gpu_jobs AS j
                               WHERE j.job_id = local_model_bakeoff_home_gpu_cases.job_id)
                         WHEN 'expired' THEN '{CASE_SUBMITTED}' ELSE '{CASE_FALLBACK}' END
            WHERE status = ? AND job_id IN (
                SELECT job_id FROM home_gpu_jobs
                WHERE status IN ('fallback', 'home_gpu_expired', 'expired'))
            """,
            (CASE_SUBMITTING,),
        )


def submit_home_gpu_jobs(
    fixture: list[dict[str, Any]],
    *,
    db_path: str,
    repo_root: str = str(ROOT),
    mailbox_dir: Path | str,
    deadline_s: int = HOME_GPU_DEADLINE_S,
    commit: bool = True,
    run_id: str | None = None,
    now: datetime | None = None,
    submit_fn: Callable[..., str] | None = None,
    payload_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Submit one ``bakeoff_prompt`` job per fixture case via the home GPU (#857).

    Every case goes through :func:`modules.home_gpu.submit_job` (the canonical
    entry point: it tracks the job in ``home_gpu_jobs`` and falls back to the
    VM path when the PC counts as off). The function returns at once and
    writes **no** bake-off rows; :func:`collect_home_gpu_bakeoffs` does that
    once the PC answered.

    Crash and re-run safety: the case row (with its payload) is committed
    under a fresh job id *before* ``submit_job`` runs. A run interrupted
    between the two, or by a failed mailbox commit, leaves the case in
    ``submitting``; the next run re-submits it under the same job id, which
    ``submit_job`` turns into a republish, never a second job. A case whose
    job is still queued or answered-but-not-collected is not sent again, so
    running ``--via home-gpu`` twice does not double the PC's work. Raises
    ``HomeGpuDisabled`` before anything is written when ``NEXUS_HOME_GPU`` is
    not ``1``.
    """
    if not home_gpu.home_gpu_enabled():
        raise home_gpu.HomeGpuDisabled(
            f"{home_gpu.jobs.ENV_FLAG}=1 is not set; home-GPU jobs are off")
    submit = submit_fn or home_gpu.submit_job
    build = payload_fn or (lambda item, index: bakeoff_case_payload(
        item, index, repo_root=repo_root, db_path=db_path))
    run_id = run_id or uuid.uuid4().hex
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        # Retire jobs whose deadline passed, so their cases may be sent again.
        home_gpu.expire_jobs(conn, now)
        _reconcile_submitting(conn)
        conn.commit()
        jobs: list[dict[str, Any]] = []
        for index, item in enumerate(fixture):
            entry: dict[str, Any] = {"index": index,
                                     "experiment_id": str(item.get("experiment_id") or "")}
            jobs.append(entry)
            try:
                payload = build(item, index)
                body = home_gpu.payload_body(payload)
            except home_gpu.PayloadRejected as exc:
                entry.update(status="rejected", error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one bad case must not stop the others
                entry.update(status="fixture_error", error=f"{type(exc).__name__}: {exc}")
                continue
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            pending = _pending_case(conn, digest)
            if pending is not None and (pending["case_status"] != CASE_SUBMITTING
                                        or pending["job_status"] == "answered"):
                entry.update(status="already_pending", job_id=pending["job_id"],
                             run_id=pending["run_id"])
                continue
            if pending is not None:
                job_id, case_run = str(pending["job_id"]), str(pending["run_id"])
                entry["resumed"] = True
            else:
                job_id = home_gpu.jobs._new_job_id(datetime.now(timezone.utc))
                case_run = run_id
                with conn:
                    conn.execute(
                        "INSERT INTO local_model_bakeoff_home_gpu_cases (job_id, run_id, "
                        "created_at, experiment_id, case_index, input_sha256, payload_json, "
                        "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (job_id, case_run, _now_iso(now), payload["bakeoff"]["experiment_id"],
                         index, digest, json.dumps(payload, sort_keys=True), CASE_SUBMITTING),
                    )
            entry.update(job_id=job_id, run_id=case_run,
                         experiment_id=payload["bakeoff"]["experiment_id"])
            try:
                result = submit(conn, BAKEOFF_KIND, payload, deadline_s,
                                mailbox_dir=mailbox_dir, commit=commit, now=now, job_id=job_id)
            except home_gpu.HomeGpuDisabled:
                raise
            except home_gpu.HomeGpuJobError as exc:
                # The case stays ``submitting``: the next run resumes it.
                conn.rollback()
                with conn:
                    conn.execute("UPDATE local_model_bakeoff_home_gpu_cases SET error = ? "
                                 "WHERE job_id = ?", (str(exc)[:1000], job_id))
                entry.update(status="error", error=str(exc))
                continue
            status = CASE_FALLBACK if result == home_gpu.FALLBACK else CASE_SUBMITTED
            with conn:
                conn.execute("UPDATE local_model_bakeoff_home_gpu_cases SET status = ?, "
                             "error = NULL WHERE job_id = ?", (status, job_id))
            entry["status"] = status
        counts = {name: sum(1 for job in jobs if job.get("status") == name)
                  for name in (CASE_SUBMITTED, CASE_FALLBACK, "already_pending", "rejected",
                               "fixture_error", "error")}
        return {"run_id": run_id, "via": HOST_HOME_GPU, "jobs": jobs,
                "submitted": counts[CASE_SUBMITTED], "fallback": counts[CASE_FALLBACK],
                "already_pending": counts["already_pending"], "rejected": counts["rejected"],
                "fixture_errors": counts["fixture_error"], "errors": counts["error"]}
    finally:
        conn.close()


def score_home_gpu_answer(payload: dict[str, Any], answer: str, *, repo_root: str,
                          db_path: str) -> dict[str, Any]:
    """Score one home-GPU answer with the checker the VM replay uses (#857).

    Re-resolves the case's unit (the one the VM bake-off replays) and runs
    :func:`modules.local_shadow_replay_hunks.score_unit_text` - the VM
    replay's baseline step plus :func:`check_unit_text` - on the answer, with
    the payload's ``file_text`` as the start text: the PC is judged against
    exactly the text it was given, as the VM worker is against its own.
    """
    experiment_id = str(payload["bakeoff"]["experiment_id"])
    case = resolve_case(experiment_id, repo_root=repo_root, db_path=db_path)
    unit = case["unit"]
    if (unit.file, unit.symbol, case["new"]) != (payload.get("file"), payload.get("symbol"),
                                                 payload.get("commit")):
        raise ValueError(f"the case of {experiment_id} no longer resolves to the submitted unit")
    module = lsh.module_name(unit.file)
    worktree = lsr._create_worktree(repo_root, case["new"], db_path=db_path)
    try:
        outcome = lsh.score_unit_text(
            worktree, unit, answer, start_text=str(payload["file_text"]), tests=case["tests"],
            test_runner=lambda wt, t, timeout: lsh.run_unit_tests(wt, t, module, timeout),
        )
    finally:
        lsr.remove_worktree(repo_root, worktree)
    return {
        "status": outcome.status, "applied": bool(outcome.applied),
        "compiled": bool(outcome.compiled), "has_tests": bool(outcome.has_tests),
        "tests_passed": bool(outcome.tests_passed), "tests_total": outcome.tests_total,
        "tests_ok": outcome.tests_ok, "overlap": outcome.overlap, "error": outcome.error,
        "file": unit.file, "symbol": unit.symbol,
    }


def _uncollected_cases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.job_id, c.run_id, c.experiment_id, c.case_index, c.payload_json,
               c.status AS case_status, j.status AS job_status, j.model, j.prompt_tokens,
               j.completion_tokens, j.seconds, j.answer_path, j.answer_sha256
        FROM local_model_bakeoff_home_gpu_cases AS c
        LEFT JOIN home_gpu_jobs AS j ON j.job_id = c.job_id
        WHERE c.collected_at IS NULL
        ORDER BY c.created_at, c.run_id, c.case_index, c.job_id
        """
    ).fetchall()


def _home_gpu_row(run_id: str, model: str, entries: list[dict[str, Any]],
                  created_at: str) -> dict[str, Any]:
    seconds = sum(float(e["seconds"] or 0) for e in entries)
    prompt_tokens = sum(int(e["prompt_tokens"] or 0) for e in entries)
    completion_tokens = sum(int(e["completion_tokens"] or 0) for e in entries)
    hunks = [{**e["outcome"], "job_id": e["job_id"], "experiment_id": e["experiment_id"],
              "index": e["index"], "seconds": e["seconds"], "prompt_tokens": e["prompt_tokens"],
              "completion_tokens": e["completion_tokens"], "answer_sha256": e["answer_sha256"]}
             for e in entries]
    row: dict[str, Any] = {
        "run_id": run_id, "created_at": created_at, "model": model, "is_current": 0,
        "status": "completed",
        "prompt_tokens_per_second": round(prompt_tokens / seconds, 3) if seconds > 0 else None,
        "output_tokens_per_second": round(completion_tokens / seconds, 3) if seconds > 0 else None,
        "hunks_total": len(hunks),
        "hunks_applied": sum(bool(h.get("applied")) for h in hunks),
        "hunks_compiled": sum(bool(h.get("compiled")) for h in hunks),
        "passing_hunks": sum(bool(h.get("tests_passed")) for h in hunks),
        "local_too_slow": sum(h.get("status") == "local_too_slow" for h in hunks),
        "seconds": round(seconds, 3), "winner": 0, "recommend_switch": 0,
        "details_json": json.dumps({"host": HOST_HOME_GPU, "model": model, "hunks": hunks}),
        "error": None, "host": HOST_HOME_GPU,
    }
    row["passing_hunks_per_hour"] = round(passing_hunks_per_hour(row), 3)
    return row


def collect_home_gpu_bakeoffs(
    *,
    db_path: str,
    repo_root: str = str(ROOT),
    mailbox_dir: Path | str | None = None,
    collect_fn: Callable[..., int] | None = None,
    score_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score answered home-GPU bake-off jobs into bake-off rows (``host='home-gpu'``).

    With *mailbox_dir* and ``NEXUS_HOME_GPU=1`` pending outbox replies are
    collected first (:func:`modules.home_gpu.collect.collect_replies`, the
    call the mailbox poll makes, answers under ``<repo_root>/state``). Then
    every submitted case whose ``home_gpu_jobs`` row is ``answered`` is scored
    against the start text it was sent with, and one normal bake-off row per
    ``(run_id, model)`` is written with ``host='home-gpu'``. Queued, expired,
    fallback and never-submitted cases are reported as ``waiting`` and are
    never scored as failures; a later ``--collect`` picks them up once
    answered.

    Crash and re-run safety: scoring writes nothing. The rows and the
    ``collected_at`` mark of their cases are written in one ``BEGIN
    IMMEDIATE`` transaction that first re-checks which cases are still
    uncollected, so an interrupted run writes nothing and a repeated or
    concurrent run never writes a case twice.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        conn.commit()
        if mailbox_dir is not None and home_gpu.home_gpu_enabled():
            (collect_fn or collect_replies)(mailbox_dir, conn, Path(repo_root) / "state")
            conn.commit()
        score = score_fn or (lambda payload, answer: score_home_gpu_answer(
            payload, answer, repo_root=repo_root, db_path=db_path))
        scored: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for case in _uncollected_cases(conn):
            job_id = str(case["job_id"])
            if case["job_status"] != "answered" or not case["answer_path"]:
                waiting.append({"job_id": job_id, "index": case["case_index"],
                                "status": case["job_status"] or case["case_status"]})
                continue
            try:
                raw = Path(str(case["answer_path"])).read_bytes()
            except OSError as exc:
                skipped.append({"job_id": job_id, "reason": f"answer file unreadable: {exc}"})
                continue
            if case["answer_sha256"] and hashlib.sha256(raw).hexdigest() != case["answer_sha256"]:
                skipped.append({"job_id": job_id,
                                "reason": "answer file does not match its recorded sha256"})
                continue
            payload = json.loads(case["payload_json"])
            try:
                outcome = score(payload, answer_file_text(raw.decode("utf-8")))
            except (ValueError, sqlite3.Error) as exc:
                # Same classification as the VM path's fixture errors.
                outcome = {"status": "fixture_error", "error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:  # noqa: BLE001 - one bad case must not stop the others
                outcome = {"status": lsh.UNIT_ERROR, "error": f"{type(exc).__name__}: {exc}"}
            scored.append({"job_id": job_id, "run_id": str(case["run_id"]),
                           "experiment_id": case["experiment_id"], "index": case["case_index"],
                           "model": str(case["model"] or HOST_HOME_GPU),
                           "seconds": case["seconds"], "prompt_tokens": case["prompt_tokens"],
                           "completion_tokens": case["completion_tokens"],
                           "answer_sha256": case["answer_sha256"], "outcome": outcome})

        written: list[dict[str, Any]] = []
        if scored:
            created_at = _now_iso()
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [e["job_id"] for e in scored]
                still = {str(r[0]) for r in conn.execute(
                    "SELECT job_id FROM local_model_bakeoff_home_gpu_cases WHERE collected_at "
                    f"IS NULL AND job_id IN ({','.join('?' for _ in ids)})", ids)}
                for entry in scored:
                    if entry["job_id"] not in still:
                        skipped.append({"job_id": entry["job_id"], "reason": "already collected"})
                groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for entry in scored:
                    if entry["job_id"] in still:
                        groups.setdefault((entry["run_id"], entry["model"]), []).append(entry)
                for (run_id, model), entries in sorted(groups.items()):
                    row = _home_gpu_row(run_id, model, entries, created_at)
                    bakeoff_id = insert_bakeoff_row(conn, row, commit=False)
                    conn.executemany(
                        "UPDATE local_model_bakeoff_home_gpu_cases SET collected_at = ?, "
                        "bakeoff_id = ?, status = ? WHERE job_id = ? AND collected_at IS NULL",
                        [(created_at, bakeoff_id, CASE_COLLECTED, e["job_id"]) for e in entries],
                    )
                    written.append(row)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"via": HOST_HOME_GPU,
                "scored": sum(len(json.loads(r["details_json"])["hunks"]) for r in written),
                "written": len(written), "waiting": waiting, "skipped": skipped,
                "rows": written}
    finally:
        conn.close()


def run_bakeoff(
    candidates: list[str],
    *,
    repo_root: str = str(ROOT),
    db_path: str = DEFAULT_DB_PATH,
    fixture_path: Path = DEFAULT_FIXTURE,
    url: str = "http://127.0.0.1:8080",
    seed: int = 42,
    timeout: float = 600.0,
    run_command: Callable[[list[str]], Any] = command,
    bench_fn: Callable[..., dict[str, Any]] = bench_local_speed.bench,
    replay_fn: Callable[..., dict[str, Any] | None] = replay_hunks,
    client: Any | None = None,
    rates_path: str | None = None,
    lease_wait_seconds: float = 60.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("at least one candidate is required")
    fixture = _load_fixture(fixture_path, db_path)
    original = current_model(run_command)
    run_id = uuid.uuid4().hex
    ephemeral_rates = rates_path is None
    rates_path = rates_path or str(Path(db_path).parent / f".bakeoff-rates-{run_id}.json")
    created_at = datetime.now(timezone.utc).isoformat()
    conn = init_db(db_path)
    rows: list[dict[str, Any]] = []
    try:
        for candidate_index, model in enumerate(candidates):
            base = {"run_id": run_id, "created_at": created_at, "model": model,
                    "is_current": int(model == original), "winner": 0,
                    "recommend_switch": 0}
            try:
                switch_model(model, run_command)
            except Exception as exc:  # one unavailable candidate does not invalidate the run
                row = {**base, "status": "switch_failed", "hunks_total": 0,
                       "hunks_applied": 0, "hunks_compiled": 0, "passing_hunks": 0,
                       "local_too_slow": 0, "seconds": 0, "passing_hunks_per_hour": 0,
                       "details_json": None, "error": f"{type(exc).__name__}: {exc}"[:1000]}
                insert_bakeoff_row(conn, row)
                rows.append(row)
                continue
            try:
                scored = score_candidate(
                    model, fixture, repo_root=repo_root, db_path=db_path, url=url,
                    seed=seed, timeout=timeout, run_id=run_id, bench_fn=bench_fn,
                    replay_fn=replay_fn, client=client, rates_path=rates_path,
                    lease_wait_seconds=lease_wait_seconds, sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )
            except BaseException as exc:
                # A durable partial row makes interruption/crash observable;
                # BaseException is immediately re-raised after persistence.
                row = {**base, "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                       "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0,
                       "passing_hunks": 0, "local_too_slow": 0, "seconds": 0,
                       "passing_hunks_per_hour": 0, "details_json": json.dumps({
                           "seed": seed, "timeout_seconds": timeout, "model": model}),
                       "error": f"{type(exc).__name__}: {exc}"[:1000]}
                insert_bakeoff_row(conn, row)
                rows.append(row)
                for pending_model in candidates[candidate_index + 1:]:
                    pending = {
                        "run_id": run_id, "created_at": created_at, "model": pending_model,
                        "is_current": int(pending_model == original), "status": "not_run",
                        "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0,
                        "passing_hunks": 0, "local_too_slow": 0, "seconds": 0,
                        "passing_hunks_per_hour": 0, "winner": 0, "recommend_switch": 0,
                        "details_json": json.dumps({"seed": seed,
                                                    "timeout_seconds": timeout,
                                                    "model": pending_model}),
                        "error": "an earlier candidate interrupted the run",
                    }
                    insert_bakeoff_row(conn, pending)
                    rows.append(pending)
                raise
            details = scored.pop("details")
            row = {**base, **scored, "details_json": json.dumps(details), "error": None}
            insert_bakeoff_row(conn, row)
            rows.append(row)
        selection = choose_winner(rows, original)
        winner = selection["winner"]
        if winner is not None:
            winner["winner"] = 1
            winner["recommend_switch"] = int(selection["recommend_switch"])
            conn.execute(
                "UPDATE local_model_bakeoffs SET winner=1, recommend_switch=? WHERE run_id=? AND model=?",
                (int(selection["recommend_switch"]), run_id, winner["model"]),
            )
            conn.commit()
        return {"run_id": run_id, "original_model": original, "rows": rows,
                "winner": winner["model"] if winner else None,
                "recommend_switch": bool(selection["recommend_switch"])}
    finally:
        try:
            switch_model(original, run_command)
        finally:
            try:
                conn.close()
            finally:
                if ephemeral_rates:
                    try:
                        Path(rates_path).unlink()
                    except FileNotFoundError:
                        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", nargs="*",
                        help="downloaded GGUF filenames, in scoring order (VM path only)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=600.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--via", choices=("vm", HOST_HOME_GPU), default=HOST_VM,
                      help="where to run the bake-off: 'vm' (default) switches models locally; "
                           "'home-gpu' submits one bakeoff_prompt job per case through "
                           "modules.home_gpu.submit_job and returns at once (no rows yet)")
    mode.add_argument("--collect", action="store_true",
                      help="score the answered home-GPU bakeoff_prompt jobs and write bake-off "
                           "rows with host='home-gpu'")
    parser.add_argument("--mailbox-dir",
                        default=os.environ.get("AGENT_MAILBOX_REPO_PATH"),
                        help="mailbox working copy (default AGENT_MAILBOX_REPO_PATH); "
                             "required for --via home-gpu")
    parser.add_argument("--deadline", type=int, default=HOME_GPU_DEADLINE_S,
                        help="home-GPU job deadline in seconds (--via home-gpu)")
    args = parser.parse_args(argv)

    if args.collect:
        if args.candidates:
            parser.error("candidates are not used with --collect")
        report = collect_home_gpu_bakeoffs(
            db_path=args.db_path, repo_root=args.repo_root, mailbox_dir=args.mailbox_dir,
        )
        print(json.dumps(report, indent=2))
        return 0

    if args.via == HOST_HOME_GPU:
        if args.candidates:
            parser.error("candidates are not used with --via home-gpu; "
                         "the home PC runs its own model")
        if not home_gpu.home_gpu_enabled():
            print(f"error: --via home-gpu requires {home_gpu.jobs.ENV_FLAG}=1 "
                  "(home-GPU jobs are off); nothing was submitted", file=sys.stderr)
            return 2
        if not args.mailbox_dir:
            print("error: --via home-gpu needs the mailbox working copy "
                  "(--mailbox-dir or AGENT_MAILBOX_REPO_PATH); nothing was submitted",
                  file=sys.stderr)
            return 2
        try:
            fixture = _load_fixture(args.fixture, args.db_path)
            report = submit_home_gpu_jobs(
                fixture, db_path=args.db_path, repo_root=args.repo_root,
                mailbox_dir=args.mailbox_dir, deadline_s=args.deadline, commit=True,
            )
        except home_gpu.HomeGpuDisabled as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2))
        in_flight = report["submitted"] + report["fallback"] + report["already_pending"]
        if report["errors"] or not in_flight:
            print(f"error: {report['errors']} case(s) failed to submit, "
                  f"{report['rejected']} rejected, {report['fixture_errors']} unresolvable; "
                  "re-run --via home-gpu to resume", file=sys.stderr)
            return 1
        return 0

    if not args.candidates:
        parser.error("candidates are required unless --via home-gpu or --collect is given")
    report = run_bakeoff(args.candidates, repo_root=args.repo_root, db_path=args.db_path,
                         fixture_path=args.fixture, url=args.url, seed=args.seed,
                         timeout=args.timeout)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
