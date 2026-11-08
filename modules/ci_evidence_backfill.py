"""Backfill authoritative GitHub Actions evidence from pre-#139 lifecycle state.

Copies only explicit GitHub Actions verdicts from ``verification_runs`` into the
durable ``github_ci_verifications`` table (#138/#139); it never upgrades a
generic/local test pass.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
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
    "service_settings",
}
_BACKFILL_MARKER = "migration.issue140_ci_evidence_backfill_v1"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _git_parent(repo: Path, checkpoint_sha: str) -> str | None:
    if not repo.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"{checkpoint_sha}^1"],
            cwd=str(repo),
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


def _checkpoint_base(
    worktree_path: str | None,
    checkpoint_sha: str,
    *,
    repo_root: str | None = None,
) -> str | None:
    """Resolve the checkpoint parent without any GitHub/network lookup.

    Prefer the historical worktree while it still exists. If it has already
    been cleaned up (or cannot resolve the object), fall back to the main
    repository: git worktrees share its object store and the published Nexus
    branch keeps the checkpoint reachable there.
    """
    candidates: list[Path] = []
    if worktree_path:
        candidates.append(Path(worktree_path))
    fallback = repo_root or os.environ.get("NEXUS_REPO_ROOT") or os.getcwd()
    if fallback:
        candidate = Path(fallback)
        if candidate not in candidates:
            candidates.append(candidate)
    production_root = Path("/agent")
    if production_root not in candidates:
        candidates.append(production_root)

    for candidate in candidates:
        base_sha = _git_parent(candidate, checkpoint_sha)
        if base_sha is not None:
            return base_sha
    return None


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
    # Later lines carry the failure detail of a red run (#472).
    match = _CI_OUTPUT_RE.fullmatch(str(row["output"] or "").strip().partition("\n")[0].strip())
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


def _backfill_done(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM service_settings WHERE key=?",
        (_BACKFILL_MARKER,),
    ).fetchone()
    return bool(row and str(row[0]) == "done")


def _mark_backfill_done(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO service_settings(key, value, updated_at)
        VALUES (?, 'done', ?)
        ON CONFLICT(key) DO UPDATE SET value='done', updated_at=excluded.updated_at
        """,
        (_BACKFILL_MARKER, datetime.now(timezone.utc).isoformat()),
    )


def backfill_legacy_ci_evidence(
    conn: sqlite3.Connection,
    *,
    repo_root: str | None = None,
) -> int:
    """Migrate pre-#139 CI evidence once, without network access.

    Existing durable rows always win. The checkpoint's first parent is
    reconstructed from local git so #139's base-SHA validity rule remains
    enforceable after migration. Once the historical population has been
    scanned, a durable marker prevents future ``init_db`` hot paths from
    rescanning every experiment or spawning git subprocesses again.
    """
    if not _REQUIRED_TABLES.issubset(_tables(conn)):
        # In particular, GitHubWorkTrackingService may not have created its
        # table yet. Do not mark the migration complete until it can run.
        return 0
    if _backfill_done(conn):
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
            base_sha = _checkpoint_base(
                link["worktree_path"], checkpoint_sha, repo_root=repo_root
            )
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

        # This is a versioned one-shot migration. Current code persists new CI
        # observations directly, so no future historical candidates can appear.
        _mark_backfill_done(conn)
        conn.commit()
        return inserted
    finally:
        conn.row_factory = old_row_factory
