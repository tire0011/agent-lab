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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from modules.nexus_planner import _json_object
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

REVIEWERS: tuple[str, ...] = ("claude", "codex", "azure")
VERDICT_APPROVE = "approve"
VERDICT_BLOCK = "block"

_DIFF_CHARS = 60000
_MODEL_WRITTEN_TESTS_CHARS = 10000
_AGENT_TIMEOUT_SECONDS = 900
_AGENT_STALL_SECONDS = 300


# Kinds of a blocking problem. From the second block on the same issue only
# BLOCKING_KINDS still block; the others go to a follow-up issue (edge_case)
# or to the owner (elsewhere) - 2026-11-22: 66 blocked vs 56 adopted, and
# issues like #782 and #763 took 3-4 rounds, each finding a new edge case.
PROBLEM_KINDS = ("task", "bug", "regression", "edge_case", "elsewhere")
BLOCKING_KINDS = ("task", "bug", "regression")
CONVERGE_AFTER_BLOCKS = 1
# From the third attempt (two earlier blocks) a new bug blocks only when it
# breaks a case the task names (2026-11-23: Codex blocked five adoptions in a
# row, each retry with a new finding).
STRICT_AFTER_BLOCKS = 2


@dataclass(slots=True)
class ReviewResult:
    reviewer: str
    verdict: str
    summary: str
    # (kind, problem) deferred instead of blocking; see CONVERGE_AFTER_BLOCKS.
    deferred: list[tuple[str, str]] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        return self.verdict == VERDICT_BLOCK


def review_prompt(goal: str, diff: str, *, can_read_files: bool, gate_findings: str = "",
                  earlier_findings: str = "", model_written_tests: str = "",
                  earlier_blocks: int = 0) -> str:
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
        'what the change does>", "problems": [{"kind": "<task|bug|regression|'
        'edge_case|elsewhere>", "problem": "<what is wrong, where, and what to '
        'change>"}, ...]}\n'
        '"problems" is empty when you approve. Kinds: "task" = the change does '
        'not do what the task asks; "bug" = the changed code is wrong in a case '
        'the task covers; "regression" = it breaks existing behaviour or weakens '
        'a security, safety or budget check; "edge_case" = a rare case beyond '
        'what the task asks; "elsewhere" = it needs a change outside this '
        'repository or a decision by the owner.\n\n'
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
            "FOLLOW-UP REVIEW: an earlier review blocked a previous attempt at this "
            f"task with these findings:\n{earlier_findings[:4000]}\n"
            "First check each of them against this new attempt. Block again if "
            "one of them is not fixed. Block for a NEW problem only when it is "
            "serious: it breaks existing behaviour, loses data, weakens a "
            "security, safety or budget check, or can loop. Mention smaller new "
            "findings in the summary without blocking.\n\n"
            if earlier_findings else ""
        )
        + (
            f"STRICT SCOPE: this task has been blocked {earlier_blocks} times already. "
            "A new problem counts as \"bug\" only when it breaks a case the TASK text "
            "explicitly names; any other new problem is an \"edge_case\", however "
            "serious it seems, unless it is a \"regression\". An earlier finding that "
            "is still not fixed keeps its kind.\n\n"
            if earlier_blocks >= STRICT_AFTER_BLOCKS else ""
        )
        + f"TASK:\n{goal[:8000]}\n\n"
        + (
            # #636: the coder got these as suggestions; they are not part of the
            # task, so a diff without them is not missing anything.
            "The coder was handed these generated tests as a suggestion. Leaving "
            "them out of the diff, or changing them, is not a reason to block; "
            "block for them only if they are in the diff and wrong.\n"
            f"{model_written_tests[:_MODEL_WRITTEN_TESTS_CHARS]}\n\n"
            if model_written_tests else ""
        )
        + f"DIFF:\n{diff[:_DIFF_CHARS]}"
    )


def _problems(problems: list) -> list[tuple[str, str]]:
    """``(kind, text)`` per problem; a plain string or unknown kind is a ``bug``."""
    out = []
    for item in problems:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "").strip().lower()
            text = " ".join(str(item.get("problem") or "").split())
        else:
            kind, text = "bug", " ".join(str(item).split())
        if text:
            out.append((kind if kind in PROBLEM_KINDS else "bug", text))
    return out


def parse_problems(raw: str) -> list[tuple[str, str]]:
    data = _json_object(raw or "")
    problems = data.get("problems") if data else None
    return _problems(problems) if isinstance(problems, list) else []


def converge(verdict: str, summary: str, problems: list[tuple[str, str]],
             earlier_blocks: int) -> tuple[str, str, list[tuple[str, str]]]:
    """From the second block on: only task, bug and regression problems block;
    edge cases and outside work are deferred. Returns (verdict, summary, deferred)."""
    if verdict != VERDICT_BLOCK or earlier_blocks < CONVERGE_AFTER_BLOCKS or not problems:
        return verdict, summary, []
    if any(kind in BLOCKING_KINDS for kind, _text in problems):
        return verdict, summary, []
    head = summary.split(" Blocking problems:")[0]
    listed = " ".join(f"({i}) [{kind}] {text[:400]}" for i, (kind, text) in enumerate(problems, 1))
    return (VERDICT_APPROVE,
            f"{head} Approved after {earlier_blocks} earlier block(s): no remaining task, bug or "
            f"regression problem; deferred to follow-up: {listed}"[:4000],
            problems)


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
        items = [text for _kind, text in _problems(problems)]
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

    def _earlier_block_count(self, experiment_id: str) -> int:
        """Blocking reviews of other attempts at the same issue."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM nexus_pr_reviews r
                    JOIN github_experiment_links other ON other.experiment_id = r.experiment_id
                    JOIN github_experiment_links mine ON mine.issue_number = other.issue_number
                    WHERE mine.experiment_id = ? AND r.experiment_id != ? AND r.verdict = ?
                    """,
                    (experiment_id, experiment_id, VERDICT_BLOCK),
                ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row else 0

    def latest_blocking_review(self, experiment_id: str) -> tuple[str, str] | None:
        """Return the persisted findings before the adoption reason's clipping."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT reviewer, summary FROM nexus_pr_reviews "
                "WHERE experiment_id = ? AND verdict = ? ORDER BY id DESC LIMIT 1",
                (experiment_id, VERDICT_BLOCK),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def review(
        self, *, experiment_id: str, goal: str, worktree_path: str, base_sha: str,
        gate_findings: str = "", model_written_tests: str = "",
        earlier_review: tuple[str, str] | None = None,
    ) -> ReviewResult:
        diff = (_diff(worktree_path, base_sha, exact_base=True) if earlier_review
                else _diff(worktree_path, base_sha))
        result = None
        earlier = earlier_review or self._earlier_block(experiment_id)
        order = [earlier_review[0]] if earlier_review else self._order()
        if earlier is not None and earlier_review is None:
            # A retry goes to another reviewer, which checks the earlier findings
            # first; the blocking one only stands in. Sending retries back to it
            # (#642) let one reviewer find a new problem in every round
            # (2026-11-23: five Codex blocks in a row). The strict scope from the
            # third attempt stops new edge cases from blocking.
            order = [*[name for name in order if name != earlier[0]], earlier[0]]
        earlier_blocks = self._earlier_block_count(experiment_id) if earlier_review is None else 0
        for name in order:
            runner = self._runners.get(name)
            if runner is None:
                continue
            prompt = review_prompt(
                goal, diff, can_read_files=name != "azure", gate_findings=gate_findings,
                # A stand-in for that reviewer checks the same findings (#642: claude
                # gave no verdict, and codex reviewed the retry from scratch).
                earlier_findings=earlier[1] if earlier is not None else "",
                model_written_tests=model_written_tests,
                earlier_blocks=earlier_blocks,
            )
            try:
                raw = runner(prompt, worktree_path)
                parsed = parse_review(raw)
            except Exception as exc:  # noqa: BLE001 - try the next reviewer
                logger.warning("review: %s could not review: %s", name, exc)
                continue
            if parsed is None:
                logger.warning("review: %s gave no usable verdict", name)
                continue
            verdict, summary, deferred = converge(*parsed, parse_problems(raw), earlier_blocks)
            result = ReviewResult(name, verdict, summary, deferred)
            break
        if result is None:
            summary = (
                "The blocking reviewer did not provide a follow-up verdict."
                if earlier_review else "No reviewer could run; merged on tests and the quality gate."
            )
            result = ReviewResult("none", "unreviewed", summary)
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


def _diff(worktree_path: str, base_sha: str, *, exact_base: bool = False) -> str:
    if exact_base:
        args = ["diff", "--no-ext-diff", "--no-textconv", base_sha, "HEAD"]
    else:
        args = ["diff", f"{base_sha}...HEAD"] if base_sha else ["show", "HEAD"]
    proc = subprocess.run(
        ["git", "-C", worktree_path, *args],
        capture_output=True, text=True, timeout=60, check=exact_base,
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
