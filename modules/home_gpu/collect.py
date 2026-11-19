"""Publish the durable spool, collect signed replies and keep deadlines (#743).

Order inside one mailbox poll (``modules.mailbox.poll_runner``):

1. ``cleanup_spool_artifacts`` and ``advance_deadlines`` - local only, so a
   mailbox outage can never keep a job queued past its deadline;
2. ``publish_pending`` - only jobs that are still queued and well inside their
   deadline are written and committed;
3. the regular mailbox sync;
4. ``collect`` - replies are read from the synced commit (``HEAD``), never
   from the worktree, and must carry a valid signature;
5. ``run_checks`` - each answered generic job goes to its checker.

Statuses: ``queued`` (waiting for the PC), ``answered`` (the PC answered),
``fallback`` (the VM may answer it), ``expired`` (dropped). Inference and
checking are recorded separately: ``answered_at``/``answer_host`` say who
produced the text, ``checked_at``/``check_status`` what the checker said.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from handlers.batch_job import (
    JOB_ID, MAX_PAYLOAD, SigningKeyError, load_key, split_message, verify_reply,
)
from modules.home_gpu.jobs import BATCH_KINDS, TMP_SUFFIX, inbox_path, write_inbox
from modules.mailbox.config import MailboxConfig
from modules.redaction import contains_credential
from modules.run_store import init_db

logger = logging.getLogger(__name__)
OFFLINE_AFTER_SECONDS = 24 * 3600
# A job this close to its deadline is not published any more: the PC could not
# answer it in time and the VM would have to redo it anyway.
PUBLISH_MARGIN_SECONDS = 300
LATE_REPLY_WINDOW_SECONDS = 7 * 24 * 3600


def night_window(now: float | None = None) -> bool:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).hour < 6


def get_job(job_id: str, *, db_path: str) -> dict | None:
    with closing(init_db(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM home_gpu_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


# ---- 1. local housekeeping ---------------------------------------------------

def cleanup_spool_artifacts(config: MailboxConfig) -> list[Path]:
    """Remove temporary inbox files an interrupted write left behind.

    Only names this lane creates (``.<job_id>.md.home-gpu-tmp``) are touched.
    """
    removed: list[Path] = []
    inbox = config.inbox_dir
    if not inbox.is_dir() or inbox.is_symlink():
        return removed
    for path in inbox.glob(f".*.md{TMP_SUFFIX}"):
        job_id = path.name[1:-len(f".md{TMP_SUFFIX}")]
        if JOB_ID.fullmatch(job_id) and not path.is_dir():
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def advance_deadlines(db_path: str, now: float | None = None) -> int:
    """Move overdue or offline jobs to fallback/expired. Needs no mailbox access."""
    now = time.time() if now is None else now
    changed = 0
    with closing(init_db(db_path)) as conn, conn:
        last_reply, first_published = conn.execute(
            "SELECT MAX(reply_seen_at), MIN(published_at) FROM home_gpu_jobs",
        ).fetchone()
        contact = last_reply if last_reply is not None else first_published
        offline = contact is not None and now - contact >= OFFLINE_AFTER_SECONDS
        rows = conn.execute(
            "SELECT job_id, kind, deadline, published_at FROM home_gpu_jobs WHERE status='queued'",
        ).fetchall()
        for job_id, kind, deadline, published_at in rows:
            overdue = now >= deadline
            # Offline only counts for jobs the PC could have seen, so new jobs
            # keep being published and a returning PC is noticed.
            if not overdue and not (offline and published_at is not None):
                continue
            batch = kind in BATCH_KINDS
            status = "fallback" if batch else "expired"
            reason = ("home_gpu_expired" if not batch
                      else "home_gpu_deadline" if overdue else "home_gpu_offline")
            changed += conn.execute(
                "UPDATE home_gpu_jobs SET status=?, finished_at=?, error=? "
                "WHERE job_id=? AND status='queued'", (status, now, reason, job_id),
            ).rowcount
    return changed


# ---- 2. publication ----------------------------------------------------------

def _rel(config: MailboxConfig, path: Path) -> str:
    return path.relative_to(config.mailbox_repo_path).as_posix()


def publish_pending(config: MailboxConfig, db_path: str, mailbox_module: ModuleType,
                    now: float | None = None) -> set[str]:
    """Write and commit inbox files for queued jobs; the mailbox sync pushes them.

    Returns the job ids whose inbox file is committed, so ``collect`` can mark
    them published once the sync succeeded.
    """
    now = time.time() if now is None else now
    repo = config.mailbox_repo_path
    with closing(init_db(db_path)) as conn:
        rows = conn.execute(
            "SELECT job_id, kind, deadline, input_sha256, payload, status FROM home_gpu_jobs "
            "WHERE published_at IS NULL AND (status='queued' OR publish_error IS NOT NULL "
            "OR created_at > ?)", (now - 2 * 86400,),
        ).fetchall()
    if not rows:
        return set()
    try:
        key = load_key()
    except SigningKeyError as exc:
        logger.warning("Home GPU publication skipped: %s", exc)
        return set()
    cleanup_spool_artifacts(config)
    live = {_rel(config, inbox_path(config, r[0])): r for r in rows
            if r[5] == "queued" and r[2] > now + PUBLISH_MARGIN_SECONDS}
    stale = {_rel(config, inbox_path(config, r[0])) for r in rows} - set(live)
    status = mailbox_module._git(["status", "--porcelain", "--untracked-files=all"], repo, check=False)
    if status.returncode:
        return set()
    pending: dict[str, str] = {}
    for entry in status.stdout.splitlines():
        name = entry[3:]
        if entry.startswith("?? outbox/"):
            continue
        if entry[:2] in ("??", "A ") and name in live.keys() | stale:
            pending[name] = entry[:2]
            continue
        logger.warning("Home GPU publication deferred by mailbox worktree changes")
        return set()
    for name in stale & pending.keys():
        # Overdue or no longer queued: withdraw the unpublished file.
        if pending.pop(name) == "A ":
            mailbox_module._git(["rm", "-q", "--cached", "-f", "--", name], repo, check=False)
        (repo / name).unlink(missing_ok=True)
    paths: list[Path] = []
    committed: set[str] = set()
    for name, row in live.items():
        tracked = mailbox_module._git(["ls-files", "--error-unmatch", "--", name], repo,
                                      check=False).returncode == 0
        if tracked and name not in pending:
            committed.add(row[0])
            continue
        try:
            paths.append(write_inbox(config, row[:5], key))
        except (OSError, ValueError) as exc:
            # The row stays queued and identifiable; the next poll retries.
            with closing(init_db(db_path)) as conn, conn:
                conn.execute("UPDATE home_gpu_jobs SET publish_error=? WHERE job_id=?",
                             (type(exc).__name__, row[0]))
            logger.warning("Home GPU inbox write failed for %s: %s", row[0], type(exc).__name__)
    if paths:
        if not mailbox_module.git_add_and_commit(repo, paths, "Publish Nexus home GPU batch jobs"):
            # Withdraw our files: an uncommitted inbox file would block the
            # regular mailbox sync. The rows stay queued and are retried.
            for path in paths:
                mailbox_module._git(["rm", "-q", "--cached", "-f", "--ignore-unmatch", "--",
                                     _rel(config, path)], repo, check=False)
                path.unlink(missing_ok=True)
            with closing(init_db(db_path)) as conn, conn:
                conn.executemany("UPDATE home_gpu_jobs SET publish_error='commit_failed' WHERE job_id=?",
                                 [(path.stem,) for path in paths])
            return committed
        committed |= {path.stem for path in paths}
        with closing(init_db(db_path)) as conn, conn:
            conn.executemany("UPDATE home_gpu_jobs SET publish_error=NULL WHERE job_id=?",
                             [(job_id,) for job_id in committed])
    return committed


# ---- 4. collection -----------------------------------------------------------

def _committed_reply(config: MailboxConfig, job_id: str) -> str | None:
    """The reply as committed in the synced ``HEAD``; worktree files never count."""
    rel = f"{_rel(config, config.outbox_dir)}/{job_id}.reply.md"
    repo = str(config.mailbox_repo_path)
    size = subprocess.run(["git", "cat-file", "-s", f"HEAD:{rel}"], cwd=repo,
                          capture_output=True, text=True, timeout=30)
    if size.returncode or not size.stdout.strip().isdigit():
        return None
    if int(size.stdout) > MAX_PAYLOAD:
        raise ValueError("reply exceeds size limit")
    blob = subprocess.run(["git", "cat-file", "blob", f"HEAD:{rel}"], cwd=repo,
                          capture_output=True, timeout=30)
    if blob.returncode:
        return None
    return blob.stdout.decode("utf-8")


def collect(config: MailboxConfig, db_path: str, *, published: set[str] | None = None,
            now: float | None = None) -> int:
    """Store valid replies. Returns how many jobs became ``answered``."""
    now = time.time() if now is None else now
    try:
        key = load_key()
    except SigningKeyError as exc:
        logger.warning("Home GPU collection skipped: %s", exc)
        return 0
    answered = 0
    with closing(init_db(db_path)) as conn, conn:
        for job_id in published or ():
            conn.execute("UPDATE home_gpu_jobs SET published_at=? WHERE job_id=? AND published_at IS NULL",
                         (now, job_id))
        rows = conn.execute(
            "SELECT job_id, status, deadline, input_sha256, expected_model, answer, fallback_job_id "
            "FROM home_gpu_jobs WHERE published_at IS NOT NULL AND reply_seen_at IS NULL "
            "AND deadline > ?", (now - LATE_REPLY_WINDOW_SECONDS,),
        ).fetchall()
    for job_id, status, deadline, digest, expected_model, answer, fallback_job in rows:
        try:
            text = _committed_reply(config, job_id)
            if text is None:
                continue
            meta, body = split_message(text)
            result = verify_reply(key, meta, body, job_id=job_id, digest=digest)
            if expected_model and result["model"] != expected_model:
                raise ValueError("reply is from a different model")
            if contains_credential(result["answer"]):
                raise ValueError("reply contains a credential shape")
        except (OSError, UnicodeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            logger.warning("Ignored invalid home GPU reply for job %s: %s", job_id, exc)
            continue
        with closing(init_db(db_path)) as conn, conn:
            conn.execute("UPDATE home_gpu_jobs SET reply_seen_at=? WHERE job_id=?", (now, job_id))
            # Before the deadline a PC answer wins, also over a fallback the
            # VM has not started; afterwards it only proves the PC is alive.
            usable = (now < deadline and answer is None and fallback_job is None
                      and status in ("queued", "fallback"))
            if usable:
                answered += conn.execute(
                    "UPDATE home_gpu_jobs SET status='answered', answered_at=?, answer_host='home-gpu', "
                    "finished_at=?, error=NULL, model=?, prompt_tokens=?, completion_tokens=?, "
                    "seconds=?, tokens_per_second=?, answer=? "
                    "WHERE job_id=? AND answer IS NULL AND fallback_job_id IS NULL "
                    "AND status IN ('queued', 'fallback')",
                    (now, now, result["model"], result["prompt_tokens"], result["completion_tokens"],
                     result["seconds"], result["tokens_per_second"], result["answer"], job_id),
                ).rowcount
    return answered


# ---- 5. checking generic jobs ------------------------------------------------

def record_check(conn: sqlite3.Connection, job_id: str, status: str, detail: str | None,
                 now: float | None = None) -> None:
    conn.execute(
        "UPDATE home_gpu_jobs SET checked_at=?, check_status=?, check_detail=?, "
        "fallback_lease_until=NULL WHERE job_id=? AND checked_at IS NULL",
        (time.time() if now is None else now, status, (detail or "")[:500] or None, job_id),
    )


def run_checks(repo: Path, db_path: str, *, limit: int = 20) -> int:
    """Hand each answered generic job to its checker; returns how many were checked."""
    from modules.home_gpu.checkers import check_answer

    with closing(init_db(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM home_gpu_jobs WHERE replay_key IS NULL AND answer IS NOT NULL "
            "AND checked_at IS NULL ORDER BY created_at LIMIT ?", (limit,),
        )]
    for job in rows:
        try:
            status, detail = check_answer(job, Path(repo))
        except Exception as exc:  # noqa: BLE001 - a broken checker is a failed check
            status, detail = "error", f"checker raised {type(exc).__name__}"
        with closing(init_db(db_path)) as conn, conn:
            record_check(conn, job["job_id"], status, detail)
    return len(rows)
