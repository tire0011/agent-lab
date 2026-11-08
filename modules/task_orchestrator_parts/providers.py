"""Controller completion providers, external fallback and tool resolution of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import logging
import json
import os
from typing import Any
from modules.local_llm_jobs import LocalLlmJobError
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESUME_EXPERIMENT,
    FIND_TOOL_GOAL_PLACEHOLDER,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    ToolResolverUnavailableError,
    terminal_not_found_guidance,
    terminal_not_found_observation,
    tokens as tool_search_tokens,
)
from modules.prompt_budget import (
    PromptBudgetPlan,
    compute_budget,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
)
from modules.redaction import redact_secrets
from modules.tool_result_reader import TOOL_READ_TOOL_RESULT
from modules.task_orchestrator_parts.core import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_KNOWLEDGE_COLLECTOR,
    ACTION_NEW_EXPERIMENT,
    ControllerBudgetExceededError,
    TaskOrchestratorError,
    _AZURE_CONTROLLER_MAX_TOKENS,
    _CONTROLLER_PROVIDER_ENV,
    _EXTERNAL_FALLBACK_ORDER,
    _EXTERNAL_PROMPT_CHARS,
    _ORCHESTRATOR_RESPONSE_FORMAT,
    _ORCHESTRATOR_SCHEMA,
)
logger = logging.getLogger("modules.task_orchestrator")


class ProviderToolMixin:
    """Provider fallback and tool-resolution methods of :class:`NexusTaskOrchestrator`."""

    async def _controller_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        budget_plan: "PromptBudgetPlan | None" = None,
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        """Run one controller completion.

        Returns ``(raw_response, provider_used)``. External providers are used only
        after a real local inference failure.
        """
        if self._azure_drives_controller():
            self._claimed_status(session_id, "waiting_for_llm")
            try:
                result = await self.delegation.azure_chat(
                    prompt, max_tokens=_AZURE_CONTROLLER_MAX_TOKENS
                )
            finally:
                self._claimed_status(session_id, "active")
            payload = result.to_dict()
            content = str(payload.get("content") or "")
            if payload.get("success") and content.strip():
                return content, "azure_openai"
            logger.warning(
                "controller_azure_failed session_id=%s error=%s; using the local model",
                session_id,
                str((payload.get("metadata") or {}).get("error") or "empty answer")[:300],
            )
        return await self._schema_completion(
            prompt,
            session_id=session_id,
            max_tokens=max_tokens,
            response_format=_ORCHESTRATOR_RESPONSE_FORMAT,
            budget_plan=budget_plan,
            waiting_status="waiting_for_llm",
            role="controller",
            task_logger=task_logger,
        )

    def _azure_configured(self) -> bool:
        return any(item.get("provider") == "azure_openai" for item in self.delegation.catalog())

    @staticmethod
    def _controller_forced_local() -> bool:
        return os.environ.get(_CONTROLLER_PROVIDER_ENV, "").strip().lower() == "local"

    def _azure_drives_controller(self) -> bool:
        """Azure takes the controller's turns when configured (#337)."""
        if self._controller_forced_local():
            return False
        return callable(getattr(self.delegation, "azure_chat", None)) and self._azure_configured()

    async def _external_fallback_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        budget_plan: "PromptBudgetPlan",
        task_logger: Any | None,
        role: str = "controller",
        local_failure: str = "",
        attempt_log: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """Delegate externally after local inference has actually failed.

        Never falls back to ``local_worker`` - same llama-server, no recovery.
        ``attempt_log`` gets one entry per provider tried, answer or error (#301).
        """
        available = {item["provider"] for item in self.delegation.catalog()}
        candidates = [p for p in _EXTERNAL_FALLBACK_ORDER if p in available]
        if not candidates:
            raise ControllerBudgetExceededError(
                "local inference failed"
                + (f": {local_failure}" if local_failure else "")
                + "; no external provider is configured"
            )
        if role == "knowledge_collector":
            task_text = (
                "Continue as the Nexus knowledge collector for this turn only. "
                "Lookups are not possible here; write the final briefing now as exactly one "
                'JSON object: {"action":"briefing","source":"none","query":"","briefing":"..."}. '
                "No Markdown, no prose.\n\n" + prompt
            )[:_EXTERNAL_PROMPT_CHARS]
        elif role == "tool_resolver":
            task_text = (
                "Continue as the Nexus tool resolver for this turn only. "
                "Return exactly one JSON object matching the resolver "
                "schema/instructions below; no Markdown, no prose.\n\n" + prompt
            )[:_EXTERNAL_PROMPT_CHARS]
        else:
            task_text = (
                "Continue as the Nexus persistent controller for this turn only. "
                "Return exactly one JSON object matching the schema/instructions below; "
                "no Markdown, no prose.\n\n" + prompt
            )[:_EXTERNAL_PROMPT_CHARS]
        attempts: list[str] = []
        tried: list[dict[str, Any]] = []
        for provider in candidates:
            result = await self.delegation.delegate(provider, task_text, context="")
            payload = result.to_dict()
            attempts.append(provider)
            attempt = self._fallback_attempt(provider, payload)
            tried.append(attempt)
            if attempt_log is not None:
                attempt_log.append(attempt)
            # The answer or provider error is what explains an escalation afterwards;
            # success=False alone did not (#301).
            outcome = (
                f"answer={attempt.get('answer', '')[:500]!r}"
                if attempt["success"]
                else f"error={attempt.get('error', '-')} detail={attempt.get('error_detail', '')[:300]!r}"
            )
            logger.info(
                "%s_external_fallback session_id=%s provider=%s success=%s %s",
                role,
                session_id,
                provider,
                attempt["success"],
                outcome,
            )
            if task_logger is not None:
                task_logger.info(
                    "orchestration",
                    f"{role}_external_fallback",
                    f"provider={provider} success={payload.get('success')} {outcome}",
                    session_id=session_id,
                    **{key: value for key, value in attempt.items() if key != "success"},
                )
            if payload.get("success"):
                logger.info(
                    "llm_provider completed provider=%s role=%s_fallback_after_local_failure "
                    "session_id=%s "
                    "prompt_chars=%s safe_local_budget_chars=%s",
                    provider,
                    role,
                    session_id,
                    len(prompt),
                    budget_plan.safe_local_budget_chars,
                )
                return str(payload.get("content", "")), provider
        raise ControllerBudgetExceededError(
            "local inference failed"
            + (f": {local_failure}" if local_failure else "")
            + f"; all external providers failed: {attempts}"
            + "".join(
                f"; {item['provider']}: {item.get('error', '-')}"
                for item in tried
            )
        )

    @staticmethod
    def _fallback_attempt(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One provider's answer or error, bounded and redacted for logs (#301)."""
        metadata = payload.get("metadata") or {}
        attempt: dict[str, Any] = {"provider": provider, "success": bool(payload.get("success"))}
        if attempt["success"]:
            attempt["answer"] = redact_secrets(str(payload.get("content", "")))[:2000]
        else:
            attempt["error"] = redact_secrets(
                str(metadata.get("error") or payload.get("content") or "unsuccessful result")
            )[:500]
            if metadata.get("error_detail"):
                attempt["error_detail"] = redact_secrets(str(metadata["error_detail"]))[:1000]
        for key in ("http_status", "deployment", "job_id", "task_id", "status", "error_kind"):
            if metadata.get(key) not in (None, ""):
                attempt[key] = metadata[key]
        return attempt

    async def _schema_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        budget_plan: "PromptBudgetPlan | None" = None,
        waiting_status: str = "waiting_for_llm",
        role: str = "controller",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        if budget_plan is not None and (
            len(prompt) > budget_plan.safe_local_budget_chars or budget_plan.low_memory
        ):
            logger.warning(
                "%s_memory_budget_advisory session_id=%s prompt_chars=%s "
                "safe_local_budget_chars=%s low_memory=%s mem_available_kb=%s "
                "action=attempt_local_inference",
                role,
                session_id,
                len(prompt),
                budget_plan.safe_local_budget_chars,
                budget_plan.low_memory,
                budget_plan.mem_available_kb,
            )
        job_id = self.llm_jobs.submit(
            prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            session_id=session_id,
            response_format=response_format,
            role=role,
        )
        self._claimed_status(session_id, waiting_status)
        try:
            return await self.llm_jobs.wait(job_id), "local"
        except LocalLlmJobError as exc:
            failure = str(exc)[:1000]
            logger.error(
                "%s_local_inference_failed session_id=%s error_kind=%s job_id=%s error=%s; "
                "attempting external fallback",
                role,
                session_id,
                exc.kind,
                job_id,
                failure,
            )
            if task_logger is not None:
                task_logger.error(
                    "orchestration",
                    f"{role}_local_inference_failed",
                    failure,
                    session_id=session_id,
                    error_kind=exc.kind,
                    job_id=job_id,
                )
            if budget_plan is None:
                raise TaskOrchestratorError(f"local LLM job failed: {failure}") from exc
            return await self._external_fallback_completion(
                prompt,
                session_id=session_id,
                budget_plan=budget_plan,
                task_logger=task_logger,
                role=role,
                local_failure=failure,
            )
        finally:
            self._claimed_status(session_id, "active")

    @staticmethod
    def _json_correction_prompt(
        raw: str,
        error: TaskOrchestratorError,
    ) -> str:
        return (
            "Your previous Nexus controller response was not valid JSON. "
            "Correct syntax only and preserve the intended next step. "
            "Return exactly one JSON object with no Markdown or prose.\n\n"
            f"VALIDATION ERROR:\n{str(error)[:1000]}\n\n"
            f"REQUIRED JSON SCHEMA:\n"
            f"{json.dumps(_ORCHESTRATOR_SCHEMA, separators=(',', ':'))}\n\n"
            f"INVALID RESPONSE:\n{raw[:6000]}"
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return tool_search_tokens(text)

    def _read_only_operation_specs(self) -> list[Any]:
        return self.tool_router.operation_specs()

    @staticmethod
    def _terminal_not_found_observation(
        goal: str,
        observations: list[dict[str, Any]],
        state_token: str,
    ) -> dict[str, Any] | None:
        return terminal_not_found_observation(goal, observations, state_token)

    @staticmethod
    def _terminal_not_found_guidance(
        prior: dict[str, Any],
        resolved: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return terminal_not_found_guidance(prior, resolved)

    async def _resolve_find_tool(
        self,
        goal: str,
        *,
        session_id: str,
        current_task: str = "",
        budget_plan: PromptBudgetPlan | None = None,
        task_logger: Any | None = None,
        expected_operation: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one capability request through the shared tool router (#172)."""
        del budget_plan  # resolver must recompute current memory before its own inference
        return await self.tool_router.resolve_find_tool(
            goal,
            session_id=session_id,
            current_task=current_task,
            task_logger=task_logger,
            expected_operation=expected_operation,
            expected_status=expected_status,
        )

    async def _router_schema_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        role: str = "tool_resolver",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        """The controller inference path, as the tool router expects it.

        The router owns the resolver prompt and schema; the controller side keeps the
        advisory memory signal and external fallback and maps their failures to the
        router's single "resolver unavailable" signal.
        """
        budget_plan = compute_budget(
            mem_available_kb=read_mem_available_kb(),
            swap_free_kb=read_swap_free_kb(),
            swap_total_kb=read_swap_total_kb(),
        )
        try:
            return await self._schema_completion(
                prompt,
                session_id=session_id,
                max_tokens=max_tokens,
                response_format=response_format,
                budget_plan=budget_plan,
                waiting_status="waiting_for_tool_resolver",
                role=role,
                task_logger=task_logger,
            )
        except ControllerBudgetExceededError as exc:
            raise ToolResolverUnavailableError(
                str(exc)[:500], provider="none", reason=str(exc)[:500]
            ) from exc
        except TaskOrchestratorError as exc:
            raise ToolResolverUnavailableError(str(exc)[:500], provider="local") from exc

    def _capabilities(self) -> list[dict[str, Any]]:
        legacy_catalog_chars = len(
            json.dumps(
                [spec.to_dict() for spec in self.operations_registry.catalog()],
                separators=(",", ":"),
            )
        )
        return [
            {
                "name": ACTION_KNOWLEDGE_COLLECTOR,
                "description": (
                    "Gather additional factual context when it would materially help Nexus "
                    "understand or solve the current task. The collector may use read-only "
                    "sources and project memory, deduplicates what it finds, and returns one "
                    "compact factual briefing. It does not plan, implement, or choose Nexus' "
                    "next action."
                ),
                "arguments": {
                    "request": "focused research request describing what Nexus needs to know",
                },
            },
            {
                "name": "delegate",
                "description": (
                    "Ask another bounded LLM worker for analysis/research/review. "
                    "Provider failures automatically fall back to another available "
                    "model and control then returns to Nexus."
                ),
                "providers": self.delegation.catalog(),
                "arguments": {
                    "provider": "provider name",
                    "task": "sub-problem",
                    "context": "optional compact context",
                },
            },
            {
                "name": ACTION_COMMAND,
                "description": (
                    "Execute one trusted local shell command for machine work or for reading "
                    "the repository, observe its real output, then keep control and decide "
                    "again. Never use it to change repository code."
                ),
                "arguments": {"command": "shell command"},
            },
            {
                "name": TOOL_PYTHON_RUN,
                "description": (
                    "Run project Python deterministically: Nexus resolves its own "
                    "interpreter, so never write an interpreter name or path yourself. "
                    "Give either a repository-relative script or an importable module "
                    "plus arguments. Use it for machine work or for reading the "
                    "repository. Never use it to change repository code."
                ),
                "arguments": {
                    "script": "repository-relative .py path",
                    "module": "importable module name",
                    "argv": "list of arguments",
                },
            },
            {
                "name": TOOL_READ_TOOL_RESULT,
                "description": (
                    "Read one range of a tool result of this run that was too large for the "
                    "prompt and is shown only as a summary or stub with its event_id. "
                    "Returns content[offset:offset+limit], total_chars and next_offset; "
                    "limit is capped. Read the next range with offset=next_offset. Only "
                    "tool results of this session are readable."
                ),
                "arguments": {
                    "event_id": "event_id of a tool observation of this run",
                    "offset": "character offset to start at, 0 for the beginning",
                    "limit": "number of characters to read (capped)",
                },
            },
            {
                "name": ACTION_NEW_EXPERIMENT,
                "description": (
                    "Development path for every repository code change, small ones "
                    "included: start a new isolated implementation experiment. What you "
                    "already found in this run is handed over with your goal."
                ),
                "arguments": {
                    "goal": "the concrete change to make and why",
                    "complexity": "small for a clear change in one or two files; otherwise large",
                },
            },
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment. Only for an experiment this task started or the user explicitly asked to resume."},
            {"name": ACTION_ANSWER, "description": "Finish directly when Nexus itself or its tool/delegate observations have solved the request."},
            {"name": ACTION_MISSING_CAPABILITY, "description": "State that no available capability can complete the request and record a structured requirement."},
            {"name": ACTION_CLARIFICATION, "description": "Pause the persistent task and ask one concise question when a material ambiguity/information gap cannot be resolved with tools or memory."},
            {
                "name": TOOL_FIND_TOOL,
                "description": (
                    "Resolve one needed read-only Nexus operation by goal. Returns either "
                    "one full operation schema, invalid_goal for placeholder/generic requests, "
                    "or a non-retryable not_found carrying described related operations "
                    "(operation, description, why_relevant, arguments) and the direct "
                    "controller capabilities that own the goal instead; it never implements "
                    "missing tools and never resolves shell or Python execution."
                ),
                # A description, never a usable goal: the local model copies whatever concrete
                # goal the prompt shows (#218).
                "arguments": {"goal": FIND_TOOL_GOAL_PLACEHOLDER},
                "legacy_catalog_schema_chars": legacy_catalog_chars,
            },
            {
                "name": ACTION_CALL_TOOL,
                "description": (
                    "Invoke exactly one concrete read-only Nexus operation only after "
                    "find_tool returned its schema in this persistent session."
                ),
                "arguments": {
                    "tool": "tool identifier returned by find_tool",
                    "arguments": "object matching the returned argument schema",
                },
            },
        ]

