"""Classify why an experiment produced no publishable changes (#811).

2026-11-22 the weekly review (#807) found that 20 of 73 experiments in one week
ended with "implementation produced no publishable source/test/documentation
changes". This module gives every such run a durable, countable cause:

- ``wrote_outside_worktree`` - the coding agent edited the deployed checkout
  instead of its worktree (detected and stashed by #803's
  ``agent_wrote_outside_worktree`` event / the "wrote outside its worktree"
  error text);
- ``limit_or_timeout`` - the run hit a provider usage limit, an overload, a
  stall/timeout watchdog stop or the token budget;
- ``already_done_on_main`` - the agent found the change already present on the
  base commit and edited nothing;
- ``answered_without_edits`` - the implementation call itself succeeded but the
  agent only answered or planned, so the worktree stayed clean;
- ``other`` - anything else (worker crashes, checkpoint conflicts, ...).

The cause is recorded per experiment in three places so the weekly review can
count it: the ``experiments.no_changes_cause`` column, a ``no_changes_cause``
event in the experiment's JSONL log, and a ``no_changes_cause=...`` token in the
``error`` text.

``classify_recent_no_change_runs`` (also runnable as
``python -m modules.no_changes_cause <db> [--log-dir DIR] [--limit 20]``)
classifies the most recent no-change runs of a database and prints the counts,
so "the causes of the last 20 such runs, with counts" is a command, not an
archaeology project.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from modules.central_logging import ExperimentLogger, read_log_entries
from modules.coding_provider import is_provider_limit, is_provider_overload
from modules.run_store import DEFAULT_DB_PATH

# --- causes ---------------------------------------------------------------

CAUSE_OUTSIDE_WORKTREE = "wrote_outside_worktree"
CAUSE_LIMIT_OR_TIMEOUT = "limit_or_timeout"
CAUSE_ALREADY_ON_MAIN = "already_done_on_main"
CAUSE_NO_EDITS = "answered_without_edits"
CAUSE_OTHER = "other"
ALL_CAUSES = (
    CAUSE_OUTSIDE_WORKTREE,
    CAUSE_LIMIT_OR_TIMEOUT,
    CAUSE_ALREADY_ON_MAIN,
    CAUSE_NO_EDITS,
    CAUSE_OTHER,
)

#: Event name written to the experiment log when a cause is recorded.
EVENT = "no_changes_cause"

#: Matches the checkpoint error text of ``checkpoint_experiment``.
NO_PUBLISHABLE_MARKER = "no publishable"

#: Statuses whose runs can end without publishable changes (policy rejections
#: are excluded: they never started work, #408).
NO_CHANGE_STATUSES = ("failed", "timed_out", "needs_review", "interrupted", "cancelled")
#: Every status an experiment can end in.
ENDED_STATUSES = NO_CHANGE_STATUSES + ("succeeded", "rejected")

_PIPELINE_CRASH_PREFIX = "unhandled exception in experiment pipeline: "

_TIMEOUT_RE = re.compile(
    r"timed out|stalled|absolute cap|time limit|token budget|stopped by the run observer|watchdog",
    re.IGNORECASE,
)
_OUTSIDE_RE = re.compile(r"wrote outside (?:its|the) worktree", re.IGNORECASE)
# A verification failure is not the implementation worker's outcome: when the
# worker call itself succeeded, the run still counts as "answered without
# edits" even if an earlier verification round failed.
_VERIFICATION_ERROR_RE = re.compile(r"^(?:tests failed|verification failed|quality gate)", re.IGNORECASE)
# The agent found the goal already implemented on the base/main and edited nothing.
_ALREADY_ON_MAIN_RE = re.compile(
    r"already (?:implemented|present|merged|done|in main|on main|in the base"
    r"|in the base commit|contained in (?:main|the base))",
    re.IGNORECASE,
)
# How much of an experiment log is scanned for evidence: the relevant events
# (worker result, guard verdict, checkpoint failure) are at the end.
_LOG_SCAN_LIMIT = 2000


def is_no_changes_error(error: Any) -> bool:
    """True when *error* is the checkpoint's "no publishable changes" failure."""
    return NO_PUBLISHABLE_MARKER in str(error or "")


def _outside_worktree_event() -> str:
    # Lazy import: modules.checkout_guard imports the cause constant from here.
    from modules.checkout_guard import EVENT as guard_event

    return guard_event


def _log_entries(log_dir: str | Path | None, experiment_id: str) -> list[dict[str, Any]]:
    if not log_dir:
        return []
    path = Path(log_dir) / f"{experiment_id}.jsonl"
    try:
        return read_log_entries(path, limit=_LOG_SCAN_LIMIT)
    except (OSError, ValueError):
        return []


def _entry_texts(entry: dict[str, Any]) -> list[str]:
    """The human/agent-written text fields of one log entry worth scanning."""
    texts = []
    for key in ("message", "stdout", "stderr", "final_message", "reason",
                "exception_message", "detail"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def _strip_checkpoint_error(text: str) -> str:
    """Remove the checkpoint-failure wrapping from *text*.

    ``_verify`` prefixes "GitHub checkpoint failed before verification: " and
    ``_checkpoint_interrupted_work`` appends "; GitHub checkpoint failed: ...";
    what remains is the implementation worker's own outcome ("" when the worker
    itself succeeded).
    """
    stripped = text
    for prefix in (
        "GitHub checkpoint failed before verification: ",
        "GitHub checkpoint failed: ",
    ):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    idx = stripped.find("; GitHub checkpoint failed:")
    if idx != -1:
        stripped = stripped[:idx]
    # The no-publishable message itself is always terminal in the error text
    # (including its "; excluded runtime paths: ..." detail).
    stripped = re.sub(r"implementation produced no publishable.*$", "", stripped, flags=re.DOTALL)
    return stripped.strip(" ;")


def _error_is_limit_or_timeout(status: Any, error: Any) -> bool:
    text = str(error or "")
    if status == "timed_out":
        return True
    if _TIMEOUT_RE.search(text):
        return True
    # A test failure or pipeline crash that merely quotes a limit word is not a
    # provider limit (same guard as nexus_initiative._is_provider_overload).
    if text.startswith("tests failed") or text.startswith(_PIPELINE_CRASH_PREFIX):
        return False
    return is_provider_limit(text) or is_provider_overload(text)


def classify_no_changes_cause(
    conn: sqlite3.Connection,
    experiment_id: str,
    log_dir: str | Path | None = None,
    *,
    use_recorded: bool = True,
    extra_error: str | None = None,
) -> str:
    """Classify one experiment's "no publishable changes" run.

    Reads the ``experiments`` row (and, when *log_dir* is given, the tail of the
    experiment's JSONL log) and returns one of :data:`ALL_CAUSES`. With
    *use_recorded* a previously recorded ``no_changes_cause`` wins, so repeated
    classification is idempotent. *extra_error* is OR-ed into the error text -
    the pipeline classifies interrupted runs before their terminal status/error
    is persisted, so the worker's own error is passed in explicitly.
    """
    try:
        row = conn.execute(
            "SELECT status, error, no_changes_cause FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Pre-#811 database without the column: classify from status/error only.
        row = conn.execute(
            "SELECT status, error, NULL FROM experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return CAUSE_OTHER

    def col(key: str, index: int) -> Any:
        return row[index] if isinstance(row, tuple) else row[key]

    status, error, recorded = col("status", 0), col("error", 1), col("no_changes_cause", 2)
    if use_recorded and recorded in ALL_CAUSES:
        return recorded

    entries = _log_entries(log_dir, experiment_id)
    log_text = "\n".join(
        text for entry in entries for text in _entry_texts(entry)
    )
    error_text = str(error or "")
    if extra_error:
        error_text = (error_text + "\n" + str(extra_error)) if error_text else str(extra_error)

    # 1. The agent wrote into the deployed checkout instead of its worktree:
    #    #803's guard event, or the error the guard put into the worker result.
    outside_event = _outside_worktree_event()
    if any(entry.get("event") == outside_event for entry in entries) or _OUTSIDE_RE.search(error_text):
        return CAUSE_OUTSIDE_WORKTREE

    # 2. The run hit a limit or a timeout before/while editing.
    if _error_is_limit_or_timeout(status, error_text):
        return CAUSE_LIMIT_OR_TIMEOUT

    worker_error = _strip_checkpoint_error(error_text)
    if _VERIFICATION_ERROR_RE.match(worker_error):
        worker_error = ""

    # 3. The agent found the goal already implemented on the base commit.
    if not worker_error and _ALREADY_ON_MAIN_RE.search(log_text):
        return CAUSE_ALREADY_ON_MAIN

    # 4. The implementation call succeeded but changed nothing: it answered or
    #    planned without editing.
    if not worker_error and status != "failed":
        return CAUSE_NO_EDITS

    return CAUSE_OTHER


def set_no_changes_cause(conn: sqlite3.Connection, experiment_id: str, cause: str) -> None:
    """Persist *cause* on the experiment row; a pre-#811 database without the
    column is tolerated (the log event and error token still record it)."""
    try:
        with conn:
            conn.execute(
                "UPDATE experiments SET no_changes_cause = ? WHERE id = ?",
                (cause, experiment_id),
            )
    except sqlite3.OperationalError:
        pass


def record_no_changes_cause(
    conn: sqlite3.Connection,
    experiment_id: str,
    cause: str,
    elog: Any | None = None,
    phase: str = "verifying",
) -> str:
    """Record *cause* on the row AND as a ``no_changes_cause`` log event."""
    set_no_changes_cause(conn, experiment_id, cause)
    if elog is not None:
        try:
            elog.warning(
                phase, EVENT,
                f"Implementation produced no publishable changes; classified cause: {cause}",
                no_changes_cause=cause,
                experiment_id=experiment_id,
            )
        except Exception:  # noqa: BLE001 - logging must never crash the pipeline
            pass
    return cause


def find_no_change_runs(
    conn: sqlite3.Connection, limit: int = 20
) -> list[sqlite3.Row]:
    """The most recent experiments that ended without publishable changes."""
    placeholders = ",".join("?" for _ in NO_CHANGE_STATUSES)
    sql = f"""
        SELECT id, status, error, diff_text, no_changes_cause, created_at
        FROM experiments
        WHERE status IN ({placeholders})
          AND (COALESCE(diff_text, '') = '' OR COALESCE(error, '') LIKE '%{NO_PUBLISHABLE_MARKER}%')
        ORDER BY created_at DESC
        LIMIT ?
        """
    args = (*NO_CHANGE_STATUSES, int(limit))
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        # Pre-#811 database without the column.
        return conn.execute(sql.replace("no_changes_cause", "NULL"), args).fetchall()


def classify_recent_no_change_runs(
    db_path: str,
    log_dir: str | Path | None = None,
    limit: int = 20,
    *,
    apply: bool = True,
) -> dict[str, int]:
    """Classify the last *limit* no-change runs of the database at *db_path*.

    With ``apply`` the cause is written to ``experiments.no_changes_cause`` and
    appended to the experiment's JSONL log (when *log_dir* is given). Returns
    the counts per cause, e.g. ``{"answered_without_edits": 12, ...}``.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        counts: Counter[str] = Counter()
        for row in find_no_change_runs(conn, limit=limit):
            experiment_id = row["id"]
            cause = classify_no_changes_cause(conn, experiment_id, log_dir)
            counts[cause] += 1
            if apply:
                set_no_changes_cause(conn, experiment_id, cause)
                if log_dir:
                    try:
                        ExperimentLogger(experiment_id, log_dir=log_dir).warning(
                            "review", EVENT,
                            f"Backfilled no-change cause for experiment {experiment_id}: {cause}",
                            no_changes_cause=cause,
                            experiment_id=experiment_id,
                        )
                    except Exception:  # noqa: BLE001 - best effort backfill
                        pass
        return dict(counts)
    finally:
        conn.close()


def no_change_cause_counts(conn: sqlite3.Connection, since: str) -> list[tuple[str, int]]:
    """Recorded causes of no-change runs created at/after *since*, for the
    weekly review. Requires the ``no_changes_cause`` column (#811)."""
    placeholders = ",".join("?" for _ in NO_CHANGE_STATUSES)
    return [
        (str(row[0]), int(row[1]))
        for row in conn.execute(
            f"""
            SELECT COALESCE(no_changes_cause, '(unclassified)'), COUNT(*)
            FROM experiments
            WHERE created_at >= ?
              AND status IN ({placeholders})
              AND (COALESCE(diff_text, '') = '' OR COALESCE(error, '') LIKE '%{NO_PUBLISHABLE_MARKER}%')
            GROUP BY 1 ORDER BY 2 DESC
            """,
            (since, *NO_CHANGE_STATUSES),
        ).fetchall()
    ]


def no_change_share(conn: sqlite3.Connection, since: str) -> tuple[int, int, float] | None:
    """(no-change runs, all ended runs, percent) created at/after *since*."""
    ended = ",".join("?" for _ in ENDED_STATUSES)
    no_change = ",".join("?" for _ in NO_CHANGE_STATUSES)
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN status IN ({no_change})
                    AND (COALESCE(diff_text, '') = '' OR COALESCE(error, '') LIKE '%{NO_PUBLISHABLE_MARKER}%')
                   THEN 1 ELSE 0 END),
          COUNT(*)
        FROM experiments
        WHERE created_at >= ? AND status IN ({ended})
        """,
        (*NO_CHANGE_STATUSES, since, *ENDED_STATUSES),
    ).fetchone()
    if row is None or not row[1]:
        return None
    no_changes, total = int(row[0] or 0), int(row[1])
    return no_changes, total, round(100.0 * no_changes / total, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify the most recent experiments that produced no publishable "
                    "changes (#811) and print the cause counts.",
    )
    parser.add_argument("db_path", nargs="?", default=DEFAULT_DB_PATH,
                        help="SQLite database (default: AGENT_DB_PATH or /var/lib/nexus/agent.db)")
    parser.add_argument("--log-dir", default=None,
                        help="experiment JSONL log directory (default: none, row evidence only)")
    parser.add_argument("--limit", type=int, default=20,
                        help="how many recent no-change runs to classify (default: 20)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only print the classification, do not write it back")
    args = parser.parse_args(argv)

    counts = classify_recent_no_change_runs(
        args.db_path, log_dir=args.log_dir, limit=args.limit, apply=not args.dry_run,
    )
    total = sum(counts.values())
    print(f"no-change runs classified: {total} (limit {args.limit}, "
          f"{'written' if not args.dry_run else 'dry-run'})")
    for cause, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {cause}: {count}")
    if not counts:
        print("  (none found)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
