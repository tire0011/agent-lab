"""Diff size of verified experiments next to their review verdict (#821).

Verification stores ``diff_files``, ``diff_added`` and ``diff_removed`` on the
experiment row (``git diff --numstat <base>``, free). This module reads them
back:

- :func:`parse_numstat` turns numstat output into the three numbers;
- :func:`size_vs_verdict` groups finished experiments by changed lines and
  reports, per bucket, how many merged, were blocked by the review or ended
  ``needs_review``;
- :func:`split_guard_applies` is the planner-side rule: two review-blocked
  attempts that were both large are not retried a third time but split;
- :func:`backfill_diff_sizes` fills the numbers for older rows from branches
  that still exist.

Nothing here writes to GitHub.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Upper bounds (inclusive) of the changed-line buckets; the last is open.
BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("<=80", 80),
    ("81-200", 200),
    ("201-500", 500),
    (">500", None),
)

SPLIT_DIFF_LINES_ENV = "NEXUS_SPLIT_DIFF_LINES"
DEFAULT_SPLIT_DIFF_LINES = 300
# Review-blocked attempts with a large diff after which a plain retry stops.
SPLIT_GUARD_ATTEMPTS = 2
# Same wording as the planner's SPLIT instruction (#626): "too large to do in
# one experiment", so the planner sees the reason it already knows how to act on.
SPLIT_GUARD_REASON = (
    "too large to do in one experiment: the review blocked {attempts} attempts whose "
    "diffs changed {sizes} lines (each over {threshold}); split it into smaller parts "
    "instead of a plain retry"
)

_REVIEW_BLOCK_PREFIX = "Review by "
_MERGED_ADOPTION_STATUSES = ("adopted", "succeeded")


@dataclass(frozen=True, slots=True)
class DiffSize:
    files: int
    added: int
    removed: int

    @property
    def lines(self) -> int:
        return self.added + self.removed


def parse_numstat(output: str) -> DiffSize:
    """Sum ``git diff --numstat`` output.

    A binary file is listed as ``-\\t-\\tpath``: it counts as a changed file
    with zero added and removed lines.
    """
    files = added = removed = 0
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        files += 1
        added += _count(parts[0])
        removed += _count(parts[1])
    return DiffSize(files, added, removed)


def _count(value: str) -> int:
    value = value.strip()
    return int(value) if value.isdigit() else 0


def bucket_of(lines: int) -> str:
    for label, upper in BUCKETS:
        if upper is None or lines <= upper:
            return label
    return BUCKETS[-1][0]


def verdict_of(status: Any, adoption_status: Any, adoption_reason: Any) -> str:
    """``merged``, ``review_blocked``, ``needs_review`` or ``other``."""
    adoption_status = str(adoption_status or "")
    if adoption_status in _MERGED_ADOPTION_STATUSES:
        return "merged"
    if adoption_status == "failed" and str(adoption_reason or "").startswith(_REVIEW_BLOCK_PREFIX):
        return "review_blocked"
    if str(status or "") == "needs_review":
        return "needs_review"
    return "other"


def _connect(db: Any) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        return db, False
    conn = sqlite3.connect(str(db))
    return conn, True


def size_vs_verdict(db: Any, days: int = 30, *, now: datetime | None = None) -> dict[str, Any]:
    """Finished experiments of the last *days* days, grouped by changed lines.

    *db* is a path or an open connection. Rows without a stored diff size are
    counted in ``unsized`` only. Each bucket holds ``count`` and the shares
    (0..1, ``None`` for an empty bucket) of ``merged``, ``review_blocked`` and
    ``needs_review``.
    """
    days = max(1, int(days))
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    conn, owned = _connect(db)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT e.status AS status, e.ended_at AS ended_at,
                       e.diff_added AS diff_added, e.diff_removed AS diff_removed,
                       a.status AS adoption_status, a.reason AS adoption_reason
                FROM experiments e
                LEFT JOIN experiment_adoptions a ON a.experiment_id = e.id
                WHERE e.ended_at IS NOT NULL AND e.ended_at >= ?
                """,
                (since,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        if owned:
            conn.close()
    counts = {label: {"count": 0, "merged": 0, "review_blocked": 0, "needs_review": 0}
              for label, _ in BUCKETS}
    unsized = 0
    for row in rows:
        if row["diff_added"] is None or row["diff_removed"] is None:
            unsized += 1
            continue
        label = bucket_of(int(row["diff_added"]) + int(row["diff_removed"]))
        entry = counts[label]
        entry["count"] += 1
        verdict = verdict_of(row["status"], row["adoption_status"], row["adoption_reason"])
        if verdict in entry:
            entry[verdict] += 1
    buckets = []
    for label, _ in BUCKETS:
        entry = counts[label]
        total = entry["count"]
        buckets.append({
            "bucket": label,
            "count": total,
            **{
                f"{key}_share": (entry[key] / total if total else None)
                for key in ("merged", "review_blocked", "needs_review")
            },
        })
    return {"days": days, "since": since, "sized": sum(b["count"] for b in buckets),
            "unsized": unsized, "buckets": buckets}


def split_diff_lines() -> int:
    raw = os.environ.get(SPLIT_DIFF_LINES_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SPLIT_DIFF_LINES
    except ValueError:
        return DEFAULT_SPLIT_DIFF_LINES
    return value if value > 0 else DEFAULT_SPLIT_DIFF_LINES


def split_guard_applies(blocked_sizes: Iterable[int | None], threshold: int | None = None) -> bool:
    """True when the newest review-blocked attempts were all too large.

    *blocked_sizes* are the stored changed lines of an issue's review-blocked
    attempts, oldest first (``None`` when unknown). The newest
    ``SPLIT_GUARD_ATTEMPTS`` must all be known and over *threshold*.
    """
    threshold = split_diff_lines() if threshold is None else threshold
    sizes = list(blocked_sizes)
    if len(sizes) < SPLIT_GUARD_ATTEMPTS:
        return False
    newest = sizes[-SPLIT_GUARD_ATTEMPTS:]
    return all(size is not None and size > threshold for size in newest)


def split_guard_reason(blocked_sizes: Iterable[int | None], threshold: int | None = None) -> str:
    threshold = split_diff_lines() if threshold is None else threshold
    newest = list(blocked_sizes)[-SPLIT_GUARD_ATTEMPTS:]
    return SPLIT_GUARD_REASON.format(
        attempts=len(newest), sizes=" and ".join(str(s) for s in newest), threshold=threshold,
    )


def backfill_diff_sizes(db: Any, repo_root: str) -> dict[str, int]:
    """Fill the diff size of older rows from branches that still exist.

    Reads ``git diff --numstat <base> <branch>`` in *repo_root* (free, local
    only). Rows whose branch or base is gone stay ``NULL``.
    """
    conn, owned = _connect(db)
    filled = missing = 0
    try:
        try:
            rows = conn.execute(
                "SELECT id, branch_name, base_commit_sha FROM experiments "
                "WHERE diff_files IS NULL AND branch_name IS NOT NULL "
                "AND base_commit_sha IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            return {"filled": 0, "missing": 0}
        for experiment_id, branch, base in rows:
            try:
                proc = subprocess.run(
                    ["git", "diff", "--numstat", "--no-color", str(base), str(branch), "--"],
                    cwd=repo_root, capture_output=True, text=True, timeout=60, check=True,
                )
            except (OSError, subprocess.SubprocessError):
                missing += 1
                continue
            size = parse_numstat(proc.stdout)
            with conn:
                conn.execute(
                    "UPDATE experiments SET diff_files = ?, diff_added = ?, diff_removed = ? "
                    "WHERE id = ? AND diff_files IS NULL",
                    (size.files, size.added, size.removed, experiment_id),
                )
            filled += 1
    finally:
        if owned:
            conn.close()
    return {"filled": filled, "missing": missing}
