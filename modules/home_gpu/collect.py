"""Collect home-GPU replies from the shared mailbox outbox (#813, part 2b).

The home GPU's worker answers a ``batch_job`` (#759) by writing a reply file
into the mailbox outbox in the ``mailbox_client`` reply format::

    ---
    reply_to: <job_id>
    status: done
    model: <model>
    prompt_tokens: <int>
    completion_tokens: <int>
    seconds: <float>
    ---
    <answer text>

``reply_to`` carries the job id; ``in_reply_to`` is not used. ``collect_replies``
matches each reply to its ``queued`` ``home_gpu_jobs`` row, writes the answer
atomically to ``<state_dir>/home_gpu/<job_id>.txt`` (temp file plus rename)
*before* the row is updated, and then flips the row to ``answered`` with the
answer hash, path and the reply's numbers in one transaction.

Unknown job ids, a repeated reply for an already answered row and answers over
256 KB are ignored; each such file is logged once, and the set of seen files is
kept in the ``service_settings`` table so a later copy of the same file is
skipped silently.

Failure handling across the file/transaction boundary: the answer file is
written before the row update, so a crash or a failed transaction can leave a
file on disk that no row represents. A retry safely *replaces* such an orphan
(the write is an atomic tmp-file-plus-rename), and when a transaction fails
after the file landed, ``_reconcile_orphan`` removes the orphan again - but
only when the row does not reference the path and the file content is exactly
the bytes this collector wrote, so a concurrent winner's answer is never
deleted. All database work runs inside savepoints (``_tx``), and schema
initialization never commits, so a caller's open transaction on the shared
connection is preserved end to end.

Everything here is off unless the tick calls it, and the tick only calls it
when ``NEXUS_HOME_GPU=1`` (``modules.home_gpu.jobs.home_gpu_enabled``).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from modules.mailbox import core as mailbox_core

logger = logging.getLogger(__name__)

#: Answers larger than this are ignored (the 300 KB case in #813).
MAX_ANSWER_BYTES = 256 * 1024

#: Upper bound for the seen-file set kept in ``service_settings``; the oldest
#: names are dropped so a long-lived database cannot grow it without limit.
MAX_SEEN_FILES = 4096

#: ``service_settings`` key holding the JSON list of already handled reply files.
SEEN_FILES_SETTING = "home_gpu.collect.seen_reply_files"

#: Job ids are generated as ``hg-<timestamp>-<hex>`` (#759); anything else in
#: ``reply_to`` cannot name a row and must never become a file name.
_JOB_ID = re.compile(r"[A-Za-z0-9-]+\Z")

# ``CREATE TABLE IF NOT EXISTS`` mirrors the #812 migration so this module also
# works against a database whose schema predates it; the columns are identical.
# The statements are kept as individual strings on purpose: ``executescript``
# would COMMIT any pending transaction first, which would commit unrelated
# caller work on the shared connection, so it is never used here.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS service_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS home_gpu_jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        submitted_at TEXT NOT NULL,
        deadline TEXT NOT NULL,
        answered_at TEXT NULL,
        model TEXT NULL,
        prompt_tokens INTEGER NULL,
        completion_tokens INTEGER NULL,
        seconds REAL NULL,
        input_sha256 TEXT NOT NULL,
        answer_sha256 TEXT NULL,
        answer_path TEXT NULL,
        host TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_home_gpu_jobs_status ON home_gpu_jobs(status)",
    """
    CREATE INDEX IF NOT EXISTS idx_home_gpu_jobs_submitted_at
        ON home_gpu_jobs(submitted_at DESC)
    """,
)

#: Every schema object this module needs; all four present means "skip DDL".
_EXPECTED_SCHEMA_OBJECTS = (
    "service_settings",
    "home_gpu_jobs",
    "idx_home_gpu_jobs_status",
    "idx_home_gpu_jobs_submitted_at",
)


class _NotClaimed(Exception):
    """The guarded UPDATE did not claim the row (answered concurrently)."""


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Create the home-GPU tables only when they are missing.

    The ``sqlite_master`` probe is a plain read: it neither starts nor commits
    a transaction, so calling this on every collection is free and never
    disturbs a pending transaction on the caller's connection. Only when an
    object is missing are the ``CREATE IF NOT EXISTS`` statements executed, as
    ordinary statements: with no transaction open they autocommit on their own,
    with a caller's transaction open they join it and are left for the caller
    to commit or roll back. This function never commits anything itself.
    """
    placeholders = ",".join("?" for _ in _EXPECTED_SCHEMA_OBJECTS)
    found = db.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE name IN ({placeholders})",
        _EXPECTED_SCHEMA_OBJECTS,
    ).fetchone()[0]
    if found == len(_EXPECTED_SCHEMA_OBJECTS):
        return
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


_SAVEPOINT_SEQ = itertools.count(1)


@contextmanager
def _tx(db: sqlite3.Connection, name: str | None = None) -> Iterator[None]:
    """One unit of database work that never commits a caller's transaction.

    A SAVEPOINT is used instead of ``with db:``: with no transaction open the
    RELEASE commits exactly this unit; with a caller's transaction already
    open the savepoint nests inside it, so RELEASE / ROLLBACK TO touch only
    this unit and the caller's transaction stays open and uncommitted. The
    savepoint name is unique per call, so units that nest (the fallback
    wrapper runs inside its own savepoint) each roll back exactly their own
    work.
    """
    point = name or f"home_gpu_{next(_SAVEPOINT_SEQ)}"
    db.execute(f"SAVEPOINT {point}")
    try:
        yield
    except BaseException:
        db.execute(f"ROLLBACK TO {point}")
        db.execute(f"RELEASE {point}")
        raise
    db.execute(f"RELEASE {point}")


def _load_seen(db: sqlite3.Connection) -> list[str]:
    """The seen reply file names, oldest first."""
    row = db.execute(
        "SELECT value FROM service_settings WHERE key = ?", (SEEN_FILES_SETTING,)
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _save_seen(db: sqlite3.Connection, seen: list[str]) -> None:
    """Persist the seen set; must run inside the caller's transaction."""
    db.execute(
        """
        INSERT INTO service_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (SEEN_FILES_SETTING, json.dumps(seen[-MAX_SEEN_FILES:]), mailbox_core._now_iso()),
    )


def _ignore(
    db: sqlite3.Connection,
    seen: list[str],
    seen_set: set[str],
    name: str,
    reason: str,
    job_id: str,
) -> None:
    """Log one warning for *name* (it is not in the seen set yet) and mark it
    seen in its own transaction, so the warning is never repeated."""
    logger.warning(
        "Ignoring home-GPU reply file=%s reason=%s job_id=%s", name, reason, job_id or "-"
    )
    seen.append(name)
    seen_set.add(name)
    with _tx(db):
        _save_seen(db, seen)


def _reconcile_orphan(
    db: sqlite3.Connection, answer_path: Path, digest: str, job_id: str
) -> None:
    """Remove an answer file whose row update did not commit.

    The answer file is written before the row is updated, so a failed or
    interrupted transaction can leave a file on disk that no row represents.
    The file is removed only when the row does *not* reference it (still
    queued, missing, or answered with a different path) *and* the file's
    content hashes to *digest* - i.e. it is exactly the bytes this collector
    wrote. A concurrent winner's answer (row answered with this path, or a
    file with different content) is never deleted. A retry after a plain
    crash needs no cleanup here: the atomic tmp-plus-rename write safely
    replaces the orphan on the next attempt.
    """
    try:
        row = db.execute(
            "SELECT status, answer_path FROM home_gpu_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row[0] == "answered" and row[1] == str(answer_path):
        return
    try:
        data = answer_path.read_bytes()
    except OSError:
        return
    if hashlib.sha256(data).hexdigest() != digest:
        return
    try:
        answer_path.unlink()
        logger.info("Removed orphaned answer file=%s job_id=%s", answer_path, job_id)
    except OSError:
        logger.warning(
            "Could not remove orphaned answer file=%s job_id=%s", answer_path, job_id
        )


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset); ``None`` when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _metrics(meta: dict[str, str]) -> tuple[int, int, float]:
    """Parse and validate ``prompt_tokens`` / ``completion_tokens`` / ``seconds``."""
    try:
        prompt = int(meta["prompt_tokens"])
        completion = int(meta["completion_tokens"])
        seconds = float(meta["seconds"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("missing or non-numeric reply metrics") from None
    if prompt < 0 or completion < 0:
        raise ValueError("negative token count")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid duration")
    return prompt, completion, seconds


def collect_replies(
    mailbox_dir: Path | str,
    db: sqlite3.Connection,
    state_dir: Path | str,
) -> int:
    """Match outbox reply files to queued ``home_gpu_jobs`` rows and collect them.

    ``mailbox_dir`` is the mailbox working copy (replies are read from its
    ``outbox/``), ``db`` the run-store connection and ``state_dir`` the state
    root whose ``home_gpu/<job_id>.txt`` receives each answer. Returns the number
    of newly answered jobs. Files already in the seen set (kept in the
    ``service_settings`` table) are skipped silently, which makes repeated runs
    over the same outbox idempotent.

    ``db`` is never committed wholesale: each unit of work runs in a savepoint
    and schema setup never commits, so a transaction the caller already has
    open on the shared connection is preserved. If the row update fails after
    the answer file was written, the orphaned file is removed again before the
    error propagates; the next run retries the file from scratch.
    """
    _ensure_schema(db)
    outbox = Path(mailbox_dir) / "outbox"
    if not outbox.is_dir():
        return 0

    seen = _load_seen(db)
    seen_set = set(seen)
    answers_dir = Path(state_dir) / "home_gpu"
    collected = 0

    for path in sorted(outbox.iterdir()):
        if path.name in seen_set:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _ignore(db, seen, seen_set, path.name, f"unreadable ({type(exc).__name__})", "")
            continue
        try:
            meta, body = mailbox_core.parse_front_matter(text)
        except ValueError:
            # No parseable front matter: not a mailbox_client reply (Nexus' own
            # outbox entries land here too) - skip without logging.
            continue

        job_id = (meta.get("reply_to") or "").strip()
        if not job_id:
            # ``in_reply_to`` is not used: a reply without ``reply_to`` is not ours.
            continue
        if not _JOB_ID.fullmatch(job_id):
            _ignore(db, seen, seen_set, path.name, "unknown job id", job_id)
            continue
        try:
            if not meta.get("status") or not meta.get("model"):
                raise ValueError("missing status or model")
            prompt, completion, seconds = _metrics(meta)
        except ValueError as exc:
            _ignore(db, seen, seen_set, path.name, f"malformed reply ({exc})", job_id)
            continue

        answer = body
        answer_bytes = answer.encode("utf-8")
        if len(answer_bytes) > MAX_ANSWER_BYTES:
            _ignore(db, seen, seen_set, path.name, "answer over 256 KB", job_id)
            continue

        row = db.execute(
            "SELECT status, deadline FROM home_gpu_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            _ignore(db, seen, seen_set, path.name, "unknown job", job_id)
            continue
        if row[0] != "queued":
            _ignore(db, seen, seen_set, path.name, "job already answered", job_id)
            continue
        # A reply for a job whose deadline has passed is never claimed: the row
        # becomes ``expired`` here (the poll also expires it, this covers a
        # reply that lands in the same window) and the reply is ignored.
        deadline = _parse_iso(row[1])
        if deadline is not None and deadline <= datetime.now(timezone.utc):
            with _tx(db):
                db.execute(
                    "UPDATE home_gpu_jobs SET status = 'expired' "
                    "WHERE job_id = ? AND status = 'queued'",
                    (job_id,),
                )
            _ignore(db, seen, seen_set, path.name, "deadline passed", job_id)
            logger.info(
                "Expired home-GPU job at reply collection job_id=%s deadline=%s",
                job_id,
                row[1],
            )
            continue

        digest = hashlib.sha256(answer_bytes).hexdigest()
        answer_path = answers_dir / f"{job_id}.txt"
        written = False
        try:
            # The answer file is written (atomically: temp file plus rename)
            # before the row is updated; both the UPDATE and the seen-mark
            # commit in this one savepoint, so a crash either leaves the row
            # queued for the next tick or fully answered with its file present.
            # The savepoint (not ``with db:``) keeps a caller's open
            # transaction on the shared connection uncommitted.
            with _tx(db):
                answers_dir.mkdir(parents=True, exist_ok=True)
                # A leftover orphan from an interrupted earlier attempt is
                # safely replaced here by the rename.
                mailbox_core._atomic_write(answer_path, answer)
                written = True
                updated = db.execute(
                    """
                    UPDATE home_gpu_jobs
                    SET status = 'answered',
                        answered_at = ?,
                        model = ?,
                        prompt_tokens = ?,
                        completion_tokens = ?,
                        seconds = ?,
                        answer_sha256 = ?,
                        answer_path = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (
                        mailbox_core._now_iso(),
                        meta["model"],
                        prompt,
                        completion,
                        seconds,
                        digest,
                        str(answer_path),
                        job_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise _NotClaimed(job_id)
                seen.append(path.name)
                seen_set.add(path.name)
                _save_seen(db, seen)
        except _NotClaimed:
            if written:
                _reconcile_orphan(db, answer_path, digest, job_id)
            _ignore(db, seen, seen_set, path.name, "job already answered", job_id)
            continue
        except BaseException:
            # The transaction rolled back. If the answer file already landed
            # on disk it is now orphaned (no row represents it): remove it so
            # a later, differently failing retry can never leave a file whose
            # contents are not represented by the row. The file is not marked
            # seen, so the next tick retries it.
            if written:
                _reconcile_orphan(db, answer_path, digest, job_id)
            raise
        collected += 1
        logger.info(
            "Collected home-GPU reply file=%s job_id=%s model=%s bytes=%d",
            path.name,
            job_id,
            meta["model"],
            len(answer_bytes),
        )

    return collected


__all__ = ["MAX_ANSWER_BYTES", "MAX_SEEN_FILES", "SEEN_FILES_SETTING", "collect_replies"]
