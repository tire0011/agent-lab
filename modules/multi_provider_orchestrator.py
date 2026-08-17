"""Multi-Provider Orchestrator.

Coordinates the sequential execution pipeline used when ``--use-router`` is
active:

1. **Router** (Llama 2 13B)  — decides SELF / AZURE_OPENAI / CODEX / CONSULT.
2. **Planner** (Llama 2 13B)  — breaks the task into ordered sub-tasks.
3. **Developer** (chosen provider) — implements each sub-task.
4. **Tester** (Llama 2 13B)  — validates the developer output.

Each provider_usage and model_consultation event is written to SQLite so costs
and routing patterns can be reported later.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError
from modules.llama_router import LlamaRouter, RouterDecision
from modules.prompt_library import render_prompt


# ── Result dataclasses ────────────────────────────────────────────────────────

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
    provider_used: str
    raw_response: str = ""


@dataclass
class TesterResult:
    verdict: str  # pass | pass_with_notes | fail
    issues: list[dict[str, Any]]
    suggested_tests: list[str]
    summary: str
    raw_response: str = ""


@dataclass
class OrchestratorResult:
    """Complete output of a single orchestrated run."""

    task: str
    router_decision: RouterDecision
    plan: PlanResult | None = None
    developer_results: list[DeveloperResult] = field(default_factory=list)
    tester_result: TesterResult | None = None
    final_response: str = ""
    error: str = ""


# ── Orchestrator ──────────────────────────────────────────────────────────────

class MultiProviderOrchestrator:
    """Coordinates Llama 2 13B with Azure OpenAI and Codex for task execution.

    Parameters
    ----------
    llama_client:
        Client for the local llama-cpp-python server.
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
    """

    def __init__(
        self,
        llama_client: LlamaCppClient | None = None,
        azure_run_fn: Any = None,
        codex_run_fn: Any = None,
        db: sqlite3.Connection | None = None,
        prompts_dir: Any = None,
    ) -> None:
        self._llama = llama_client or LlamaCppClient()
        self._azure_run_fn = azure_run_fn
        self._codex_run_fn = codex_run_fn
        self._db = db
        self._prompts_dir = prompts_dir
        self._router = LlamaRouter(
            client=self._llama,
            db=db,
            prompts_dir=prompts_dir,
        )
        if db is not None:
            _ensure_orchestrator_tables(db)

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self, task: str) -> OrchestratorResult:
        """Execute *task* through the full router → plan → dev → test pipeline.

        Returns an :class:`OrchestratorResult` regardless of whether the run
        succeeded or failed; callers should check ``result.error``.
        """
        result = OrchestratorResult(task=task, router_decision=RouterDecision(
            decision="SELF", confidence=0.0, reasoning=""
        ))

        # ── Step 1: Route ─────────────────────────────────────────────────────
        try:
            decision = await self._router.route(task)
            result.router_decision = decision
        except Exception as exc:  # noqa: BLE001
            result.error = f"Router failed: {exc}"
            return result

        # ── Step 1b: CONSULT (optional detour) ───────────────────────────────
        if decision.decision == "CONSULT":
            try:
                consultation_reply = await self._consult(
                    decision.consult_query or task
                )
                _log_consultation(self._db, task, decision.consult_query or task, consultation_reply)
                # Re-route with the consultation answer appended.
                enriched_task = (
                    f"{task}\n\n[Consultation result]: {consultation_reply}"
                )
                decision = await self._router.route(enriched_task)
                result.router_decision = decision
            except Exception as exc:  # noqa: BLE001
                result.error = f"Consultation failed: {exc}"
                return result

        # ── Step 2: Plan ──────────────────────────────────────────────────────
        try:
            plan = await self._plan(task)
            result.plan = plan
        except Exception as exc:  # noqa: BLE001
            result.error = f"Planner failed: {exc}"
            return result

        # ── Step 3: Develop ───────────────────────────────────────────────────
        provider = _decision_to_provider(decision.decision)
        for sub_task in plan.sub_tasks:
            sub_desc = sub_task.get("description", str(sub_task))
            provider_hint = sub_task.get("provider_hint", provider)
            try:
                dev_result = await self._develop(sub_desc, provider_hint, task)
                result.developer_results.append(dev_result)
                _log_provider_usage(self._db, task, provider_hint, sub_desc, dev_result.summary)
            except Exception as exc:  # noqa: BLE001
                result.error = f"Developer failed on sub-task {sub_desc!r}: {exc}"
                return result

        # ── Step 4: Test ──────────────────────────────────────────────────────
        try:
            developer_summary = "\n".join(
                dr.summary for dr in result.developer_results
            )
            files_changed = [
                f for dr in result.developer_results for f in dr.files_changed
            ]
            tester_result = await self._test(developer_summary, files_changed)
            result.tester_result = tester_result
        except Exception as exc:  # noqa: BLE001
            result.error = f"Tester failed: {exc}"
            return result

        result.final_response = _build_final_response(result)
        return result

    # ── Private step implementations ──────────────────────────────────────────

    async def _plan(self, task: str) -> PlanResult:
        prompt = render_prompt(
            "planner", {"task": task}, prompts_dir=self._prompts_dir
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.0, max_tokens=1024)
        data = self._llama.extract_json(raw)
        return PlanResult(
            goal=str(data.get("goal", task)),
            sub_tasks=list(data.get("sub_tasks") or [{"description": task, "provider_hint": "self"}]),
            raw_response=raw,
        )

    async def _develop(
        self, sub_task: str, provider_hint: str, context: str
    ) -> DeveloperResult:
        if provider_hint in ("azure", "AZURE_OPENAI") and self._azure_run_fn:
            prompt = render_prompt(
                "developer",
                {"sub_task": sub_task, "context": context},
                prompts_dir=self._prompts_dir,
            )
            raw = await self._azure_run_fn(prompt)
            return _parse_developer_response(raw, provider_used="azure_openai")

        if provider_hint in ("codex", "CODEX") and self._codex_run_fn:
            prompt = render_prompt(
                "developer",
                {"sub_task": sub_task, "context": context},
                prompts_dir=self._prompts_dir,
            )
            raw = await self._codex_run_fn(prompt)
            return _parse_developer_response(raw, provider_used="codex")

        # Default: handle locally with Llama.
        prompt = render_prompt(
            "developer",
            {"sub_task": sub_task, "context": context},
            prompts_dir=self._prompts_dir,
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.1, max_tokens=2048)
        return _parse_developer_response(raw, provider_used="llama_local")

    async def _test(
        self, developer_summary: str, files_changed: list[str]
    ) -> TesterResult:
        prompt = render_prompt(
            "tester",
            {
                "developer_output": developer_summary,
                "files_changed": "\n".join(files_changed) or "(none)",
            },
            prompts_dir=self._prompts_dir,
        )
        raw = await self._llama.complete(prompt=prompt, temperature=0.0, max_tokens=1024)
        return _parse_tester_response(raw)

    async def _consult(self, query: str) -> str:
        """Ask Azure OpenAI for advice when Llama is uncertain."""
        if self._azure_run_fn:
            return await self._azure_run_fn(query)
        # Fallback: Llama answers its own query (for offline scenarios).
        return await self._llama.complete(
            prompt=query, temperature=0.3, max_tokens=512
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decision_to_provider(decision: str) -> str:
    return {
        "SELF": "self",
        "AZURE_OPENAI": "azure",
        "CODEX": "codex",
        "CONSULT": "self",
    }.get(decision, "self")


def _parse_developer_response(raw: str, provider_used: str) -> DeveloperResult:
    client = LlamaCppClient()
    try:
        data = client.extract_json(raw)
    except LlamaCppClientError:
        return DeveloperResult(
            files_changed=[],
            summary=raw[:500],
            notes="",
            provider_used=provider_used,
            raw_response=raw,
        )
    return DeveloperResult(
        files_changed=list(data.get("files_changed") or []),
        summary=str(data.get("summary", raw[:300])),
        notes=str(data.get("notes", "")),
        provider_used=provider_used,
        raw_response=raw,
    )


def _parse_tester_response(raw: str) -> TesterResult:
    client = LlamaCppClient()
    try:
        data = client.extract_json(raw)
    except LlamaCppClientError:
        return TesterResult(
            verdict="pass_with_notes",
            issues=[],
            suggested_tests=[],
            summary=raw[:500],
            raw_response=raw,
        )
    return TesterResult(
        verdict=str(data.get("verdict", "pass_with_notes")),
        issues=list(data.get("issues") or []),
        suggested_tests=list(data.get("suggested_tests") or []),
        summary=str(data.get("summary", "")),
        raw_response=raw,
    )


def _build_final_response(result: OrchestratorResult) -> str:
    lines = [
        f"Task: {result.task}",
        f"Router decision: {result.router_decision.decision} "
        f"(confidence={result.router_decision.confidence:.2f})",
        f"Reasoning: {result.router_decision.reasoning}",
        "",
    ]
    if result.plan:
        lines.append(f"Goal: {result.plan.goal}")
        lines.append(f"Sub-tasks: {len(result.plan.sub_tasks)}")
        lines.append("")

    for i, dev in enumerate(result.developer_results, start=1):
        lines.append(f"[Dev {i}/{len(result.developer_results)}] "
                     f"Provider={dev.provider_used} — {dev.summary}")

    if result.tester_result:
        lines.append("")
        lines.append(f"Tester verdict: {result.tester_result.verdict}")
        lines.append(result.tester_result.summary)

    return "\n".join(lines)


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _ensure_orchestrator_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            provider TEXT NOT NULL,
            sub_task TEXT NOT NULL,
            summary TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_provider_usage_timestamp
            ON provider_usage(timestamp);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_provider
            ON provider_usage(provider);

        CREATE TABLE IF NOT EXISTS model_consultations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            parent_task TEXT NOT NULL,
            query TEXT NOT NULL,
            reply TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_consultations_timestamp
            ON model_consultations(timestamp);
        """
    )
    conn.commit()


def _log_provider_usage(
    db: sqlite3.Connection | None,
    task: str,
    provider: str,
    sub_task: str,
    summary: str,
) -> None:
    if db is None:
        return
    db.execute(
        """
        INSERT INTO provider_usage (timestamp, task, provider, sub_task, summary)
        VALUES (?, ?, ?, ?, ?)
        """,
        (datetime.now(timezone.utc).isoformat(), task, provider, sub_task, summary),
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
