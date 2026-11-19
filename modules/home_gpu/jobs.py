"""Submit public-repository inputs to the home GPU mailbox lane (#743).

``submit_job`` only writes the durable SQLite row. The mailbox poll, which
already owns the mailbox lock and Git sync, materializes the signed inbox
file and publishes it (``collect.publish_pending``). A crash between the two
leaves a ``queued`` row with ``published_at IS NULL`` that the next poll picks
up again.

The "no company data" rule as code: a request is read from a Git object of
the canonical public repository at a named commit, and that commit must be
reachable from the tip that GitHub reports for ``main`` (``git ls-remote`` of
the fixed canonical URL - neither the local ``origin`` configuration nor a
local remote-tracking ref is trusted). Every string in the request is then
checked for paths outside the repository, URLs other than the public
repository and every credential shape the shared scrubbers know.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from handlers.batch_job import JOB_ID, KINDS, MAX_PAYLOAD, job_message, llama_request, load_key
from modules.mailbox.config import MailboxConfig
from modules.redaction import contains_credential
from modules.run_store import DEFAULT_DB_PATH, init_db

BATCH_KINDS = frozenset({"chat_completion", "hunk_replay", "bakeoff_prompt"})
# Kinds a caller may submit directly. Hunk and bake-off jobs are built by the
# replay itself (``modules.home_gpu.replay``), which is also their checker.
GENERIC_KINDS = frozenset({"chat_completion", "review_draft", "code_draft"})
DEFAULT_DEADLINE_SECONDS = 6 * 3600
CANONICAL_REPO = "tire0011/agent-lab"
CANONICAL_URL = f"https://github.com/{CANONICAL_REPO}"
CANONICAL_REF = "refs/heads/main"
_CANONICAL_CACHE_SECONDS = 300.0
_SHA = re.compile(r"[0-9a-f]{40}")
TMP_SUFFIX = ".home-gpu-tmp"

_URL_CANDIDATES = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s<>\"'`]*"           # any scheme://
    r"|(?<![\w:/])//[^\s/<>\"'`][^\s<>\"'`]*"      # scheme-relative //host/...
    r"|\b(?:mailto|file|data|javascript|ssh|git|ftp):[^\s<>\"'`]*"
    r"|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+:[^\s<>\"'`]*"  # scp-style git@host:repo
    r"|\bwww\.[^\s<>\"'`]+"
    r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}(?::\d+)?/[^\s<>\"'`]*",  # bare host/path
    re.I,
)
_URL_TRAILING = ".,;:!?)]}"
_OUTSIDE_PATH = re.compile(
    r"(?<![\w.~/\\-])/[\w.~$-]"                    # POSIX absolute path
    r"|(?<![\w])[A-Za-z]:[\\/]"                     # drive-rooted Windows path
    r"|\\\\[\w.$-]"                                 # UNC path
    r"|(?<![\w\\])\\[A-Za-z][\w .$-]+\\[\w.$-]"     # rooted Windows path (\Users\x)
    r"|(?<![\w-])~[\w.-]*[/\\]"                     # home directory
    r"|\$\{?(?:HOME|USERPROFILE|TMPDIR|XDG_\w+)\}?"
    r"|%[A-Za-z_][\w]*%[\\/]"                       # %APPDATA%\...
    r"|(?:^|(?<=[\s\"'`(=,:;\[{]))\.\.(?:[/\\]|$)"  # ../ at a token start
    r"|[/\\]\.\.(?=[/\\\s\"'`)]|$)"                 # /../ inside a path
)


def enabled() -> bool:
    return os.environ.get("NEXUS_HOME_GPU") == "1"


class ProvenanceUnavailable(RuntimeError):
    """The canonical repository could not be asked; retry later, nothing is decided."""


def _git(repo: Path, *args: str, network: bool = False) -> bytes:
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(["git", "--no-replace-objects", *args], cwd=repo,
                                capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        if network:
            raise ProvenanceUnavailable("canonical repository unreachable") from exc
        raise ValueError("public repository provenance check failed") from exc
    if result.returncode:
        if network:
            raise ProvenanceUnavailable("canonical repository unreachable")
        raise ValueError("public repository provenance check failed")
    return result.stdout


_canonical_lock = threading.Lock()
_canonical_cache: tuple[float, str] | None = None


def _ls_remote_main(repo: Path) -> str:
    """The SHA GitHub reports for ``main`` of the canonical public repository."""
    out = _git(repo, "-c", "credential.helper=", "-c", "core.askPass=", "ls-remote",
               "--", CANONICAL_URL, CANONICAL_REF, network=True).decode("ascii", "replace")
    lines = [line.split("\t") for line in out.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != CANONICAL_REF \
            or not _SHA.fullmatch(lines[0][0]):
        raise ValueError("canonical public main could not be resolved")
    return lines[0][0]


def canonical_main(repo: Path) -> str:
    global _canonical_cache
    with _canonical_lock:
        now = time.monotonic()
        if _canonical_cache and now - _canonical_cache[0] < _CANONICAL_CACHE_SECONDS:
            return _canonical_cache[1]
        tip = _ls_remote_main(repo)
        _canonical_cache = (now, tip)
        return tip


def _reset_canonical_cache() -> None:
    global _canonical_cache
    with _canonical_lock:
        _canonical_cache = None


def public_commit(repo: Path, commit: str) -> None:
    """Accept only a commit that is published on the canonical public ``main``."""
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise ValueError("expected a full public commit SHA")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("provenance needs a full clone")
    tip = canonical_main(repo)
    try:
        _git(repo, "cat-file", "-e", f"{tip}^{{commit}}")
    except ValueError:
        # Objects only, no ref changes: the canonical tip is fetched by its SHA.
        _git(repo, "-c", "credential.helper=", "fetch", "-q", "--no-tags",
             "--no-write-fetch-head", "--", CANONICAL_URL, tip, network=True)
    if _git(repo, "cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("expected a public commit")
    _git(repo, "merge-base", "--is-ancestor", commit, tip)


def _url_allowed(raw: str) -> bool:
    url = urlsplit(raw if re.match(r"(?i)[a-z][a-z0-9+.-]*://", raw) else "https://" + raw.lstrip("/"))
    if raw.startswith("//") or url.scheme.lower() != "https" or url.netloc != "github.com":
        return False
    path = url.path.rstrip("/")
    if path != f"/{CANONICAL_REPO}" and not url.path.startswith(f"/{CANONICAL_REPO}/"):
        return False
    return not (url.query or re.search(r"%|\\|/\.\.?(?:/|$)", url.path))


def scan_text(text: str) -> None:
    """Raise for credentials, foreign URLs or paths outside the repository."""
    if "\x00" in text or contains_credential(text):
        raise ValueError("potential secret or binary content in batch input")
    remainder = text
    for match in _URL_CANDIDATES.finditer(text):
        raw = match.group().rstrip(_URL_TRAILING)
        if not _url_allowed(raw):
            raise ValueError("non-project URL in batch input")
        remainder = remainder.replace(match.group(), " ", 1)
    if _OUTSIDE_PATH.search(remainder):
        raise ValueError("external path in batch input")


def _scan(value: object) -> None:
    if isinstance(value, str):
        scan_text(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_text(key)
            _scan(item)
    elif isinstance(value, list):
        for item in value:
            _scan(item)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("unsupported value in batch input")


def safe_repo_path(path: object) -> str:
    if not isinstance(path, str) or not re.fullmatch(r"[\w./-]{1,300}", path):
        raise ValueError("invalid public source path")
    if (PurePosixPath(path).is_absolute()
            or any(part in ("", ".", "..", ".git") for part in path.split("/"))):
        raise ValueError("invalid public source path")
    return path


def public_blob(repo: Path, commit: str, path: str, *, max_bytes: int = MAX_PAYLOAD) -> str:
    """The text of a regular file in Git object *commit*, independent of the worktree."""
    path = safe_repo_path(path)
    spec = f"{commit}:{path}"
    tree = _git(repo, "ls-tree", commit, "--", path)
    if not tree.startswith((b"100644 blob ", b"100755 blob ")):
        raise ValueError("expected a regular public file")
    if int(_git(repo, "cat-file", "-s", spec)) > max_bytes:
        raise ValueError("public source exceeds batch size limit")
    return _git(repo, "cat-file", "blob", spec).decode("utf-8")


def submit_job(kind: str, payload: dict, deadline_s: float = DEFAULT_DEADLINE_SECONDS, *,
               config: MailboxConfig | None = None, db_path: str = DEFAULT_DB_PATH) -> str:
    """Queue a committed JSON request: ``payload = {"commit": SHA, "path": "x.json"}``."""
    if not enabled():
        raise ValueError("NEXUS_HOME_GPU=1 is required")
    if kind not in GENERIC_KINDS:
        raise ValueError("unsupported batch job kind; hunk and bake-off jobs come from the replay")
    if not isinstance(payload, dict) or payload.keys() != {"commit", "path"}:
        raise ValueError("provide a public commit and committed JSON request path")
    config = config or MailboxConfig.from_env()
    commit, path = payload["commit"], payload["path"]
    public_commit(config.code_repo_path, commit)
    request = json.loads(public_blob(config.code_repo_path, commit, path))
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    for name in request.get("files", []) if kind == "code_draft" else []:
        public_blob(config.code_repo_path, commit, name)
    return enqueue(kind, request, {"commit": commit, "path": path}, deadline_s, db_path=db_path)


def build_envelope(kind: str, request: dict, source: dict) -> tuple[str, str]:
    """The validated wire body and its SHA-256; raises for anything not allowed."""
    if kind not in KINDS:
        raise ValueError("unsupported batch job kind")
    llama_request(kind, request)
    if "expected_diff_refs" in request and (
        not isinstance(request["expected_diff_refs"], list)
        or not all(isinstance(ref, str) and _SHA.fullmatch(ref) for ref in request["expected_diff_refs"])
    ):
        raise ValueError("invalid expected diff references")
    for name in request.get("files", []):
        safe_repo_path(name)
    envelope = {"source": source, "request": request}
    _scan(envelope)
    body = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                      sort_keys=True)
    raw = body.encode("utf-8")
    # The front matter adds well under 1 KiB; the whole inbox file stays below 64 KiB.
    if len(raw) > MAX_PAYLOAD - 1024:
        raise ValueError("batch payload exceeds 64 KiB")
    return body, hashlib.sha256(raw).hexdigest()


def enqueue(kind: str, request: dict, source: dict, deadline_s: float, *, db_path: str,
            job_id: str | None = None, expected_model: str | None = None,
            replay_key: str | None = None) -> str:
    """Store one job durably. Running it twice with the same ``job_id`` is a no-op."""
    if not enabled():
        raise ValueError("NEXUS_HOME_GPU=1 is required")
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float)) \
            or not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("deadline must be positive and finite")
    load_key()  # a job that could never be signed is not queued
    body, digest = build_envelope(kind, request, source)
    job_id = job_id or uuid.uuid4().hex
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("invalid job id")
    now = time.time()
    with closing(init_db(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO home_gpu_jobs "
            "(job_id, kind, status, created_at, deadline, input_sha256, payload, expected_model, replay_key) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (job_id, kind, now, now + float(deadline_s), digest, body, expected_model, replay_key),
        )
        stored = conn.execute(
            "SELECT input_sha256 FROM home_gpu_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if stored[0] != digest:
            raise ValueError("batch job ID already refers to different input")
    return job_id


def inbox_path(config: MailboxConfig, job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("invalid home GPU job id")
    return config.inbox_dir / f"{job_id}.md"


def temp_path(config: MailboxConfig, job_id: str) -> Path:
    return config.inbox_dir / f".{job_id}.md{TMP_SUFFIX}"


def write_inbox(config: MailboxConfig, row: tuple, key: bytes) -> Path:
    """Write one signed inbox file; on any failure no temporary file is left behind."""
    job_id, kind, deadline, digest, body = row
    path = inbox_path(config, job_id)
    if config.inbox_dir.is_symlink() or path.is_symlink():
        raise ValueError("invalid home GPU inbox path")
    text = job_message(key, job_id=job_id, kind=kind, deadline=deadline, body=body)
    if f"input_sha256: {digest}\n" not in text:
        raise ValueError("stored payload does not match its digest")
    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(config, job_id)
    tmp.unlink(missing_ok=True)  # left by an interrupted earlier attempt
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


_LIST_COLUMNS = (
    "job_id, kind, status, created_at, deadline, published_at, answered_at, answer_host, "
    "finished_at, model, prompt_tokens, completion_tokens, seconds, tokens_per_second, "
    "answered_at - created_at AS wall_seconds, check_status, checked_at, error, publish_error"
)


def list_jobs(limit: int = 50, *, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Job metadata only - never the payload or the answer text."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    with closing(init_db(db_path)) as conn:
        cursor = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM home_gpu_jobs "
            "ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,),
        )
        return [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]
