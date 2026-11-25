"""Tests main already has that fail under clock drift (#725).

A timing-check flag on a test that also fails under the same drift on the
experiment's base commit is not the experiment's fault. Such tests are kept in
the run_store table ``known_drift_tests``: one row per test id with the commit
it was first seen on, when it was last seen and how often. The first sighting
opens one GitHub issue per test ('Make <test> independent of wall-clock time').
"""

from __future__ import annotations

import sqlite3
from typing import Any

ISSUE_TITLE_TEMPLATE = "Make {test} independent of wall-clock time"
ISSUE_TAIL_MAX_CHARS = 6000


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS known_drift_tests (
            test_id TEXT PRIMARY KEY,
            first_seen_commit TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NULL,
            last_experiment_id TEXT NULL,
            issue_number INTEGER NULL
        );
        """
    )


def record_sighting(
    conn: sqlite3.Connection, test_id: str, commit: str, now: str,
    experiment_id: str | None = None,
) -> bool:
    """Insert *test_id* or count one more sighting; True when it is new."""
    with conn:
        cursor = conn.execute(
            "UPDATE known_drift_tests SET count = count + 1, last_seen_at = ?, "
            "last_experiment_id = COALESCE(?, last_experiment_id) WHERE test_id = ?",
            (now, experiment_id, test_id),
        )
        if cursor.rowcount:
            return False
        conn.execute(
            "INSERT INTO known_drift_tests (test_id, first_seen_commit, last_seen_at, count, "
            "first_seen_at, last_experiment_id) VALUES (?, ?, ?, 1, ?, ?)",
            (test_id, commit, now, now, experiment_id),
        )
    return True


def needs_issue(conn: sqlite3.Connection, test_id: str) -> bool:
    """True while no issue is recorded for *test_id* (also after a failed open)."""
    row = conn.execute(
        "SELECT issue_number FROM known_drift_tests WHERE test_id = ?", (test_id,),
    ).fetchone()
    return row is not None and row[0] is None


def set_issue(conn: sqlite3.Connection, test_id: str, number: int) -> None:
    with conn:
        conn.execute(
            "UPDATE known_drift_tests SET issue_number = ? WHERE test_id = ?",
            (int(number), test_id),
        )


def list_known_drift(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Known drift tests, most recently seen first."""
    if limit <= 0:
        return []
    cursor = conn.execute(
        "SELECT test_id, first_seen_commit, first_seen_at, last_seen_at, count, "
        "last_experiment_id, issue_number FROM known_drift_tests "
        "ORDER BY last_seen_at DESC, test_id LIMIT ?",
        (min(int(limit), 200),),
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def issue_title(test_id: str) -> str:
    return ISSUE_TITLE_TEMPLATE.format(test=test_id)


def issue_body(
    test_id: str, base_commit: str, tail: str | None, *, experiment_id: str | None = None,
    drift_ms: int | float = 50,
) -> str:
    """A small, self-contained task for the local coding worker; the drift rerun judges."""
    file = test_id.split("::", 1)[0]
    lines = [
        f"`{test_id}` fails under the timing check's clock drift on main itself "
        f"(base commit `{base_commit[:12]}`), so every experiment that touches the code "
        "it imports is flagged for it (#725).",
        "",
        "## Task",
        "",
        "Make this one test independent of wall-clock time: inject the clock or compute "
        "the value from configured values, not from wall time. Change only the test or "
        "the code it measures, in one or two files.",
        "",
        "Complexity: small - a clear change in one or two files. "
        "Give it to the local coding worker first; a paid model only if that fails.",
        "",
        "## Judge",
        "",
        "The drift rerun of this test must pass (and the normal run must stay green):",
        "",
        "```",
        f"NEXUS_TEST_CLOCK_DRIFT_MS={drift_ms} python -m pytest -q -p no:cacheprovider '{test_id}'",
        f"python -m pytest -q '{file}'",
        "```",
    ]
    if experiment_id:
        lines.extend(["", f"First seen in experiment `{experiment_id}`."])
    if tail:
        lines.extend([
            "", "## Failure tail under drift", "", "````text",
            tail[-ISSUE_TAIL_MAX_CHARS:], "````",
        ])
    return "\n".join(lines)
