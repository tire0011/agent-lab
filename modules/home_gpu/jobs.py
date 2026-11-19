"""Submit public-repository inputs to the home GPU mailbox lane."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from handlers.batch_job import JOB_ID, KINDS, MAX_PAYLOAD, llama_request
from modules.mailbox.config import MailboxConfig
from modules.mailbox.core import MailboxLock, _atomic_write, _has_secret
from modules.redaction import _SECRET_PATTERN
from modules.run_store import DEFAULT_DB_PATH, init_db

BATCH_KINDS = frozenset({"chat_completion", "hunk_replay", "bakeoff_prompt"})
PUBLIC_REMOTE = "https://github.com/tire0011/agent-lab"
_SHA = re.compile(r"[0-9a-f]{40}")
_URL = re.compile(
    r"[\w+.-]+://[^\s<>\"'`]+|(?:mailto:|git@|www\.)[^\s<>\"'`]+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|edu|gov|example|internal)/[^\s<>\"'`]*",
    re.I,
)
_OUTSIDE_PATH = re.compile(
    r"(?<![\w./-])(?:[A-Za-z]:[\\/]|~[/\\]|/{1,2}[\w.]|\\\\[\w.]|\.\.[/\\])"
    r"|(?:[/\\])\.\.(?:[/\\]|\b)"
)


def enabled() -> bool:
    return os.environ.get("NEXUS_HOME_GPU") == "1"


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "--no-replace-objects", *args], cwd=repo,
                            capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError("public repository provenance check failed")
    return result.stdout


def public_commit(repo: Path, commit: str) -> None:
    """Accept only commits on the canonical public remote-tracking main."""
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise ValueError("expected a full public commit SHA")
    origin = _git(repo, "config", "--get", "remote.origin.url").decode().strip()
    if origin not in {PUBLIC_REMOTE, PUBLIC_REMOTE + ".git",
                      "git@github.com:tire0011/agent-lab.git",
                      "ssh://git@github.com/tire0011/agent-lab.git"}:
        raise ValueError("public upstream is required")
    _git(repo, "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main")
    if _git(repo, "cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("expected a public commit")


def scan_text(text: str) -> None:
    if "\x00" in text or _has_secret(text) or _SECRET_PATTERN.search(text):
        raise ValueError("potential secret or binary content in batch input")
    for match in _URL.finditer(text):
        raw = match.group()
        url = urlsplit(raw if "://" in raw else "https://" + raw)
        if (url.scheme != "https" or url.netloc != "github.com"
                or not (url.path.rstrip("/") == "/tire0011/agent-lab"
                        or url.path.startswith("/tire0011/agent-lab/"))):
            raise ValueError("non-project URL in batch input")
        if re.search(r"(?i)(?:%|/\.\.(?:/|$)|\\)", url.path) or url.query:
            raise ValueError("ambiguous project URL in batch input")
    if _OUTSIDE_PATH.search(_URL.sub("", text)):
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


def public_blob(repo: Path, commit: str, path: str, *, max_bytes: int = MAX_PAYLOAD) -> str:
    if not isinstance(path, str) or not re.fullmatch(r"[\w./-]+", path):
        raise ValueError("invalid public source path")
    if (PurePosixPath(path).is_absolute()
            or any(part in ("", ".", "..", ".git") for part in path.split("/"))):
        raise ValueError("invalid public source path")
    spec = f"{commit}:{path}"
    # Read the Git object, independent of worktree edits and symlinks.
    tree = _git(repo, "ls-tree", commit, "--", path)
    if not tree.startswith((b"100644 blob ", b"100755 blob ")):
        raise ValueError("expected a regular public file")
    if int(_git(repo, "cat-file", "-s", spec)) > max_bytes:
        raise ValueError("public source exceeds batch size limit")
    return _git(repo, "cat-file", "blob", spec).decode("utf-8")


def submit_job(kind: str, payload: dict, deadline_s: float = 21600, *,
               config: MailboxConfig | None = None, db_path: str = DEFAULT_DB_PATH) -> str:
    """Submit a request identified by an immutable public commit and JSON file."""
    if not enabled():
        raise ValueError("NEXUS_HOME_GPU=1 is required")
    if kind not in KINDS:
        raise ValueError("unsupported batch job kind")
    if not isinstance(payload, dict) or payload.keys() != {"commit", "path"}:
        raise ValueError("provide a public commit and committed JSON request path")
    config = config or MailboxConfig.from_env()
    commit, path = payload["commit"], payload["path"]
    public_commit(config.code_repo_path, commit)
    request = json.loads(public_blob(config.code_repo_path, commit, path))
    if kind == "hunk_replay" and isinstance(request, dict):
        refs = request.get("expected_diff_refs")
        if isinstance(refs, list):
            for ref in refs:
                public_commit(config.code_repo_path, ref)
    return _enqueue(kind, request, payload, deadline_s, config=config, db_path=db_path)


def _enqueue(kind: str, request: dict, source: dict, deadline_s: float, *,
             config: MailboxConfig, db_path: str, job_id: str | None = None,
             expected_model: str | None = None, replay_key: str | None = None) -> str:
    if not enabled() or kind not in KINDS:
        raise ValueError("unsupported or disabled batch job")
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("deadline must be positive and finite")
    llama_request(kind, request)
    if "expected_diff_refs" in request and (
        kind != "hunk_replay" or not isinstance(request["expected_diff_refs"], list)
        or not all(isinstance(ref, str) and _SHA.fullmatch(ref)
                   for ref in request["expected_diff_refs"])
    ):
        raise ValueError("invalid expected diff references")
    envelope = {"source": source, "request": request}
    _scan(envelope)
    body = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    raw = body.encode("utf-8")
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("batch payload exceeds 64 KiB")
    digest = hashlib.sha256(raw).hexdigest()
    job_id = job_id or uuid.uuid4().hex
    now = time.time()
    deadline = now + deadline_s
    with closing(init_db(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO home_gpu_jobs "
            "(job_id, kind, status, created_at, deadline, input_sha256, payload, expected_model, replay_key) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (job_id, kind, now, deadline, digest, body, expected_model, replay_key),
        )
        stored = conn.execute(
            "SELECT input_sha256 FROM home_gpu_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        if stored[0] != digest:
            raise ValueError("batch job ID already refers to different input")
    # The poll owns publication and can recover an unwritten file from SQLite.
    lock = MailboxLock(Path(os.getenv("AGENT_MAILBOX_LOCK_PATH", "/var/lib/nexus/mailbox.lock")))
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock.acquire():
        try:
            with closing(init_db(db_path)) as conn:
                row = conn.execute(
                    "SELECT job_id, kind, deadline, input_sha256, payload FROM home_gpu_jobs "
                    "WHERE job_id=? AND status='queued' AND published_at IS NULL",
                    (job_id,),
                ).fetchone()
            if row:
                write_inbox(config, row)
        finally:
            lock.release()
    return job_id


def write_inbox(config: MailboxConfig, row: tuple) -> Path:
    job_id, kind, deadline, digest, body = row
    if not JOB_ID.fullmatch(job_id) or config.inbox_dir.is_symlink():
        raise ValueError("invalid home GPU inbox path")
    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    path = config.inbox_dir / f"{job_id}.md"
    _atomic_write(path, f"---\ntype: batch_job\nto: home-gpu\ncreated_by: nexus\n"
                       f"job_id: {job_id}\nkind: {kind}\ndeadline: {deadline}\n"
                       f"input_sha256: {digest}\n---\n{body}\n")
    return path


def list_jobs(limit: int = 50, *, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    with closing(init_db(db_path)) as conn:
        cursor = conn.execute(
            "SELECT job_id, kind, status, created_at, deadline, finished_at, model, "
            "prompt_tokens, completion_tokens, seconds, tokens_per_second, "
            "finished_at - created_at AS wall_seconds, error "
            "FROM home_gpu_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,),
        )
        return [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]
