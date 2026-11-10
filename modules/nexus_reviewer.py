"""Code review before Nexus merges its own pull request (#396).

Tests and the deterministic quality gate say whether a change runs and
matches its plan, not whether it is good. Before the merge, one of three
reviewers reads the issue and the diff: Claude and Codex as read-only agents
in the integration worktree (they can open any file), Azure from the diff
alone. They take turns, so each model gets to review.

A reviewer that cannot run is skipped for the next one. When none can run,
the change is not blocked: tests and the quality gate already passed, and a
missing reviewer must not stop Nexus. The result says so.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from modules.nexus_planner import _json_object
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

REVIEWERS: tuple[str, ...] = ("claude", "codex", "azure")
VERDICT_APPROVE = "approve"
VERDICT_BLOCK = "block"

_DIFF_CHARS = 60000
_AGENT_TIMEOUT_SECONDS = 900
_AGENT_STALL_SECONDS = 300


@dataclass(slots=True)
class ReviewResult:
    reviewer: str
    verdict: str
    summary: str

    @property
    def blocks(self) -> bool:
        return self.verdict == VERDICT_BLOCK


def review_prompt(goal: str, diff: str, *, can_read_files: bool, gate_findings: str = "",
                  earlier_findings: str = "") -> str:
    look = (
        "You are in the repository with the change applied and may read any file "
        "to check the change in context.\n\n"
        if can_read_files else ""
    )
    return (
        "You review a change that Nexus, a self-developing agent, made to its own "
        "code before it is merged. Its tests already pass.\n\n" + look +
        "Block it only for a real problem: it does not do what the task asks, it "
        "has a bug, it weakens security or safety checks, it deletes or changes "
        "unrelated behaviour, or it adds something dangerous. Style and small "
        "improvements are not a reason to block.\n\n"
        "When you block, list EVERY blocking problem you find, not only the "
        "first: the change gets one more attempt and must be able to fix all of "
        "them at once. Check the failure paths too (a call that raises, a "
        "partial write, a retry).\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"verdict": "approve" or "block", "summary": "<two or three sentences: '
        'what the change does>", "problems": ["<each blocking problem: what is '
        'wrong, where, and what to change>", ...]}\n'
        '"problems" is empty when you approve.\n\n'
        + (
            "QUALITY GATE FINDINGS: a deterministic check that reads file paths "
            "out of the task text flagged this change: "
            f"{gate_findings[:2000]}\n"
            "Decide whether this is a real gap (the task needs that file or "
            "change and it is missing: block and say exactly what to add) or a "
            "false alarm (an example path, a file the task only mentions, or a "
            "task that asks for tests only: approve and say why).\n\n"
            if gate_findings else ""
        )
        + (
            "FOLLOW-UP REVIEW: you blocked an earlier attempt at this task with "
            f"these findings:\n{earlier_findings[:4000]}\n"
            "First check each of them against this new attempt. Block again if "
            "one of them is not fixed. Block for a NEW problem only when it is "
            "serious: it breaks existing behaviour, loses data, weakens a "
            "security, safety or budget check, or can loop. Mention smaller new "
            "findings in the summary without blocking.\n\n"
            if earlier_findings else ""
        )
        + f"TASK:\n{goal[:8000]}\n\nDIFF:\n{diff[:_DIFF_CHARS]}"
    )


def parse_review(raw: str) -> tuple[str, str] | None:
    data = _json_object(raw or "")
    if data is None:
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    summary = " ".join(str(data.get("summary") or "").split())[:1500]
    if verdict not in (VERDICT_APPROVE, VERDICT_BLOCK) or not summary:
        return None
    problems = data.get("problems")
    if verdict == VERDICT_BLOCK and isinstance(problems, list):
        items = [" ".join(str(p).split()) for p in problems if str(p).strip()]
        if items:
            # Every finding in one reason, so the retry can fix them all.
            listed = " ".join(f"({i}) {p[:600]}" for i, p in enumerate(items[:8], 1))
            summary = f"{summary} Blocking problems: {listed}"[:4000]
    return verdict, summary


class NexusReviewer:
    """Pick the next reviewer in turn and ask it about one change."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        runners: dict[str, Callable[[str, str], str]] | None = None,
    ) -> None:
        self.db_path = db_path
        self._runners = runners if runners is not None else {
            "claude": _run_claude,
            "codex": _run_codex,
            "azure": _run_azure,
        }
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS nexus_pr_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def _order(self) -> list[str]:
        """All reviewers, starting with the one after the last that reviewed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT reviewer FROM nexus_pr_reviews WHERE verdict != 'unreviewed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        start = (REVIEWERS.index(row[0]) + 1) % len(REVIEWERS) if row and row[0] in REVIEWERS else 0
        return [REVIEWERS[(start + i) % len(REVIEWERS)] for i in range(len(REVIEWERS))]

    def _earlier_block(self, experiment_id: str) -> tuple[str, str] | None:
        """``(reviewer, findings)`` of the latest review that blocked another
        attempt at the same issue, so that reviewer checks its own findings."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT r.reviewer, r.summary FROM nexus_pr_reviews r
                    JOIN github_experiment_links other ON other.experiment_id = r.experiment_id
                    JOIN github_experiment_links mine ON mine.issue_number = other.issue_number
                    WHERE mine.experiment_id = ? AND r.experiment_id != ? AND r.verdict = ?
                    ORDER BY r.id DESC LIMIT 1
                    """,
                    (experiment_id, experiment_id, VERDICT_BLOCK),
                ).fetchone()
        except sqlite3.Error:
            return None
        return (str(row[0]), str(row[1])) if row and row[0] in REVIEWERS else None

    def review(
        self, *, experiment_id: str, goal: str, worktree_path: str, base_sha: str,
        gate_findings: str = "",
    ) -> ReviewResult:
        diff = _diff(worktree_path, base_sha)
        result = None
        order = self._order()
        earlier = self._earlier_block(experiment_id)
        if earlier is not None:
            # A retry goes back to the reviewer that blocked it: a new reviewer
            # each time found new edge cases until the attempts ran out.
            order = [earlier[0], *[name for name in order if name != earlier[0]]]
        for name in order:
            runner = self._runners.get(name)
            if runner is None:
                continue
            prompt = review_prompt(
                goal, diff, can_read_files=name != "azure", gate_findings=gate_findings,
                earlier_findings=earlier[1] if earlier is not None and name == earlier[0] else "",
            )
            try:
                parsed = parse_review(runner(prompt, worktree_path))
            except Exception as exc:  # noqa: BLE001 - try the next reviewer
                logger.warning("review: %s could not review: %s", name, exc)
                continue
            if parsed is None:
                logger.warning("review: %s gave no usable verdict", name)
                continue
            result = ReviewResult(name, *parsed)
            break
        if result is None:
            result = ReviewResult("none", "unreviewed", "No reviewer could run; merged on tests and the quality gate.")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO nexus_pr_reviews (experiment_id, reviewer, verdict, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (experiment_id, result.reviewer, result.verdict, result.summary,
                 datetime.now(timezone.utc).isoformat()),
            )
        return result


def default_reviewer(db_path: str = DEFAULT_DB_PATH) -> NexusReviewer | None:
    """The production reviewer; ``NEXUS_PR_REVIEW=off`` turns it off."""
    if os.environ.get("NEXUS_PR_REVIEW", "").strip().lower() == "off":
        return None
    return NexusReviewer(db_path=db_path)


def _diff(worktree_path: str, base_sha: str) -> str:
    proc = subprocess.run(
        ["git", "-C", worktree_path, "diff", f"{base_sha}...HEAD"] if base_sha
        else ["git", "-C", worktree_path, "show", "HEAD"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return proc.stdout


def _run_claude(prompt: str, worktree_path: str) -> str:
    from modules.claude_worker import ClaudeWorker

    result = ClaudeWorker().run(
        task=prompt, working_dir=worktree_path, sandbox="read-only",
        timeout_seconds=_AGENT_TIMEOUT_SECONDS, stall_seconds=_AGENT_STALL_SECONDS, phase="review",
    )
    if not result.success:
        raise RuntimeError(result.error or "claude review failed")
    return result.final_message or ""


def _run_codex(prompt: str, worktree_path: str) -> str:
    from modules.codex_worker import CodexWorker

    result = CodexWorker().run(
        task=prompt, working_dir=worktree_path, sandbox="read-only",
        timeout_seconds=_AGENT_TIMEOUT_SECONDS, stall_seconds=_AGENT_STALL_SECONDS, phase="review",
    )
    if not result.success:
        raise RuntimeError(result.error or "codex review failed")
    return result.final_message or ""


def _run_azure(prompt: str, worktree_path: str) -> str:
    import asyncio

    from modules.nexus_delegation import NexusDelegationService

    service = NexusDelegationService()
    try:
        result = asyncio.run(service.azure_chat(prompt, max_tokens=1500)).to_dict()
    finally:
        service.close()
    if not result.get("success"):
        raise RuntimeError(str((result.get("metadata") or {}).get("error") or "azure failed"))
    return str(result.get("content") or "")


def review_comment(result: ReviewResult) -> str:
    verdict = {"approve": "approved", "block": "blocked"}.get(result.verdict, result.verdict)
    return f"**Nexus review ({result.reviewer}): {verdict}**\n\n{result.summary}"


__all__ = [
    "NexusReviewer", "ReviewResult", "REVIEWERS", "default_reviewer",
    "parse_review", "review_comment", "review_prompt",
]
