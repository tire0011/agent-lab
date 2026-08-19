"""Multi-Provider Orchestrator.

Coordinates the strictly-sequential execution pipeline used when
``--use-router`` is active.  Each step returns control to Nexus before the
next role/provider is selected.

Pipeline
--------
1. **Router** (local model) — decides *provider* (local / azure_openai / codex)
   and *role* (developer by default).  Roles and providers are independent:
   any role can run on any provider.
2. **Context Gatherer** (local model) — reads relevant files, git log, and
   state; packages a compact context string for the downstream steps.
3. **Planner** (local model) — breaks the task into ordered sub-tasks with
   per-sub-task provider hints.
4. **Developer** (chosen provider) — implements each sub-task.  External
   providers receive only the prepared context, not the whole repo.
5. **Verifier** (real tools: git diff + test runner) — produces an objective
   pass/fail result.  No LLM involvement at this stage.
6. **Reviewer** (local model) — interprets the real verification output and
   returns a structured assessment.

Provider/usage events are written to SQLite so costs and routing patterns can
be reported later.  The ``provider_actually_used`` column records the provider
that *ran* the sub-task, which may differ from the router hint if a fallback
was triggered.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.llama_router import LlamaRouter, RouterDecision
from modules.prompt_library import render_prompt


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ContextResult:
    """Output of the context-gathering step."""

    relevant_files: list[str]
    git_diff_summary: str
    state_summary: str
    context_package: str   # compact text sent to downstream providers
    raw_response: str = ""


@dataclass
class PlanResult:
    goal: str
    sub_tasks: list[dict[str, Any]]
    raw_response: str = ""


@dataclass
class DeveloperResult:
    files_changed: list[str]
    summary: str
    notes: str
    provider_hint: str      # provider requested by router/planner
    provider_used: str      # provider that actually ran the sub-task
    input_tokens: int = 0
    output_tokens: int = 0
    runtime_ms: int = 0
    raw_response: str = ""


@dataclass
class VerificationResult:
    """Output of the real-tools verification step (no LLM)."""

    git_diff: str
    test_output: str
    tests_passed: bool
    tests_run: int
    tests_failed: int
    error: str = ""


@dataclass
class ReviewerResult:
    """Local model's interpretation of real verification results."""

    verdict: str    # pass | pass_with_notes | fail
    issues: list[dict[str, Any]]
    suggested_tests: list[str]
    summary: str
    raw_response: str = ""


@dataclass
class OrchestratorResult:
    """Complete output of a single orchestrated run."""

    task: str
    router_decision: RouterDecision
    context: ContextResult | None = None
    plan: PlanResult | None = None
    developer_results: list[DeveloperResult] = field(default_factory=list)
    verification: VerificationResult | None = None
    reviewer_result: ReviewerResult | None = None
    final_response: str = ""
    error: str = ""


# ── Orchestrator ──────────────────────────────────────────────────────────────

class MultiProviderOrchestrator:
    """Coordinates a local model with Azure OpenAI and Codex for task execution.

    The local model acts as the persistent brain: it routes, gathers context,
    plans, and reviews.  External providers (azure_openai, codex) handle only
    the developer step when the router selects them, and receive only the
    context package prepared by the local model.

    Parameters
    ----------
    llama_client:
        Client for the local model server.
    azure_run_fn:
        Async callable ``(task: str) -> str`` that forwards the task to the
        Azure OpenAI agent and returns its final text response.
    codex_run_fn:
        Async callable ``(task: str) -> str`` that forwards the task to the
        Codex task service and returns its final text response.
    db:
        Open SQLite connection used to persist provider_usage and
        model_consultations rows.
    prompts_dir:
        Override the default prompts directory (useful in tests).
    repo_root:
        Root directory of the repository for git diff / test runner.
        Defaults to the ``REPO_ROOT`` env var or the current working directory.
    test_cmd:
        Command used to run the test suite during verification.  Defaults to
        ``python -m pytest tests/ -q --tb=short``.
    """

    def __init__(
        self,
        llama_client: LlamaCppClient | None = None,
        azure_run_fn: Any = None,
        codex_run_fn: Any = None,
        db: sqlite3.Connection | None = None,
        prompts_dir: Any = None,
        repo_root: str | None = None,
        test_cmd: list[str] | None = None,
    ) -> None:
        self._llama = llama_client or LlamaCppClient()
        self._azure_run_fn = azure_run_fn
        self._codex_run_fn = codex_run_fn
        self._db = db
        self._prompts_dir = prompts_dir
        self._repo_root = repo_root or os.getenv("REPO_ROOT", os.getcwd())
        self._test_cmd = test_cmd or [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"]
        self._router = LlamaRouter(
            client=self._llama,
            db=db,
            prompts_dir=prompts_dir,
        )

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self, task: str) -> OrchestratorResult:
        """Execute *task* through the full sequential pipeline.

        Returns an :class:`OrchestratorResult` regardless of whether the run
        succeeded or failed; callers should check ``result.error``.
        """
        result = OrchestratorResult(
            task=task,
            router_decision=RouterDecision(provider="local", role="developer"),
        )

        # ── Step 1: Route ─────────────────────────────────────────────────────
        try:
            decision = await self._router.route(task)
            result.router_decision = decision
        except Exception as exc:  # noqa: BLE001
            result.error = f"Router failed: {exc}"
            return result

        # ── Step 2: Gather context ────────────────────────────────────────────
        try:
            ctx = await self._gather_context(task)
            result.context = ctx
        except Exception as exc:  # noqa: BLE001
            result.error = f"Context gatherer failed: {exc}"
            return result

        # ── Step 3: Plan ──────────────────────────────────────────────────────
        try:
            plan = await self._plan(task, ctx.context_package)
            result.plan = plan
        except Exception as exc:  # noqa: BLE001
            result.error = f"Planner failed: {exc}"
            return result

        # ── Step 4: Develop ───────────────────────────────────────────────────
        for sub_task in plan.sub_tasks:
            sub_desc = sub_task.get("description", str(sub_task))
            # Sub-task may override provider; fall back to router decision.
            provider_hint = sub_task.get("provider_hint", decision.provider)
            t0 = time.monotonic()
            try:
                dev_result = await self._develop(
                    sub_desc, provider_hint, ctx.context_package
                )
                dev_result.runtime_ms = int((time.monotonic() - t0) * 1000)
                result.developer_results.append(dev_result)
                _log_provider_usage(self._db, task, provider_hint, dev_result.provider_used, sub_desc, dev_result.summary, dev_result.input_tokens, dev_result.output_tokens, dev_result.runtime_ms)
            except Exception as exc:  # noqa: BLE001
                result.error = f"Developer failed on sub-task {sub_desc!r}: {exc}"
                return result

        # ── Step 5: Verify (real tools) ───────────────────────────────────────
        try:
            files_changed = [f for dr in result.developer_results for f in dr.files_changed]
            verification = self._verify(files_changed=files_changed)
            result.verification = verification
        except Exception as exc:  # noqa: BLE001
            result.error = f"Verifier failed: {exc}"
            return result

        # ── Step 6: Review (local model interprets real results) ──────────────
        try:
            reviewer_result = await self._review(verification, result.developer_results)
            result.reviewer_result = reviewer_result
        except Exception as exc:  # noqa: BLE001
            result.error = f"Reviewer failed: {exc}"
            return result

        result.final_response = _build_final_response(result)
        return result

    # ── Private step implementations ──────────────────────────────────────────

    async def _gather_context(self, task: str) -> ContextResult:
        """Ask the local model to identify relevant files and prepare a context package.

        Always provides deterministic, bounded repo evidence to the model even on
        a clean worktree where ``git diff HEAD`` is empty: the tracked-file listing,
        recent git log, and git status are included so the model never infers that
        the repository is empty.
        """
        git_diff = _run_git_diff(self._repo_root)
        tracked_files = _list_tracked_files(self._repo_root, limit=100)
        git_log = _run_git_log(self._repo_root, lines=10)
        git_status = _run_git_status(self._repo_root)

        prompt = render_prompt(
            "context",
            {
                "task": task,
                "git_diff": git_diff[:2000],
                "tracked_files": tracked_files[:3000],
                "git_log": git_log[:1000],
                "git_status": git_status[:500],
            },
            prompts_dir=self._prompts_dir,
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.0, max_tokens=1024)
        try:
            data = extract_json(raw)
        except LlamaCppClientError:
            # Build a deterministic fallback context so downstream steps always have
            # repo evidence even when the model response cannot be parsed.
            fallback_context = (
                f"Task: {task}\n\n"
                f"Repository files:\n{tracked_files[:800]}\n\n"
                f"Recent commits:\n{git_log[:400]}\n\n"
                f"Uncommitted changes:\n{git_diff[:600] or '(none)'}"
            )
            return ContextResult(
                relevant_files=[],
                git_diff_summary=git_diff[:500] if git_diff else "no uncommitted changes",
                state_summary="",
                context_package=fallback_context,
                raw_response=raw,
            )
        return ContextResult(
            relevant_files=list(data.get("relevant_files") or []),
            git_diff_summary=str(data.get("git_diff_summary", "")),
            state_summary=str(data.get("state_summary", "")),
            context_package=str(data.get("context_package", f"Task: {task}")),
            raw_response=raw,
        )

    async def _plan(self, task: str, context_package: str) -> PlanResult:
        prompt = render_prompt(
            "planner",
            {"task": task, "context": context_package},
            prompts_dir=self._prompts_dir,
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.0, max_tokens=1024)
        try:
            data = extract_json(raw)
        except LlamaCppClientError:
            data = {}
        return PlanResult(
            goal=str(data.get("goal", task)),
            sub_tasks=list(data.get("sub_tasks") or [{"description": task, "provider_hint": "local"}]),
            raw_response=raw,
        )

    async def _develop(
        self, sub_task: str, provider_hint: str, context_package: str
    ) -> DeveloperResult:
        prompt = render_prompt(
            "developer",
            {"sub_task": sub_task, "context": context_package},
            prompts_dir=self._prompts_dir,
        )

        provider_actually_used: str
        raw: str

        if provider_hint in ("azure", "azure_openai", "AZURE_OPENAI") and self._azure_run_fn:
            raw = await self._azure_run_fn(prompt)
            provider_actually_used = "azure_openai"
        elif provider_hint in ("codex", "CODEX") and self._codex_run_fn:
            raw = await self._codex_run_fn(prompt)
            provider_actually_used = "codex"
        elif provider_hint in ("copilot",) and self._azure_run_fn:
            # Copilot uses the Azure OpenAI path when azure_run_fn is available.
            raw = await self._azure_run_fn(prompt)
            provider_actually_used = "copilot_via_azure"
        else:
            # Default: handle locally.
            raw = await self._llama.complete(prompt=prompt, temperature=0.1, max_tokens=2048)
            provider_actually_used = "local"

        return _parse_developer_response(raw, provider_hint=provider_hint, provider_used=provider_actually_used)

    def _verify(self, files_changed: list[str] | None = None) -> VerificationResult:
        """Run real verification tools and return an objective result.

        Uses ``git diff HEAD`` to capture changes.  If neither the git diff nor the
        developer-reported ``files_changed`` list indicates any modifications, the
        task is treated as read-only: the test suite is skipped and the verification
        confirms only that the worktree remained clean.  For code-changing tasks the
        configured test command is executed with a bounded timeout.

        The LLM is not involved in this step.
        """
        git_diff = _run_git_diff(self._repo_root)
        is_read_only = not git_diff.strip() and not (files_changed or [])

        if is_read_only:
            # Verify the worktree is clean and skip the test suite.
            worktree_clean = _is_worktree_clean(self._repo_root)
            skip_reason = (
                "no file changes detected (empty git diff, no files reported by developer); "
                "test suite skipped for read-only task"
            )
            return VerificationResult(
                git_diff=git_diff,
                test_output=f"[skipped] {skip_reason}",
                tests_passed=worktree_clean,
                tests_run=0,
                tests_failed=0,
                error="" if worktree_clean else "worktree is unexpectedly dirty for a read-only task",
            )

        # Code-changing task: run the test suite.
        try:
            proc = subprocess.run(
                self._test_cmd,
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=300,
            )
            test_output = (proc.stdout + proc.stderr)[:4000]
            tests_passed = proc.returncode == 0
            # Parse simple pass/fail counts from pytest output if available.
            tests_run, tests_failed = _parse_pytest_summary(proc.stdout)
        except subprocess.TimeoutExpired:
            test_output = "Test runner timed out after 300 s"
            tests_passed = False
            tests_run = 0
            tests_failed = 0
        except Exception as exc:  # noqa: BLE001
            test_output = f"Test runner error: {exc}"
            tests_passed = False
            tests_run = 0
            tests_failed = 0

        return VerificationResult(
            git_diff=git_diff[:3000],
            test_output=test_output,
            tests_passed=tests_passed,
            tests_run=tests_run,
            tests_failed=tests_failed,
        )

    async def _review(
        self,
        verification: VerificationResult,
        developer_results: list[DeveloperResult],
    ) -> ReviewerResult:
        """Ask the local model to interpret real verification results."""
        files_changed = [f for dr in developer_results for f in dr.files_changed]
        prompt = render_prompt(
            "reviewer",
            {
                "git_diff": verification.git_diff[:2000],
                "test_output": verification.test_output[:2000],
                "tests_passed": str(verification.tests_passed),
                "tests_run": str(verification.tests_run),
                "tests_failed": str(verification.tests_failed),
                "files_changed": "\n".join(files_changed) or "(none reported)",
            },
            prompts_dir=self._prompts_dir,
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.0, max_tokens=1024)
        return _parse_reviewer_response(raw, verification)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_git_diff(repo_root: str) -> str:
    """Return ``git diff HEAD`` output, or empty string on error."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout[:4000]
    except Exception:  # noqa: BLE001
        return ""


def _list_tracked_files(repo_root: str, limit: int = 100) -> str:
    """Return a newline-joined list of tracked files (bounded by *limit*)."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = result.stdout.splitlines()
        truncated = lines[:limit]
        suffix = f"\n... ({len(lines) - limit} more)" if len(lines) > limit else ""
        return "\n".join(truncated) + suffix
    except Exception:  # noqa: BLE001
        return ""


def _run_git_log(repo_root: str, lines: int = 10) -> str:
    """Return the last *lines* one-line git log entries."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={lines}", "--oneline"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _run_git_status(repo_root: str) -> str:
    """Return ``git status --short`` output."""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip() or "(clean)"
    except Exception:  # noqa: BLE001
        return ""


def _is_worktree_clean(repo_root: str) -> bool:
    """Return True when the worktree has no uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip() == ""
    except Exception:  # noqa: BLE001
        return True  # assume clean on error


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """Extract (tests_run, tests_failed) from pytest summary line."""
    # Match: "3 failed, 10 passed" or "13 passed"
    m = re.search(r"(\d+) failed", output)
    failed = int(m.group(1)) if m else 0
    m2 = re.search(r"(\d+) passed", output)
    passed = int(m2.group(1)) if m2 else 0
    total = passed + failed
    return total, failed


def _parse_developer_response(raw: str, provider_hint: str, provider_used: str) -> DeveloperResult:
    try:
        data = extract_json(raw)
    except LlamaCppClientError:
        return DeveloperResult(
            files_changed=[],
            summary=raw[:500],
            notes="",
            provider_hint=provider_hint,
            provider_used=provider_used,
            raw_response=raw,
        )
    return DeveloperResult(
        files_changed=list(data.get("files_changed") or []),
        summary=str(data.get("summary", raw[:300])),
        notes=str(data.get("notes", "")),
        provider_hint=provider_hint,
        provider_used=provider_used,
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
        raw_response=raw,
    )


def _parse_reviewer_response(raw: str, verification: VerificationResult) -> ReviewerResult:
    # The objective pass/fail comes from verification tools, not the LLM.
    objective_verdict = "pass" if verification.tests_passed else "fail"
    try:
        data = extract_json(raw)
    except LlamaCppClientError:
        return ReviewerResult(
            verdict=objective_verdict,
            issues=[],
            suggested_tests=[],
            summary=raw[:500],
            raw_response=raw,
        )
    # Honour LLM's pass_with_notes only when tests actually passed.
    llm_verdict = str(data.get("verdict", objective_verdict))
    if not verification.tests_passed:
        # Tests failed: override to fail regardless of LLM opinion.
        verdict = "fail"
    elif llm_verdict == "pass_with_notes":
        verdict = "pass_with_notes"
    else:
        verdict = objective_verdict

    return ReviewerResult(
        verdict=verdict,
        issues=list(data.get("issues") or []),
        suggested_tests=list(data.get("suggested_tests") or []),
        summary=str(data.get("summary", "")),
        raw_response=raw,
    )


def _build_final_response(result: OrchestratorResult) -> str:
    lines = [
        f"Task: {result.task}",
        f"Router: provider={result.router_decision.provider} "
        f"role={result.router_decision.role} "
        f"(confidence={result.router_decision.confidence:.2f})",
        f"Reasoning: {result.router_decision.reasoning}",
        "",
    ]
    if result.context:
        lines.append(f"Context files: {', '.join(result.context.relevant_files) or '(none)'}")
        lines.append("")

    if result.plan:
        lines.append(f"Goal: {result.plan.goal}")
        lines.append(f"Sub-tasks: {len(result.plan.sub_tasks)}")
        lines.append("")

    for i, dev in enumerate(result.developer_results, start=1):
        lines.append(
            f"[Dev {i}/{len(result.developer_results)}] "
            f"hint={dev.provider_hint} actual={dev.provider_used} — {dev.summary}"
        )

    if result.verification:
        v = result.verification
        lines.append("")
        lines.append(
            f"Verification: tests_passed={v.tests_passed} "
            f"({v.tests_run} run, {v.tests_failed} failed)"
        )

    if result.reviewer_result:
        lines.append(f"Reviewer verdict: {result.reviewer_result.verdict}")
        lines.append(result.reviewer_result.summary)

    return "\n".join(lines)


# ── SQLite helpers ────────────────────────────────────────────────────────────
# Note: the router_decisions, provider_usage, and model_consultations tables
# are created by modules/run_store.py (_migrate_router_tables). The orchestrator
# relies on run_store.init_db() having been called before use.


def _log_provider_usage(
    db: sqlite3.Connection | None,
    task: str,
    provider_hint: str,
    provider_actually_used: str,
    sub_task: str,
    summary: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    runtime_ms: int = 0,
) -> None:
    if db is None:
        return
    db.execute(
        """
        INSERT INTO provider_usage
            (timestamp, task, provider, provider_actually_used,
             sub_task, summary, input_tokens, output_tokens, runtime_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            task,
            provider_hint,
            provider_actually_used,
            sub_task,
            summary,
            input_tokens,
            output_tokens,
            runtime_ms,
        ),
    )
    db.commit()


def _log_consultation(
    db: sqlite3.Connection | None,
    parent_task: str,
    query: str,
    reply: str,
) -> None:
    if db is None:
        return
    db.execute(
        """
        INSERT INTO model_consultations (timestamp, parent_task, query, reply)
        VALUES (?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(), parent_task, query, reply),
    )
    db.commit()
