"""Publish the durable spool and collect untrusted replies under the mailbox lock."""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from handlers.batch_job import JOB_ID, MAX_PAYLOAD
from modules.home_gpu.jobs import BATCH_KINDS, write_inbox
from modules.mailbox.config import MailboxConfig
from modules.mailbox.core import _has_secret, parse_front_matter
from modules.run_store import init_db

logger = logging.getLogger(__name__)


def publish_pending(config: MailboxConfig, db_path: str, mailbox_module: ModuleType) -> set[str]:
    """Commit only known batch inbox files. Existing mailbox sync pushes them."""
    with closing(init_db(db_path)) as conn:
        rows = conn.execute(
            "SELECT job_id, kind, deadline, input_sha256, payload FROM home_gpu_jobs "
            "WHERE status='queued' AND published_at IS NULL",
        ).fetchall()
    if not rows:
        return set()
    by_path = {f"inbox/{row[0]}.md": row for row in rows}
    status = mailbox_module._git(["status", "--porcelain", "--untracked-files=all"],
                                 config.mailbox_repo_path)
    prior = set()
    for entry in status.stdout.splitlines():
        if entry.startswith("?? outbox/"):
            continue
        if entry.startswith(("?? inbox/", "A  inbox/")) and entry[3:] in by_path:
            prior.add(entry[3:])
            continue
        logger.warning("Home GPU publication deferred by mailbox worktree changes")
        return set()
    paths = []
    for name, row in by_path.items():
        tracked = mailbox_module._git(
            ["ls-files", "--error-unmatch", "--", name], config.mailbox_repo_path, check=False,
        ).returncode == 0
        # A staged file can remain after a failed commit. Regenerate it from the spool.
        if not tracked or name in prior:
            paths.append(write_inbox(config, row))
    if paths and not mailbox_module.git_add_and_commit(
        config.mailbox_repo_path, paths, "Publish Nexus home GPU batch jobs",
    ):
        return set()
    return {row[0] for row in rows}


def _reply(path: Path, job_id: str, digest: str, expected_model: str | None) -> tuple:
    if path.is_symlink() or not path.is_file():
        raise ValueError("reply must be a regular file")
    with path.open("rb") as fh:
        raw = fh.read(MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("reply exceeds size limit")
    text = raw.decode("utf-8")
    meta, _ = parse_front_matter(text)
    header_end = text.find("\n---", 3)
    answer = text[header_end + 4:].removeprefix("\n").removesuffix("\n")
    if meta.get("in_reply_to") != job_id or meta.get("input_sha256") != digest:
        raise ValueError("reply does not match job or input digest")
    model = meta["model"]
    if (not model or len(model) > 200 or any(not 32 <= ord(char) < 127 for char in model)
            or (expected_model and expected_model != model)):
        raise ValueError("invalid reply model")
    counts = tuple(int(meta[key]) for key in ("prompt_tokens", "completion_tokens"))
    if any(not 0 <= count <= 1000000 for count in counts):
        raise ValueError("invalid reply token counts")
    seconds = float(meta["seconds"])
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError("invalid reply duration")
    rate = float(meta.get("tokens_per_second", counts[1] / seconds))
    if not math.isfinite(rate) or rate < 0 or not answer.strip() or "\x00" in text or _has_secret(text):
        raise ValueError("invalid reply content")
    return model, *counts, seconds, rate, answer


def collect(config: MailboxConfig, db_path: str, *, published: set[str] | None = None,
            now: float | None = None) -> None:
    """Resolve each queued job once. The replay path consumes answers from SQLite."""
    now = time.time() if now is None else now
    with closing(init_db(db_path)) as conn, conn:
        for job_id in published or ():
            conn.execute("UPDATE home_gpu_jobs SET published_at=? WHERE job_id=? AND published_at IS NULL",
                         (now, job_id))
        rows = conn.execute(
            "SELECT job_id, kind, deadline, input_sha256, expected_model, status FROM home_gpu_jobs "
            "WHERE status='queued' OR (status IN ('fallback', 'expired') AND reply_seen_at IS NULL AND deadline > ?)", (now,),
        ).fetchall()
        for job_id, _, deadline, digest, model, status in rows:
            if now >= deadline or not JOB_ID.fullmatch(job_id):
                continue
            path = config.outbox_dir / f"{job_id}.reply.md"
            if config.outbox_dir.is_symlink() or not path.exists():
                continue
            try:
                result = _reply(path, job_id, digest, model)
            except (OSError, ValueError, KeyError):
                logger.warning("Ignored invalid home GPU reply for job %s", job_id)
                continue
            if status != "queued":
                # A job already handed off or dropped can still confirm PC liveness.
                conn.execute("UPDATE home_gpu_jobs SET reply_seen_at=? WHERE job_id=?", (now, job_id))
                continue
            conn.execute(
                "UPDATE home_gpu_jobs SET status='answered', finished_at=?, reply_seen_at=?, model=?, "
                "prompt_tokens=?, completion_tokens=?, seconds=?, tokens_per_second=?, answer=? "
                "WHERE job_id=? AND status='queued'", (now, now, *result, job_id),
            )
        last_answer, first_published = conn.execute(
            "SELECT MAX(reply_seen_at), MIN(published_at) "
            "FROM home_gpu_jobs",
        ).fetchone()
        contact = last_answer if last_answer is not None else first_published
        offline = contact is not None and now - contact >= 86400
        for job_id, kind, deadline, _, _, status in rows:
            if status != "queued" or (now < deadline and not offline):
                continue
            status = "fallback" if kind in BATCH_KINDS else "expired"
            reason = "home_gpu_offline" if now < deadline and status == "fallback" else "home_gpu_expired"
            conn.execute(
                "UPDATE home_gpu_jobs SET status=?, finished_at=?, error=? "
                "WHERE job_id=? AND status='queued'", (status, now, reason, job_id),
            )


def night_window(now: float | None = None) -> bool:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).hour < 6


def get_job(job_id: str, *, db_path: str) -> dict | None:
    with closing(init_db(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM home_gpu_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
