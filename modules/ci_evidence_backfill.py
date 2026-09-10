"""Backfill authoritative GitHub Actions evidence from pre-#139 lifecycle state.

Issue #138 made exact-commit CI verdicts durable in ``github_ci_verifications``.
Experiments verified before that table existed can already contain the same
accepted verdict in ``verification_runs``.  This migration copies only that
explicit GitHub Actions evidence; it never upgrades a generic/local test pass.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CI_OUTPUT_RE = re.compile(
    r"^GitHub Actions run (?P<run_id>\d+) for commit "
    r"(?P<commit>[0-9a-fA-F]{40}) completed with conclusion "
    r"(?P<conclusion>success|failure)\.(?: (?P<url>https?://\S+))?$"
)
_REQUIRED_TABLES = {
    "experiments",
    "verification_runs",
    "github_experiment_links",
    "github_ci_verifications",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _checkpoint_base(worktree_path: str | None, checkpoint_sha: str) -> str | None:
    """Resolve the checkpoint parent using only the persisted local worktree."""
    if not worktree_path or not Path(worktree_path).is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"{checkpoint_sha}^1"],
            cwd=worktree_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    base_sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or _SHA_RE.fullmatch(base_sha) is None:
        return None
    return base_sha.lower()


def _parse_authoritative_ci(row: sqlite3.Row, checkpoint_sha: str):
    """Return structured CI fields only for the exact historical CI format."""
    try:
        command = json.loads(row["command_json"] or "null")
    except (TypeError, json.JSONDecodeError):
        return None
    if not (
        isinstance(command, list)
        and len(command) >= 3
        and command[0] == "GitHub Actions"
        and str(command[2]).lower() == checkpoint_sha
    ):
        return None

    workflow = str(command[1] or "").strip()
    if not workflow:
        return None
    match = _CI_OUTPUT_RE.fullmatch(str(row["output"] or "").strip())
    if match is None or match.group("commit").lower() != checkpoint_sha:
        return None

    conclusion = match.group("conclusion").lower()
    status = "passed" if conclusion == "success" else "failed"
    expected_exit = 0 if status == "passed" else 1
    if row["exit_code"] != expected_exit:
        return None
    return {
        "status": status,
        "conclusion": conclusion,
        "run_id": int(match.group("run_id")),
        "url": match.group("url"),
        "workflow": workflow,
    }


def backfill_legacy_ci_evidence(conn: sqlite3.Connection) -> int:
    """Migrate pre-#139 CI evidence without network access.

    Existing durable rows always win.  The checkpoint's first parent is
    reconstructed from the persisted experiment worktree so #139's base-SHA
    validity rule remains enforceable after migration.
    """
    if not _REQUIRED_TABLES.issubset(_tables(conn)):
        return 0

    old_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    inserted = 0
    try:
        links = conn.execute(
            """
            SELECT l.experiment_id, lower(l.checkpoint_sha) AS checkpoint_sha,
                   e.worktree_path
            FROM github_experiment_links AS l
            JOIN experiments AS e ON e.id = l.experiment_id
            LEFT JOIN github_ci_verifications AS ci
                   ON lower(ci.commit_sha) = lower(l.checkpoint_sha)
            WHERE l.checkpoint_sha IS NOT NULL
              AND ci.commit_sha IS NULL
            ORDER BY l.updated_at ASC
            """
        ).fetchall()

        for link in links:
            checkpoint_sha = str(link["checkpoint_sha"] or "").strip().lower()
            if _SHA_RE.fullmatch(checkpoint_sha) is None:
                continue
            base_sha = _checkpoint_base(link["worktree_path"], checkpoint_sha)
            if base_sha is None:
                continue

            runs = conn.execute(
                """
                SELECT ended_at, command_json, exit_code, output
                FROM verification_runs
                WHERE experiment_id = ?
                  AND lower(COALESCE(commit_sha, '')) = ?
                ORDER BY iteration DESC
                """,
                (link["experiment_id"], checkpoint_sha),
            ).fetchall()
            for run in runs:
                evidence = _parse_authoritative_ci(run, checkpoint_sha)
                if evidence is None:
                    continue
                observed_at = str(run["ended_at"] or "").strip()
                if not observed_at:
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO github_ci_verifications (
                        commit_sha, experiment_id, status, conclusion, run_id,
                        url, workflow, provider, base_sha, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'github_actions', ?, ?)
                    """,
                    (
                        checkpoint_sha,
                        link["experiment_id"],
                        evidence["status"],
                        evidence["conclusion"],
                        evidence["run_id"],
                        evidence["url"],
                        evidence["workflow"],
                        base_sha,
                        observed_at,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    logger.info(
                        "backfilled legacy GitHub CI evidence for experiment %s commit %s",
                        link["experiment_id"],
                        checkpoint_sha,
                    )
                break
        if inserted:
            conn.commit()
        return inserted
    finally:
        conn.row_factory = old_row_factory
