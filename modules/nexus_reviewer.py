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

# Mistral reviews through the Vibe CLI on the owner's subscription (2026-11-26).
REVIEWERS: tuple[str, ...] = ("claude", "codex", "mistral", "azure")
VERDICT_APPROVE = "approve"
VERDICT_BLOCK = "block"

_DIFF_CHARS = 60000
_MODEL_WRITTEN_TESTS_CHARS = 10000
_AGENT_TIMEOUT_SECONDS = 900
_AGENT_STALL_SECONDS = 300


# Kinds of a blocking problem. after_merge: a step only possible once the
# change is merged and deployed (measuring the next ticks, closing other issues,
# posting a report); it never blocks (2026-11-24: #799 was blocked by all three
# reviewers for a 20-tick probe).
PROBLEM_KINDS = ("task", "bug", "regression", "edge_case", "elsewhere", "after_merge",
                 "unfixed", "follow_up")
# Follow-up reviews (owner rule 2026-11-25): the second and later review of an
# issue only checks that the first review's findings are fixed. Only an unfixed
# finding or a regression blocks; any other new finding goes to a follow-up
# issue (2026-11-23: Codex found a new problem in every round, five blocks in
# a row).
FOLLOW_UP_BLOCKING_KINDS = ("unfixed", "regression")


@dataclass(slots=True)
class ReviewResult:
    reviewer: str
    verdict: str
    summary: str
    # (kind, problem) deferred instead of blocking; see converge().
    deferred: list[tuple[str, str]] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        return self.verdict == VERDICT_BLOCK


def review_prompt(goal: str, diff: str, *, can_read_files: bool, gate_findings: str = "",
                  earlier_findings: str = "", model_written_tests: str = "") -> str:
    if earlier_findings:
        return follow_up_prompt(goal, diff, earlier_findings, can_read_files=can_read_files)
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
        'repository or a decision by the owner; "after_merge" = a step that '
        'can only happen after this change is merged and deployed (measuring '
        'future ticks or live data, closing other issues, posting a report) - '
        'never block for it, it becomes a follow-up.\n\n'
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


def follow_up_prompt(goal: str, diff: str, findings: str, *, can_read_files: bool) -> str:
    """Second and later reviews: check the first review's findings, nothing else blocks."""
    look = ("You are in the repository with the change applied and may read any file.\n\n"
            if can_read_files else "")
    return (
        "FOLLOW-UP REVIEW of a change Nexus, a self-developing agent, made to its own "
        "code. Its tests already pass. The first review of this task blocked an earlier "
        "attempt with these findings:\n"
        f"{findings[:4000]}\n\n" + look +
        "Check ONLY whether each of these findings is fixed in this attempt. Block if one "
        "is not fixed (kind \"unfixed\"), or if the change clearly breaks existing "
        "behaviour or weakens a security, safety or budget check (kind \"regression\"). "
        "Any other new finding does NOT block: list it with kind \"follow_up\"; it "
        "becomes a follow-up issue. Steps only possible after the merge (measuring "
        "future ticks, closing other issues) are \"follow_up\" too.\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"verdict": "approve" or "block", "summary": "<one or two sentences>", '
        '"problems": [{"kind": "unfixed|regression|follow_up", "problem": "<what and where>"}]}\n\n'
        f"TASK:\n{goal[:4000]}\n\nDIFF:\n{diff[:_DIFF_CHARS]}"
    )


def _problems(problems: list, default: str = "bug") -> list[tuple[str, str]]:
    """``(kind, text)`` per problem; a plain string or unknown kind gets *default*."""
    out = []
    for item in problems:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "").strip().lower()
            text = " ".join(str(item.get("problem") or "").split())
        else:
            kind, text = default, " ".join(str(item).split())
        if text:
            out.append((kind if kind in PROBLEM_KINDS else default, text))
    return out


def parse_problems(raw: str, default: str = "bug") -> list[tuple[str, str]]:
    data = _json_object(raw or "")
    problems = data.get("problems") if data else None
    return _problems(problems, default) if isinstance(problems, list) else []


def converge(verdict: str, summary: str, problems: list[tuple[str, str]],
             follow_up: bool) -> tuple[str, str, list[tuple[str, str]]]:
    """after_merge never blocks; in a follow-up review only unfixed findings and
    regressions block. Returns (verdict, summary, deferred)."""
    if not problems:
        return verdict, summary, []
    blocking = [k for k, _t in problems
                if (k in FOLLOW_UP_BLOCKING_KINDS if follow_up else k != "after_merge")]
    deferred = [(k, t) for k, t in problems
                if (k not in FOLLOW_UP_BLOCKING_KINDS if follow_up else k == "after_merge")]
    if verdict != VERDICT_BLOCK or blocking:
        # An approval keeps its non-blocking findings as follow-ups.
        return verdict, summary, (deferred if verdict == VERDICT_APPROVE else [])
    head = summary.split(" Blocking problems:")[0]
    listed = " ".join(f"({i}) [{kind}] {text[:400]}" for i, (kind, text) in enumerate(deferred, 1))
    return (VERDICT_APPROVE,
            f"{head} Approved: no unfixed finding or regression remains; deferred to "
            f"follow-up: {listed}"[:4000],
            deferred)


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
        # db_path is bound so review runs are audited into ``runs`` with their
        # cost (#810); injected runners keep their own (test) signature.
        self._runners = runners if runners is not None else {
            "claude": lambda prompt, root: _run_claude(prompt, root, self.db_path),
            "codex": lambda prompt, root: _run_codex(prompt, root, self.db_path),
            "mistral": lambda prompt, root: _run_mistral(prompt, root, self.db_path),
            "azure": lambda prompt, root: _run_azure(prompt, root, self.db_path),
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

    def _earlier_block(self, experiment_id: str, *, first: bool = False) -> tuple[str, str] | None:
        """``(reviewer, findings)`` of the latest (or *first*) review that blocked
        another attempt at the same issue."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT r.reviewer, r.summary FROM nexus_pr_reviews r
                    JOIN github_experiment_links other ON other.experiment_id = r.experiment_id
                    JOIN github_experiment_links mine ON mine.issue_number = other.issue_number
                    WHERE mine.experiment_id = ? AND r.experiment_id != ? AND r.verdict = ?
                    ORDER BY r.id {order} LIMIT 1
                    """.format(order="ASC" if first else "DESC"),
                    (experiment_id, experiment_id, VERDICT_BLOCK),
                ).fetchone()
        except sqlite3.Error:
            return None
        return (str(row[0]), str(row[1])) if row and row[0] in REVIEWERS else None

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
        latest = earlier_review or self._earlier_block(experiment_id)
        # The findings to check are the first review's (owner rule 2026-11-25).
        first = earlier_review or self._earlier_block(experiment_id, first=True)
        order = [earlier_review[0]] if earlier_review else self._order()
        if latest is not None and earlier_review is None:
            # A retry goes to another reviewer; the latest blocking one only
            # stands in (2026-11-23: five Codex blocks in a row).
            order = [*[name for name in order if name != latest[0]], latest[0]]
        follow_up = first is not None
        for name in order:
            runner = self._runners.get(name)
            if runner is None:
                continue
            prompt = review_prompt(
                goal, diff, can_read_files=name != "azure", gate_findings=gate_findings,
                earlier_findings=first[1] if first is not None else "",
                model_written_tests=model_written_tests,
            )
            try:
                raw = runner(prompt, worktree_path)
                parsed = parse_review(raw)
            except Exception as exc:  # noqa: BLE001 - try the next reviewer
                logger.warning("review: %s could not review: %s", name, exc)
                continue
            if parsed is None:
                # 2026-11-25: azure and claude both failed on a follow-up; the
                # answer's start shows why next time.
                logger.warning("review: %s gave no usable verdict: %r", name, (raw or "")[:400])
                continue
            verdict, summary, deferred = converge(
                *parsed, parse_problems(raw, "unfixed" if follow_up else "bug"), follow_up)
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


def _run_claude(prompt: str, worktree_path: str, db_path: str | None = None) -> str:
    from modules.claude_worker import ClaudeWorker

    # db_path is passed so the review run is audited into ``runs`` with its
    # cost (#810).
    result = ClaudeWorker(db_path=db_path).run(
        task=prompt, working_dir=worktree_path, sandbox="read-only",
        timeout_seconds=_AGENT_TIMEOUT_SECONDS, stall_seconds=_AGENT_STALL_SECONDS, phase="review",
    )
    if not result.success:
        raise RuntimeError(result.error or "claude review failed")
    return result.final_message or ""


def _run_codex(prompt: str, worktree_path: str, db_path: str | None = None) -> str:
    from modules.codex_worker import CodexWorker

    result = CodexWorker(db_path=db_path).run(
        task=prompt, working_dir=worktree_path, sandbox="read-only",
        timeout_seconds=_AGENT_TIMEOUT_SECONDS, stall_seconds=_AGENT_STALL_SECONDS, phase="review",
    )
    if not result.success:
        raise RuntimeError(result.error or "codex review failed")
    return result.final_message or ""


def _run_mistral(prompt: str, worktree_path: str, db_path: str | None = None,
                 *, phase: str = "review", timeout_seconds: int = 2 * _AGENT_TIMEOUT_SECONDS) -> str:
    from modules.vibe_worker import VibeWorker, vibe_available

    if not vibe_available():
        raise RuntimeError("vibe CLI not installed")
    # Vibe is slower than the other agents (about 30 min per implementation).
    result = VibeWorker(db_path=db_path).run(
        task=prompt, working_dir=worktree_path, sandbox="read-only",
        timeout_seconds=timeout_seconds, stall_seconds=2 * _AGENT_STALL_SECONDS, phase=phase,
    )
    if not result.success:
        raise RuntimeError(result.error or "mistral review failed")
    return result.final_message or ""


# 3000, not 1500: 2026-11-25 a follow-up review answer did not fit (#834).
def _run_azure(prompt: str, worktree_path: str, db_path: str | None = None,
               *, phase: str = "review", max_tokens: int = 3000) -> str:
    import asyncio
    import os
    import time

    from modules.nexus_delegation import NexusDelegationService
    from modules.worker_audit import audit_run

    started = time.monotonic()
    with audit_run(db_path, "azure_openai", phase, prompt) as audit:
        service = NexusDelegationService()
        try:
            result = asyncio.run(service.azure_chat(prompt, max_tokens=max_tokens))
            metadata = result.metadata
            raw_usage = metadata.get("usage") or {}
            usage = {
                target: raw_usage[key] for key, target in (
                    ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                ) if type(raw_usage.get(key)) is int
            }
            error = None if result.success else str(metadata.get("error") or "azure failed")
            audit.finish(
                model=metadata.get("deployment") or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
                success=result.success, error=error, usage=usage,
                runtime_ms=round((time.monotonic() - started) * 1000),
            )
        finally:
            service.close()
        if error:
            raise RuntimeError(error)
        return result.content[:4000]


def review_comment(result: ReviewResult) -> str:
    verdict = {"approve": "approved", "block": "blocked"}.get(result.verdict, result.verdict)
    return f"**Nexus review ({result.reviewer}): {verdict}**\n\n{result.summary}"


__all__ = [
    "NexusReviewer", "ReviewResult", "REVIEWERS", "default_reviewer",
    "parse_review", "review_comment", "review_prompt",
]
