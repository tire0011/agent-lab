"""Knowledge collector of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import logging
import json
import re
from dataclasses import replace
from typing import Any
from modules.llama_cpp_client import (
    LlamaCppClientError,
    extract_json,
)
from modules.local_llm_jobs import LocalLlmJobError
from modules.escalation_probes import (
    ProbeResult,
    issue_text,
    logprob_decision,
    memory_entry_text,
    same_requirement_logprob_prompt,
)
from modules.nexus_tool_router import TOOL_FIND_TOOL
from modules.prompt_budget import (
    PromptBudgetPlan,
    compact_observations,
    prompt_json,
    truncate_text,
)
from modules.redaction import redact_secrets
from modules.task_orchestrator_parts.core import (
    ControllerBudgetExceededError,
    TOOL_KNOWLEDGE_BRIEFING,
    TaskOrchestratorError,
    _EXTERNAL_PREAMBLE_CHARS,
    _EXTERNAL_PROMPT_CHARS,
    _KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
    _KNOWLEDGE_LOOKUP_REASON,
)
logger = logging.getLogger("modules.task_orchestrator")


class KnowledgeCollectorMixin:
    """Knowledge-collector methods of :class:`NexusTaskOrchestrator`."""

    @staticmethod
    def _knowledge_request(task: str) -> str:
        """Normalize a Nexus research request for the knowledge collector."""
        stripped = task.strip()
        lowered = stripped.lower()
        match = re.search(r"github\s+issue\s+#?(\d+)", lowered)
        if match:
            return (
                f"Gather all relevant factual information about GitHub issue #{match.group(1)}. "
                "Include the issue description, known root cause, prior attempts, related decisions, "
                "constraints, and acceptance criteria when available. Do not solve or plan the task."
            )
        return (
            "Gather all relevant factual information needed to understand this task. "
            "Summarize prior context, constraints, decisions, and acceptance criteria when available. "
            "Do not solve or plan the task.\n\nTask reference:\n"
            + stripped
        )

    def _knowledge_collector_prompt(
        self,
        knowledge_request: str,
        sources: list[dict[str, Any]],
        budget_plan: PromptBudgetPlan,
        *,
        force_briefing: bool = False,
    ) -> str:
        """Render the bounded research/synthesis prompt for the knowledge collector."""
        truncated_task, _ = truncate_text(knowledge_request, budget_plan.current_input_chars)
        compacted_sources, _ = compact_observations(
            sources,
            budget_chars=budget_plan.observation_chars,
            keep_recent_full=3,
        )
        final_instruction = (
            "No more lookups are allowed in this turn. Return action=briefing now."
            if force_briefing
            else "Either request one useful lookup or return the final briefing."
        )
        return f"""You are the Nexus Knowledge Collector.

Nexus has asked you to gather knowledge needed for its current task.

Your job is research and synthesis only:
- Gather only information relevant to the research request.
- Use available read-only sources and project memory when useful.
- Combine duplicate or overlapping information and remove duplicates.
- Prefer facts already present in the collected sources over repeating a lookup.
- Do not plan the solution.
- Do not decide what Nexus should do next.
- Do not choose development tools.
- Do not modify anything, run development commands or tests, or claim the task is solved.
- Your final product is one compact factual briefing for Nexus to reason with.
- Preserve concrete facts such as root cause, affected component, constraints, prior attempts, and acceptance criteria when present.
- Keep what the current source asks to change, in its own words.
- Sources fetched now, such as a GitHub issue, are current. Memory is earlier history and may describe an older version; when they disagree, the current source wins.
- Do not invent missing facts.

For action=lookup:
- source=read_only for one concrete current source lookup such as "fetch GitHub issue #238".
- source=memory for relevant prior project discussions, decisions, or attempts.
- query must be a short information goal, not instructions to the tool. Keep it under 120 characters.
- Do not invent repository names, URLs, owners, issue metadata, or source details that are not present in TASK or COLLECTED SOURCES.
- briefing must be empty.

For action=briefing:
- source=none
- query must be empty
- briefing contains the compact factual knowledge summary.

KNOWLEDGE REQUEST:
{truncated_task}

COLLECTED SOURCES:
{prompt_json(compacted_sources)}

{final_instruction}
Return exactly one JSON object matching the schema."""

    @staticmethod
    def _parse_knowledge_collector(raw: str) -> dict[str, Any]:
        try:
            data = extract_json(raw)
        except LlamaCppClientError as exc:
            raise TaskOrchestratorError(str(exc)) from exc
        if not isinstance(data, dict):
            raise TaskOrchestratorError("knowledge collector response must be a JSON object")

        action = str(data.get("action", "")).strip().lower()
        source = str(data.get("source", "")).strip().lower()
        query = str(data.get("query", "")).strip()
        briefing = str(data.get("briefing", "")).strip()

        if action not in {"lookup", "briefing"}:
            raise TaskOrchestratorError("knowledge collector action must be lookup or briefing")
        if source not in {"none", "memory", "read_only"}:
            raise TaskOrchestratorError(
                "knowledge collector source must be none, memory or read_only"
            )
        if action == "lookup":
            if source not in {"memory", "read_only"} or not query or briefing:
                raise TaskOrchestratorError(
                    "knowledge lookup requires memory/read_only source, query and empty briefing"
                )
        else:
            if source != "none" or query or not briefing:
                raise TaskOrchestratorError(
                    "knowledge briefing requires source=none, empty query and briefing text"
                )

        return {
            "action": action,
            "source": source,
            "query": query[:500],
            "briefing": briefing[:6000],
        }

    @staticmethod
    def _mark_already_executed(
        lookup: dict[str, Any], executed: dict[str, Any]
    ) -> dict[str, Any]:
        """Say on a ``find_tool`` observation that its operation already ran.

        A lookup reporting only `resolved_tool` reads as an invitation to call it, and
        the guard then refuses a call that was never needed (#223).
        """
        return {
            **lookup,
            "already_executed": True,
            "result_in_event": executed.get("event_id"),
            "next_step": (
                "this operation has already run; read its result below "
                "and choose a different next step"
            ),
        }

    @staticmethod
    def _knowledge_collector_unavailable_briefing(exc: BaseException) -> dict[str, Any]:
        return {
            "tool": TOOL_KNOWLEDGE_BRIEFING,
            "knowledge_briefing": "",
            "collector_error": str(exc)[:1000],
            "bounded_reason": "knowledge collector unavailable; Nexus retains control",
        }

    @staticmethod
    def _fetched_issue_heading(sources: list[dict[str, Any]]) -> str | None:
        """``GitHub issue #N: <title>`` from a fetched issue, verbatim (#292)."""
        for source in sources:
            if source.get("tool") != "operation" or source.get("operation") != "github_issue":
                continue
            result = source.get("result") or {}
            if not result.get("success"):
                continue
            for line in result.get("output_lines") or []:
                if not str(line).startswith("issue: "):
                    continue
                # Read fields one by one: redaction replaces the URL and everything after it on
                # the line, so it is no longer valid JSON (#292).
                number = re.search(r'"number":\s*(\d+)', str(line))
                title = re.search(r'"title":\s*"((?:[^"\\]|\\.)*)"', str(line))
                if number and title:
                    title_text = json.loads(f'"{title.group(1)}"')
                    return f"GitHub issue #{number.group(1)}: {title_text}"
                return None
        return None

    def _external_collector_prompt(
        self,
        knowledge_request: str,
        sources: list[dict[str, Any]],
        budget_plan: PromptBudgetPlan,
        prompt: str,
    ) -> str:
        """The collector prompt sized for the external fallback (#297 review).

        The external path cuts prompts from the end, so memory is shortened first;
        compaction then shrinks the remaining sources, the current issue last.
        """
        limit = _EXTERNAL_PROMPT_CHARS - _EXTERNAL_PREAMBLE_CHARS
        overflow = len(prompt) - limit
        if overflow <= 0:
            return prompt
        memory_chars = sum(
            len(str(item.get("content", "")))
            for source in sources
            if source.get("tool") == "memory_search"
            for item in source.get("results") or []
            if isinstance(item, dict)
        )
        if memory_chars:
            keep = max(0.0, (memory_chars - overflow - 200) / memory_chars)
            sources = [
                {
                    **source,
                    "results": [
                        {
                            **item,
                            "content": truncate_text(
                                str(item.get("content", "")),
                                int(len(str(item.get("content", ""))) * keep),
                            )[0],
                        }
                        if isinstance(item, dict)
                        else item
                        for item in source.get("results") or []
                    ],
                }
                if source.get("tool") == "memory_search"
                else source
                for source in sources
            ]
            prompt = self._knowledge_collector_prompt(
                knowledge_request, sources, budget_plan, force_briefing=True
            )
        plan = budget_plan
        for _ in range(4):
            if len(prompt) <= limit:
                break
            plan = replace(
                plan,
                observation_chars=max(1000, plan.observation_chars - (len(prompt) - limit) - 200),
            )
            prompt = self._knowledge_collector_prompt(
                knowledge_request, sources, plan, force_briefing=True
            )
        return prompt

    async def _memory_matches_issue(
        self, session_id: str, sources: list[dict[str, Any]]
    ) -> ProbeResult | None:
        """Ask the local model whether memory describes the fetched issue (#297).

        The answer token's probability is the signal: each distinct memory entry is
        checked on its own and the lowest p_yes decides.
        """
        issue = ""
        entries: list[str] = []
        for source in sources:
            if source.get("tool") == "operation" and source.get("operation") == "github_issue":
                result = source.get("result") or {}
                if result.get("success"):
                    issue = issue_text(result.get("output_lines") or [])
            elif source.get("tool") == "memory_search":
                for item in source.get("results") or []:
                    # A stored task input ("solve github issue #N") names the
                    # item but describes nothing to compare.
                    if not isinstance(item, dict) or item.get("kind") == "input":
                        continue
                    text = memory_entry_text(str(item.get("content", "")))
                    if text and text not in entries:
                        entries.append(text)
        if not issue or not entries:
            return None
        results: list[ProbeResult] = []
        try:
            # Every distinct entry the briefing will see is checked - one left out could
            # carry outdated text unnoticed (#299).
            for entry in entries:
                job_id = self.llm_jobs.submit(
                    same_requirement_logprob_prompt(issue, entry),
                    temperature=0.0,
                    max_tokens=2,
                    session_id=session_id,
                    role="knowledge_collector",
                    top_logprobs=5,
                )
                results.append(logprob_decision(json.loads(await self.llm_jobs.wait(job_id))))
        except (LocalLlmJobError, ValueError, TypeError) as exc:
            logger.warning("knowledge_memory_check_failed session_id=%s error=%s", session_id, exc)
            return None
        lowest = min(results, key=lambda result: float(result.details.get("p_yes", 0.0)))
        # Each entry's answer token and alternatives, not only the lowest
        # p_yes, so a check can be retraced from the event (#301).
        probes = [
            {
                "entry": entry[:300],
                "escalate": result.escalate,
                "reason": result.reason,
                **{
                    key: result.details.get(key)
                    for key in ("token", "probability", "p_yes", "p_no", "alternatives", "top_tokens")
                },
            }
            for entry, result in zip(entries, results)
        ]
        lowest.details["entries_p_yes"] = [result.details.get("p_yes") for result in results]
        lowest.details["entries"] = probes
        return lowest

    async def _run_knowledge_collector(
        self,
        session: Any,
        task: str,
        budget_plan: PromptBudgetPlan,
        *,
        task_logger: Any | None = None,
    ) -> dict[str, Any]:
        """Collect explicit research references deterministically, then synthesize them."""
        knowledge_request = self._knowledge_request(task)
        sources: list[dict[str, Any]] = []

        issue_match = re.search(r"github\s+issue\s+#?(\d+)", task, re.IGNORECASE)
        if issue_match:
            issue_number = int(issue_match.group(1))
            goal = f"fetch GitHub issue #{issue_number}"
            state_token = self._state_token(task)
            try:
                resolved = await self._resolve_find_tool(
                    goal,
                    session_id=session.id,
                    current_task=task,
                    budget_plan=budget_plan,
                    task_logger=task_logger,
                    expected_operation="github_issue",
                    expected_status="found",
                )
                event_id = self.task_store.append_event(
                    session.id,
                    role="tool",
                    kind=TOOL_FIND_TOOL,
                    content=json.dumps(resolved, separators=(",", ":")),
                    metadata={
                        "reason": _KNOWLEDGE_LOOKUP_REASON,
                        "knowledge_collector": True,
                        "knowledge_collector_internal": True,
                        "deterministic_reference": True,
                    },
                )
                resolved = {
                    **resolved,
                    "event_id": event_id,
                    "knowledge_collector": True,
                    "deterministic_reference": True,
                    "state_token": state_token,
                }
                sources.append(resolved)
                executed = self._execute_resolved_tool(
                    session_id=session.id,
                    state_token=state_token,
                    observation=resolved,
                    reason=_KNOWLEDGE_LOOKUP_REASON,
                    task_logger=task_logger,
                )
                if executed is not None:
                    sources[-1] = self._mark_already_executed(sources[-1], executed)
                    sources.append(
                        {
                            **executed,
                            "knowledge_collector": True,
                            "deterministic_reference": True,
                        }
                    )
            except (TaskOrchestratorError, ControllerBudgetExceededError) as exc:
                sources.append(
                    {
                        "tool": "knowledge_source_error",
                        "source": "github_issue",
                        "issue_number": issue_number,
                        "error": str(exc)[:1000],
                    }
                )

            # Project history complements an explicitly referenced issue, gathered by Nexus
            # so a failed GitHub read can still be complemented without another planning loop.
            memory_query = f"GitHub issue #{issue_number}"
            try:
                memory_source = {
                    "tool": "memory_search",
                    "query": memory_query,
                    "results": self.memory.search(memory_query, limit=8),
                    "knowledge_collector": True,
                    "deterministic_reference": True,
                }
                if self._fetched_issue_heading(sources):
                    # Placed first and marked as history: the current issue text must win and survive
                    # compaction as the newest source (#292).
                    memory_source["note"] = (
                        "Earlier project history. It may describe an older version of "
                        "this issue; the current GitHub issue wins on any conflict."
                    )
                    sources.insert(0, memory_source)
                else:
                    sources.append(memory_source)
            except Exception as exc:
                sources.append(
                    {
                        "tool": "knowledge_source_error",
                        "source": "memory",
                        "query": memory_query,
                        "error": str(exc)[:1000],
                    }
                )

        if not sources:
            # Generic tasks have no deterministic extractor; the collector must summarize
            # only the request, not invent external facts.
            sources.append(
                {
                    "tool": "knowledge_request",
                    "content": knowledge_request,
                    "note": "No explicit deterministic source reference was recognized.",
                }
            )

        try:
            prompt = self._knowledge_collector_prompt(
                knowledge_request,
                sources,
                budget_plan,
                force_briefing=True,
            )
            memory_check = await self._memory_matches_issue(session.id, sources)
            escalation: dict[str, Any] = {}
            raw = ""
            provider_used = ""
            if memory_check is not None:
                escalation = {
                    "p_yes": memory_check.details.get("p_yes"),
                    "p_no": memory_check.details.get("p_no"),
                    "threshold": memory_check.details.get("threshold"),
                    "entries_p_yes": memory_check.details.get("entries_p_yes"),
                    "entries": memory_check.details.get("entries"),
                    "escalated": memory_check.escalate,
                    "reason": memory_check.reason,
                }
                logger.info(
                    "knowledge_memory_check session_id=%s escalate=%s %s entries=%s",
                    session.id,
                    memory_check.escalate,
                    memory_check.reason,
                    json.dumps(escalation["entries"], default=str)[:2000],
                )
                if task_logger is not None:
                    task_logger.info(
                        "orchestration",
                        "knowledge_memory_check",
                        f"escalate={memory_check.escalate} {memory_check.reason}",
                        session_id=session.id,
                        memory_check=escalation,
                    )
            if memory_check is not None and memory_check.escalate:
                # Memory and the current issue disagree: a stronger model writes the briefing
                # through the same external chain the controller uses on local failure (#297).
                external_attempts: list[dict[str, Any]] = []
                escalation["external_attempts"] = external_attempts
                try:
                    raw, provider_used = await self._external_fallback_completion(
                        self._external_collector_prompt(
                            knowledge_request, sources, budget_plan, prompt
                        ),
                        session_id=session.id,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                        role="knowledge_collector",
                        local_failure=f"memory differs from the current issue ({memory_check.reason})",
                        attempt_log=external_attempts,
                    )
                    # A valid answer must still be a briefing; anything else
                    # (e.g. a lookup) falls back to the local briefing (#297).
                    external_action = self._parse_knowledge_collector(raw)["action"]
                    if external_action != "briefing":
                        raise TaskOrchestratorError(
                            f"{provider_used} returned action={external_action}, not a briefing"
                        )
                except (TaskOrchestratorError, ControllerBudgetExceededError) as exc:
                    escalation["escalation_failed"] = str(exc)[:500]
                    if raw:
                        # The rejected external answer stays visible (#301).
                        escalation["rejected_answer"] = redact_secrets(raw)[:2000]
                        escalation["rejected_provider"] = provider_used
                    raw = ""
                logger.info(
                    "knowledge_escalation session_id=%s %s provider=%s failed=%s attempts=%s",
                    session.id,
                    memory_check.reason,
                    provider_used or "-",
                    escalation.get("escalation_failed", False),
                    json.dumps(external_attempts, default=str)[:3000],
                )
            if not raw:
                raw, provider_used = await self._schema_completion(
                    prompt,
                    session_id=session.id,
                    max_tokens=650,
                    response_format=_KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
                    budget_plan=budget_plan,
                    waiting_status="waiting_for_knowledge",
                    role="knowledge_collector",
                    task_logger=task_logger,
                )
            decision = self._parse_knowledge_collector(raw)
            if decision["action"] != "briefing":
                raise TaskOrchestratorError(
                    "knowledge collector must return a briefing after deterministic retrieval"
                )
            self.task_store.append_event(
                session.id,
                role="system",
                kind="knowledge_collection",
                content=json.dumps(decision, separators=(",", ":")),
                metadata={
                    "provider_used": provider_used,
                    "action": "briefing",
                    "knowledge_collector_internal": True,
                    "deterministic_retrieval": True,
                    "source_count": len(sources),
                    **({"memory_check": escalation} if escalation else {}),
                },
            )
            used_sources: list[str] = []
            for source in sources:
                if source.get("tool") == "knowledge_source_error":
                    continue
                name = (
                    source.get("operation")
                    if source.get("tool") == "operation"
                    else source.get("tool")
                )
                if name == TOOL_FIND_TOOL:
                    name = source.get("operation")
                if name and name not in used_sources:
                    used_sources.append(str(name))

            briefing = decision["briefing"]
            title = self._fetched_issue_heading(sources)
            if title and not briefing.startswith(title):
                briefing = f"{title}\n{briefing}"
            return {
                "tool": TOOL_KNOWLEDGE_BRIEFING,
                "knowledge_briefing": briefing,
                "used_sources": used_sources,
                "source_count": len(sources),
                "deterministic_retrieval": True,
            }
        except (TaskOrchestratorError, ControllerBudgetExceededError) as exc:
            return self._knowledge_collector_unavailable_briefing(exc)

