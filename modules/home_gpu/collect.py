"""Collect completed home-GPU replies from the shared mailbox."""

from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
from pathlib import Path

from modules.mailbox.core import _atomic_write, _now_iso, parse_front_matter

MAX_ANSWER_BYTES = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
_RESULT_PREFIX = b"\n## Result\n\n"
logger = logging.getLogger(__name__)


def _ignore(db: sqlite3.Connection, reason: str, key: str) -> None:
    with db:
        inserted = db.execute(
            "INSERT OR IGNORE INTO home_gpu_ignored_replies VALUES (?, ?)",
            (reason, key),
        ).rowcount
    if inserted:
        logger.warning("Ignoring home-GPU reply (%s)", reason)


def _metrics(meta: dict[str, str]) -> tuple[int, int, float]:
    prompt = int(meta["prompt_tokens"])
    completion = int(meta["completion_tokens"])
    seconds = float(meta["seconds"])
    if not all(0 <= count < 2**63 for count in (prompt, completion)):
        raise ValueError("invalid token counts")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid duration")
    return prompt, completion, seconds


def collect_replies(
    mailbox_dir: Path | str,
    db: sqlite3.Connection,
    *,
    state_dir: Path | str | None = None,
) -> int:
    """Collect replies using a dedicated run-store connection and return the count."""
    if state_dir is None:
        db_file = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
        if not db_file:
            raise ValueError("state_dir is required for an in-memory run store")
        state_dir = Path(db_file).parent
    answers = Path(state_dir).resolve() / "home_gpu_answers"
    collected = 0
    for path in sorted((Path(mailbox_dir) / "outbox").glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as reply:
                header = reply.read(MAX_HEADER_BYTES)
                if not header.startswith(b"---\n"):
                    continue
                front, delimiter, _ = header.partition(b"\n---\n")
                if not delimiter:
                    if b"\nreply_to:" in header:
                        _ignore(db, "malformed front matter", path.name)
                    continue
                if not any(line.startswith(b"reply_to:") for line in front.splitlines()):
                    continue
                meta, _ = parse_front_matter((front + delimiter).decode("utf-8"))
                job_id = meta["reply_to"].removesuffix(".md")
                if not job_id or not meta.get("status"):
                    raise ValueError("missing reply metadata")
                # The mailbox client also emits retry and failure messages.
                if meta["status"] not in {"done", "ok", "success"}:
                    continue
                if not meta.get("model"):
                    raise ValueError("missing model")
                prompt, completion, seconds = _metrics(meta)
                reply.seek(len(front) + len(delimiter))
                body = reply.read(MAX_ANSWER_BYTES + len(_RESULT_PREFIX) + 2)
            if body.startswith(_RESULT_PREFIX):
                body = body[len(_RESULT_PREFIX):].removesuffix(b"\n")
            if len(body) > MAX_ANSWER_BYTES:
                _ignore(db, "oversized answer", path.name)
                continue
            answer = body.decode("utf-8")
        except (KeyError, ValueError):
            _ignore(db, "malformed reply", path.name)
            continue
        except OSError:
            logger.warning("Could not read home-GPU reply")
            continue

        digest = hashlib.sha256(body).hexdigest()
        # Use a digest for the filename so no external identifier becomes a path.
        answer_path = answers / f"{digest}.md"
        with db:
            updated = db.execute(
                """UPDATE home_gpu_jobs
                   SET status = 'answered', answered_at = ?, model = ?, prompt_tokens = ?,
                       completion_tokens = ?, seconds = ?, answer_sha256 = ?, answer_path = ?, host = ?
                   WHERE job_id = ? AND status = 'queued'""",
                (_now_iso(), meta["model"], prompt, completion, seconds, digest,
                 str(answer_path), meta.get("host"), job_id),
            ).rowcount
            if updated:
                answers.mkdir(parents=True, exist_ok=True)
                _atomic_write(answer_path, answer)
        if updated:
            collected += 1
            continue
        known = db.execute("SELECT 1 FROM home_gpu_jobs WHERE job_id = ?", (job_id,)).fetchone()
        _ignore(db, "job is no longer queued" if known else "unknown job", job_id)
    return collected
