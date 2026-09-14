"""Agentic local-LLM controller for free-form Nexus work.

The local model remains in control across tasks, issues, ideas and discussions.
It can inspect Nexus state, search persistent memory, consult other LLMs and
then decide what Nexus should do next. Existing pipelines remain tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.heartbeat import Heartbeat, worker_id as heartbeat_worker_id
from modules.local_llm_jobs import (
    LocalLlmJobError,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.mailbox.development import process_command
from modules.nexus_delegation import NexusDelegationService
from modules.nexus_memory import NexusMemory
from modules.nexus_task_store import (
    STATUS_FAILED,
    TERMINAL_REASON_TURN_CANCELLED,
    TERMINAL_REASON_TURN_FAILED,
    NexusTaskStore,
)
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESUME_EXPERIMENT,
    EXPERIMENT_ID_PATTERN,
    MAX_EXPERIMENT_ID_CANDIDATES,
    MAX_RESOLVER_CANDIDATES,
    RESUME_INTENT_PATTERN,
    TOOL_ACCURACY_CORRECT,
    TOOL_ACCURACY_INCORRECT,
    TOOL_ACCURACY_UNKNOWN,
    TOOL_FIND_TOOL,
    TOOL_RESOLVER_RESPONSE_FORMAT,
    TOOL_SEARCH_STOP_WORDS,
    NexusToolRouter,
    ToolResolverUnavailableError,
    ToolRouterError,
    invalid_find_tool_goal,
    resolver_prompt,
    selection_correctness,
    terminal_not_found_guidance,
    terminal_not_found_observation,
    tokens as tool_search_tokens,
)
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetPlan,
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
    truncate_text,
)
from modules.redaction import redact_secrets
from modules.run_store import DEFAULT_DB_PATH
from modules.runtime_execution import (
    RuntimeExecutionError,
    build_python_command,
    classify_command_failure,
    corrected_command,
    resolve_python_interpreter,
)
from modules.tool_loop_guard import PersistentToolLoopGuard

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_COMMAND = "command"
TOOL_PYTHON_RUN = "python_run"
ACTION_ANSWER = "answer"
ACTION_CLARIFICATION = "clarification"

# Delegates the local controller may fall back to when its compacted prompt
# still exceeds the safe local budget. ``local_worker`` is deliberately
# excluded: it also calls the local llama-server and carries the same OOM
# risk this fallback exists to avoid.
_EXTERNAL_FALLBACK_ORDER: tuple[str, ...] = ("azure_openai", "codex_readonly")


_FINAL_ACTIONS = {
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
}

_ORCHESTRATOR_TOOLS = {
    TOOL_FIND_TOOL,
    ACTION_CALL_TOOL,
    "memory_search",
    "delegate",
    ACTION_COMMAND,
    TOOL_PYTHON_RUN,
}

logger = logging.getLogger(__name__)

_PROVIDER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "local_worker": ("local_worker", "azure_openai", "codex_readonly"),
    "azure_openai": ("azure_openai", "local_worker", "codex_readonly"),
    "codex_readonly": ("codex_readonly", "azure_openai", "local_worker"),
}

_ORCHESTRATOR_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    TOOL_FIND_TOOL,
                    ACTION_CALL_TOOL,
                    "memory_search",
                    "delegate",
                    ACTION_COMMAND,
                    TOOL_PYTHON_RUN,
                    ACTION_NEW_EXPERIMENT,
                    ACTION_RESUME_EXPERIMENT,
                    ACTION_ANSWER,
                    ACTION_MISSING_CAPABILITY,
                    ACTION_CLARIFICATION,
                ],
            },
            "goal": {"type": "string"},
            "tool": {"type": "string"},
            "arguments": {"type": "object", "additionalProperties": True},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "provider": {"type": "string"},
            "task": {"type": "string"},
            "context": {"type": "string"},
            "command": {"type": "string"},
            "script": {"type": "string"},
            "module": {"type": "string"},
            "argv": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "experiment_id": {"type": ["string", "null"]},
            "message": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

# The shared tool mechanism owns the resolver schema, the read-only
# boundary, candidate scoring and the not-found metadata (#172). Re-exported
# here under their historical names so existing importers keep working.
# There is deliberately no read-only denylist to re-export any more: each
# capability declares whether it mutates state (#174 review).
_TOOL_RESOLVER_RESPONSE_FORMAT = TOOL_RESOLVER_RESPONSE_FORMAT
_MAX_RESOLVER_CANDIDATES = MAX_RESOLVER_CANDIDATES
_TOOL_ACCURACY_UNKNOWN = TOOL_ACCURACY_UNKNOWN
_TOOL_ACCURACY_CORRECT = TOOL_ACCURACY_CORRECT
_TOOL_ACCURACY_INCORRECT = TOOL_ACCURACY_INCORRECT
_TOOL_SEARCH_STOP_WORDS = TOOL_SEARCH_STOP_WORDS


@dataclass(slots=True)
class TaskDecision:
    action: str
    reason: str = ""
    message: str = ""
    experiment_id: str | None = None
    command: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None


class TaskOrchestratorError(RuntimeError):
    pass


class ControllerBudgetExceededError(TaskOrchestratorError):
    """Raised when a compacted controller prompt still exceeds the safe local
    budget and no external provider is available/succeeds.

    Callers must translate this into a controlled, non-crashing decision
    (``missing_capability``/needs_review) rather than invoking llama-server.
    """


_EXPERIMENT_ID_PATTERN = EXPERIMENT_ID_PATTERN
_MAX_EXPERIMENT_ID_CANDIDATES = MAX_EXPERIMENT_ID_CANDIDATES
_RESUME_INTENT_PATTERN = RESUME_INTENT_PATTERN


class _TaskStoreGrantLedger:
    """``find_tool`` grants as persistent controller-session events.

    The controller's grants are already durable: every tool observation of a
    turn is appended to the session's event log, so the ledger only has to
    read them back. Recording is therefore a no-op here - writing a second
    copy would create a grant record that could disagree with the session
    history it is supposed to describe.
    """

    def __init__(self, task_store: NexusTaskStore) -> None:
        self.task_store = task_store

    def record_grant(self, session_id: str, observation: dict[str, Any]) -> None:
        del session_id, observation

    def latest_grant(self, session_id: str, operation: str) -> dict[str, Any] | None:
        if not session_id or not operation:
            return None
        recent_events = getattr(self.task_store, "recent_events", None)
        if not callable(recent_events):
            return None
        for event in recent_events(
            session_id,
            role="tool",
            kind=TOOL_FIND_TOOL,
            limit=50,
        ):
            try:
                payload = json.loads(event["content"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("status") != "found":
                continue
            schema = payload.get("operation")
            if not isinstance(schema, dict):
                continue
            if str(schema.get("operation", "")).strip() != operation:
                continue
            return {
                "event_id": int(event["id"]),
                "operation": operation,
                "schema": schema,
            }
        return None


class NexusTaskOrchestrator:
    """Persistent local-LLM controller that chooses Nexus's next action."""

    def __init__(
        self,
        *,
        operations_registry: Any,
        experiment_service: Any,
        llama_client: LlamaCppClient | None = None,
        memory: NexusMemory | None = None,
        delegation_service: NexusDelegationService | None = None,
        task_store: NexusTaskStore | None = None,
        llm_job_service: LocalLlmJobService | None = None,
        max_steps: int = 6,
        max_total_steps: int | None = None,
        max_identical_calls: int | None = None,
        max_non_progressing_cycles: int | None = None,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        job_watchdog = float(os.environ.get("NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS", "3600"))
        job_watchdog = max(300.0, min(job_watchdog, 7200.0))
        self.llama = llama_client or LlamaCppClient(timeout=job_watchdog)
        self.max_steps = max(1, min(max_steps, 10))
        db_path = getattr(operations_registry, "db_path", DEFAULT_DB_PATH)
        self.loop_guard = PersistentToolLoopGuard(
            db_path=db_path,
            max_total_steps=max_total_steps or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_TOTAL_STEPS", "20")),
            max_identical_calls=max_identical_calls or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_IDENTICAL_CALLS", "3")),
            max_non_progressing_cycles=max_non_progressing_cycles or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_CYCLES", "2")),
        )
        # The shared local-LLM execution path (#167): controller inference and
        # every other local-model caller (failure analysis included) queue on
        # one worker per database, so they serialize instead of racing
        # llama-server with independent requests.
        self.llm_jobs = llm_job_service or acquire_local_llm_job_service(
            db_path=db_path, llama_client=llama_client
        )
        self._owns_llm_jobs = llm_job_service is None
        if llama_client is None and llm_job_service is None:
            # One client object for the whole process, owned by the shared
            # job service - including its watchdog timeout.
            self.llama = self.llm_jobs.llama
        repo_root = str(getattr(operations_registry, "repo_root", "/agent"))
        self.task_store = task_store or NexusTaskStore(db_path=db_path)
        self._owns_task_store = task_store is None
        self.memory = memory or NexusMemory(db_path=db_path, repo_root=repo_root, task_store=self.task_store)
        self._owns_memory = memory is None
        # Handed the orchestrator's own job service rather than just its
        # client: local delegation is another role on the same queue (#167),
        # so it can never open a second request path to llama-server.
        self.delegation = delegation_service or NexusDelegationService(
            llama_client=self.llama,
            db_path=db_path,
            repo_root=repo_root,
            llm_job_service=self.llm_jobs,
        )
        self._owns_delegation = delegation_service is None
        # The one shared tool mechanism (#172): capability discovery and
        # read-only capability execution live in NexusToolRouter, so the
        # controller and every other Nexus LLM role drive the same catalog,
        # the same resolver, the same grant rule and the same registry-side
        # validation/redaction/audit path.
        self.tool_router = NexusToolRouter(
            operations_registry=operations_registry,
            experiment_service=experiment_service,
            schema_completion=self._router_schema_completion,
            grant_ledger=_TaskStoreGrantLedger(self.task_store),
            caller="local task orchestrator",
        )
        # session_id -> the worker id this process currently holds the claim
        # under. Every status write during a claimed turn is scoped to it, so a
        # worker that was reaped or taken over stops being able to drive the
        # session it no longer owns. One worker drives a session at a time, so
        # a plain dict keyed by session is enough.
        self._claims: dict[str, str] = {}

    def _claim_owner(self, session_id: str) -> str | None:
        """The worker id this process holds the session under, if it does."""
        return self._claims.get(session_id)

    def _claimed_status(self, session_id: str, status: str) -> None:
        """Set a status, but only while this process still owns the session."""
        owner = self._claim_owner(session_id)
        if owner is None:
            # Unreachable from a claimed turn - ``run_async`` refuses to start
            # one without a claim - so this is a bug guard, not a fallback. It
            # fails closed: an unowned write is the thing being prevented.
            logger.warning("refusing unowned status write for session %s", session_id)
            return
        self.task_store.set_status(session_id, status, worker_id=owner)

    def close(self) -> None:
        self.loop_guard.close()
        if self._owns_llm_jobs:
            release_local_llm_job_service(self.llm_jobs)
        if self._owns_delegation:
            self.delegation.close()
        if self._owns_memory:
            self.memory.close()
        if self._owns_task_store:
            self.task_store.close()

    def run(
        self,
        task: str,
        *,
        source_type: str = "task",
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        return asyncio.run(
            self.run_async(
                task,
                source_type=source_type,
                source_id=source_id,
                title=title,
                session_id=session_id,
                task_logger=task_logger,
            )
        )

    def continue_session(
        self,
        session_id: str,
        user_message: str,
        *,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        session = self.task_store.get(session_id)
        if session is None:
            raise TaskOrchestratorError(f"unknown Nexus task session {session_id!r}")
        return self.run(
            user_message,
            source_type=session.source_type,
            source_id=session.source_id,
            title=session.title,
            session_id=session.id,
            task_logger=task_logger,
        )

    async def run_async(
        self,
        task: str,
        *,
        source_type: str = "task",
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        if not task or not task.strip():
            raise TaskOrchestratorError("task must not be empty")
        session = self.task_store.get_or_create(
            source_type=source_type,
            source_id=source_id,
            title=title,
            session_id=session_id,
        )
        # Claim the session and report this worker alive for as long as the
        # turn runs (#162). A turn can legitimately spend minutes inside one
        # local inference call without writing anything, so the heartbeat - not
        # ``updated_at`` - is what says the work still has an owner. If this
        # process dies mid-turn the beats simply stop and the session is
        # reconciled explicitly instead of sitting `active` forever.
        #
        # The claim is a prerequisite, not bookkeeping around one: treating a
        # failure here as survivable would be worse than never claiming, since
        # every later write then falls back to "no owner" and the ownership
        # rules switch themselves off exactly when persistence is already
        # misbehaving. Refusing is the safe direction - the session keeps the
        # state it has, and any worker that does own it keeps it.
        worker = heartbeat_worker_id()
        try:
            claimed = self.task_store.claim_session(session.id, worker_id=worker)
        except Exception as exc:
            raise TaskOrchestratorError(
                f"could not claim Nexus task session {session.id!r}: {exc}"
            ) from exc
        if not claimed:
            raise TaskOrchestratorError(
                f"could not claim Nexus task session {session.id!r}: "
                "the session is no longer there to claim"
            )
        self._claims[session.id] = worker
        heartbeat = Heartbeat(
            lambda: self.task_store.heartbeat(session.id, worker_id=worker),
            name=f"nexus-heartbeat-{session.id[:8]}",
        )
        heartbeat.start()
        try:
            return await self._run_claimed(session, task, task_logger=task_logger)
        except asyncio.CancelledError:
            self._abort_turn(session.id, TERMINAL_REASON_TURN_CANCELLED, "cancelled")
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            self._abort_turn(
                session.id,
                TERMINAL_REASON_TURN_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            heartbeat.stop()
            self._claims.pop(session.id, None)

    def _abort_turn(self, session_id: str, reason: str, detail: str) -> None:
        """Record a turn that died in the open, instead of letting it go quiet.

        Without this the row would keep its last status and a frozen heartbeat
        until the reaper found it, and an outcome we actually witnessed would
        end up recorded as ``heartbeat_timeout`` - "nobody ever told us" - which
        is a weaker and less true fact than "this turn raised".
        """
        owner = self._claim_owner(session_id)
        if owner is None:
            logger.warning("refusing unowned failure write for session %s", session_id)
            return
        try:
            self.task_store.finalize_turn(
                session_id,
                worker_id=owner,
                status=STATUS_FAILED,
                reason=reason,
                event_role="system",
                event_kind=reason,
                event_content=f"Controller turn ended without a decision: {detail}"[:2000],
                event_metadata={"terminal_reason": reason},
            )
        except Exception:  # noqa: BLE001 - the original failure is what matters
            logger.exception("could not record failed turn for %s", session_id)

    async def _run_claimed(
        self,
        session: Any,
        task: str,
        *,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        self._claimed_status(session.id, "active")
        # This event id anchors "prior" history: persisted history is never
        # re-read past this point, since every tool observation produced by
        # this run is already tracked (and budgeted) via ``observations``
        # in-memory. Without this boundary, persisted history and in-memory
        # observations duplicate each other and the prompt grows on every step.
        run_start_event_id = self.task_store.append_event(
            session.id,
            role="user",
            kind="input",
            content=task.strip(),
            metadata={"source_type": session.source_type, "source_id": session.source_id},
        )
        observations: list[dict[str, Any]] = []

        for _ in range(self.max_steps):
            state_token = self._state_token(task)
            within_budget, step = self.loop_guard.claim_step(session.id, state_token)
            if not within_budget:
                return self._loop_limit_result(
                    session.id,
                    task,
                    observations,
                    f"persistent orchestration step limit reached ({step - 1})",
                    task_logger,
                )
            budget_plan = compute_budget(
                mem_available_kb=read_mem_available_kb(),
                swap_free_kb=read_swap_free_kb(),
                swap_total_kb=read_swap_total_kb(),
            )
            history = self.task_store.history(
                session.id,
                limit=18,
                max_chars=budget_plan.history_chars,
                max_event_id=run_start_event_id,
            )
            prompt, prompt_meta = self._build_prompt(session, task, history, observations, budget_plan)
            logger.info(
                "controller_prompt_budget session_id=%s prompt_chars=%s estimated_tokens=%s "
                "history_events=%s observation_count=%s omitted_observations=%s %s",
                session.id,
                len(prompt),
                len(prompt) // CHARS_PER_TOKEN,
                len(history),
                len(observations),
                prompt_meta["omitted_observation_indices"],
                budget_plan.as_log_fields(),
            )
            logger.info(
                "llm_provider selected provider=local role=controller session_id=%s reason=persistent_controller",
                session.id,
            )
            try:
                raw, provider_used = await self._controller_completion(
                    prompt,
                    session_id=session.id,
                    max_tokens=550,
                    budget_plan=budget_plan,
                    task_logger=task_logger,
                )
            except ControllerBudgetExceededError as exc:
                return self._budget_exceeded_result(
                    session.id, task, observations, str(exc), task_logger
                )
            if provider_used != "local":
                logger.warning(
                    "controller_external_fallback session_id=%s provider_used=%s prompt_chars=%s "
                    "safe_local_budget_chars=%s",
                    session.id,
                    provider_used,
                    len(prompt),
                    budget_plan.safe_local_budget_chars,
                )
            try:
                decision = self._parse(raw)
            except TaskOrchestratorError as first_error:
                logger.warning(
                    "controller JSON invalid session_id=%s; requesting one correction: %s",
                    session.id,
                    str(first_error).splitlines()[0][:500],
                )
                correction_prompt = self._json_correction_prompt(raw, first_error)
                try:
                    corrected_raw, _ = await self._controller_completion(
                        correction_prompt,
                        session_id=session.id,
                        max_tokens=650,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                    )
                except ControllerBudgetExceededError as exc:
                    return self._budget_exceeded_result(
                        session.id, task, observations, str(exc), task_logger
                    )
                try:
                    decision = self._parse(corrected_raw)
                except TaskOrchestratorError as correction_error:
                    self.task_store.record_controller_turn(
                        session.id,
                        provider_used=provider_used,
                        outcome="invalid_json",
                        prompt_chars=len(prompt),
                        safe_local_budget_chars=budget_plan.safe_local_budget_chars,
                        detail=str(correction_error),
                    )
                    raise TaskOrchestratorError(
                        "controller returned invalid JSON and its single correction "
                        f"retry also failed: {correction_error}"
                    ) from correction_error
            action = str(decision.get("action", "")).strip().lower()
            # What the controller itself chose. A redirect below may change the
            # action this turn executes, but the turn record has to keep saying
            # which decision the model actually produced (#158).
            controller_action = action
            turn_detail: str | None = None
            terminal_not_found = (
                self._terminal_not_found_observation(
                    str(decision.get("goal", "")).strip(), observations, state_token
                )
                if action == TOOL_FIND_TOOL
                else None
            )
            resume_experiment_id = str(
                (terminal_not_found or {}).get("resume_experiment_id") or ""
            )
            if terminal_not_found is not None and resume_experiment_id:
                # The lookup is terminal and the resolver already established -
                # against the experiment service, not against a regex - which
                # experiment this task is about. Go there directly instead of
                # letting the controller ask the same dead question again.
                turn_detail = (
                    f"repeated terminal {TOOL_FIND_TOOL} lookup redirected to "
                    f"{ACTION_RESUME_EXPERIMENT} ({resume_experiment_id})"
                )
                logger.info(
                    "find_tool terminal not_found redirected session_id=%s goal=%s "
                    "next_action=%s experiment_id=%s",
                    session.id,
                    str(decision.get("goal", ""))[:500],
                    ACTION_RESUME_EXPERIMENT,
                    resume_experiment_id,
                )
                self.task_store.append_event(
                    session.id,
                    role="system",
                    kind="find_tool_redirected",
                    content=turn_detail,
                    metadata={
                        "controller_action": controller_action,
                        "controller_reason": str(decision.get("reason", ""))[:1000],
                        "goal": str(decision.get("goal", ""))[:500],
                        "experiment_id": resume_experiment_id,
                    },
                )
                decision = {
                    "action": ACTION_RESUME_EXPERIMENT,
                    "experiment_id": resume_experiment_id,
                    "message": "",
                    "reason": (
                        "identical find_tool lookup was terminal after not_found; "
                        "using the surfaced resume_experiment capability instead"
                    ),
                }
                action = ACTION_RESUME_EXPERIMENT
                terminal_not_found = None
            # One durable row per turn (#158). Which model decided this turn was
            # previously recoverable only from a log line, so the question "does
            # the local controller produce usable decisions" could not be
            # answered without grepping the journal.
            self.task_store.record_controller_turn(
                session.id,
                provider_used=provider_used,
                outcome=controller_action or "unparsed",
                prompt_chars=len(prompt),
                safe_local_budget_chars=budget_plan.safe_local_budget_chars,
                detail=turn_detail,
            )
            if action in _ORCHESTRATOR_TOOLS:
                execution_decision = self._action_to_tool_decision(decision)
                if task_logger is not None:
                    task_logger.info(
                        "orchestration",
                        "tool_selected",
                        (
                            "orchestrator selected intermediate action "
                            f"{action}: {decision.get('reason', '')}"
                        ),
                        tool=action,
                        reason=str(decision.get("reason", "")),
                        session_id=session.id,
                    )
                tool, arguments = self._tool_identity(execution_decision)
                check = self.loop_guard.check(
                    session.id,
                    tool,
                    arguments,
                    state_token=state_token,
                    reason=str(decision.get("reason", "")),
                )
                if not check.allowed:
                    observation = check.observation or {"event": "duplicate_tool_call"}
                    if terminal_not_found is not None:
                        # The guard already refuses the repeat and counts it, so
                        # pre-empting it would only hide the repeat from the
                        # budget that is supposed to end this loop. Make its
                        # verdict actionable instead (#164).
                        observation = {
                            **observation,
                            **self._terminal_not_found_guidance(terminal_not_found),
                        }
                    logger.warning(
                        "tool loop detected session_id=%s signature=%s previous_call_id=%s recovery=%s",
                        session.id,
                        check.signature,
                        observation.get("previous_call_id"),
                        observation.get("recovery"),
                        extra={"session_id": session.id, "event": "tool_loop_detected"},
                    )
                    if task_logger is not None:
                        task_logger.warning(
                            "orchestration",
                            str(observation.get("event", "duplicate_tool_call")),
                            (
                                f"tool loop detected signature={check.signature} "
                                f"previous_call_id={observation.get('previous_call_id')} "
                                f"recovery={observation.get('recovery')}"
                            ),
                            session_id=session.id,
                            signature=check.signature,
                            previous_call_id=str(observation.get("previous_call_id", "")),
                            recovery=str(observation.get("recovery", "")),
                        )
                    if check.stop:
                        return self._loop_limit_result(
                            session.id,
                            task,
                            observations + [observation],
                            f"tool loop limit reached after {observation.get('event')}",
                            task_logger,
                        )
                else:
                    execution_decision["session_id"] = session.id
                    # find_tool decides its not_found guidance from what this
                    # task is about, so it needs the task itself.
                    execution_decision["current_task"] = task
                    observation = await self._execute_tool(
                        execution_decision,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                    )
                    if (
                        tool == TOOL_FIND_TOOL
                        and observation.get("status") == "not_found"
                    ):
                        observation = dict(observation)
                        observation["state_token"] = state_token
                    self.loop_guard.record_result(check.call_id, observation)
                event_id = self.task_store.append_event(
                    session.id,
                    role="tool",
                    kind=str(observation.get("tool", "tool")),
                    content=json.dumps(observation, separators=(",", ":")),
                    metadata={"reason": str(decision.get("reason", ""))[:1000]},
                )
                observation = dict(observation)
                observation["event_id"] = event_id
                observations.append(observation)
                continue

            if action not in _FINAL_ACTIONS:
                raise TaskOrchestratorError(f"unknown final action {action!r}")

            experiment_id = decision.get("experiment_id")
            if action == ACTION_RESUME_EXPERIMENT:
                if not experiment_id:
                    raise TaskOrchestratorError("resume_experiment requires experiment_id")
                try:
                    experiment_id = self.experiment_service.resolve_experiment(str(experiment_id)).id
                except Exception as exc:
                    raise TaskOrchestratorError(f"unknown experiment {experiment_id!r}") from exc
            else:
                experiment_id = None

            if action == ACTION_NEW_EXPERIMENT:
                logger.info(
                    "llm_provider selected provider=codex role=developer session_id=%s reason=%s",
                    session.id,
                    str(decision.get("reason", ""))[:500],
                )

            result = TaskDecision(
                action=action,
                reason=str(decision.get("reason", ""))[:1000],
                message=str(decision.get("message", ""))[:3000],
                experiment_id=experiment_id,
                observations=observations,
                session_id=session.id,
            )
            if task_logger is not None:
                task_logger.info(
                    "orchestration",
                    "route_selected",
                    f"orchestrator selected route {result.action}: {result.reason}",
                    action=result.action,
                    reason=result.reason,
                    session_id=session.id,
                )
            self._persist_final(session.id, task, result)
            return result

        result = TaskDecision(
            action=ACTION_CLARIFICATION,
            reason="local orchestrator reached its bounded decision-step limit",
            message="I need one more piece of information before I can choose a reliable action.",
            observations=observations,
            session_id=session.id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "route_selected",
                "orchestrator requested clarification after reaching its decision-step limit",
                action=result.action,
                reason=result.reason,
                session_id=session.id,
            )
        self._persist_final(session.id, task, result)
        return result

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

        Returns ``(raw_response, provider_used)``. When ``budget_plan`` shows
        the compacted prompt still exceeds the safe local budget, llama-server
        is never invoked; the compact package is delegated to an available
        external provider instead so a persistent session cannot OOM the
        local model host.
        """
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

    async def _external_fallback_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        budget_plan: "PromptBudgetPlan",
        task_logger: Any | None,
        role: str = "controller",
    ) -> tuple[str, str]:
        """Delegate a compact, still-over-budget local inference externally.

        Never falls back to ``local_worker``: that delegate also calls the
        same local llama-server and would carry the same OOM risk this
        fallback exists to avoid.
        """
        available = {item["provider"] for item in self.delegation.catalog()}
        candidates = [p for p in _EXTERNAL_FALLBACK_ORDER if p in available]
        if not candidates:
            raise ControllerBudgetExceededError(
                "compact controller prompt still exceeds the safe local budget "
                f"({len(prompt)} chars > {budget_plan.safe_local_budget_chars}) and no "
                "external provider is configured; refusing to risk a local OOM"
            )
        if role == "tool_resolver":
            task_text = (
                "Continue as the Nexus tool resolver for this turn only. "
                "Return exactly one JSON object matching the resolver "
                "schema/instructions below; no Markdown, no prose.\n\n" + prompt
            )[:16000]
        else:
            task_text = (
                "Continue as the Nexus persistent controller for this turn only. "
                "Return exactly one JSON object matching the schema/instructions below; "
                "no Markdown, no prose.\n\n" + prompt
            )[:16000]
        attempts: list[str] = []
        for provider in candidates:
            result = await self.delegation.delegate(provider, task_text, context="")
            payload = result.to_dict()
            attempts.append(provider)
            if task_logger is not None:
                task_logger.info(
                    "orchestration",
                    f"{role}_external_fallback",
                    f"provider={provider} success={payload.get('success')}",
                    session_id=session_id,
                    provider=provider,
                )
            if payload.get("success"):
                logger.info(
                    "llm_provider completed provider=%s role=%s_fallback session_id=%s "
                    "prompt_chars=%s safe_local_budget_chars=%s",
                    provider,
                    role,
                    session_id,
                    len(prompt),
                    budget_plan.safe_local_budget_chars,
                )
                return str(payload.get("content", "")), provider
        raise ControllerBudgetExceededError(
            "compact controller prompt still exceeds the safe local budget and all "
            f"external providers failed: {attempts}"
        )

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
        if budget_plan is not None and len(prompt) > budget_plan.safe_local_budget_chars:
            logger.warning(
                "%s_budget_exceeded session_id=%s prompt_chars=%s safe_local_budget_chars=%s "
                "low_memory=%s mem_available_kb=%s",
                role,
                session_id,
                len(prompt),
                budget_plan.safe_local_budget_chars,
                budget_plan.low_memory,
                budget_plan.mem_available_kb,
            )
            return await self._external_fallback_completion(
                prompt,
                session_id=session_id,
                budget_plan=budget_plan,
                task_logger=task_logger,
                role=role,
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
            raise TaskOrchestratorError(f"local LLM job failed: {exc}") from exc
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
            f"{json.dumps(_ORCHESTRATOR_RESPONSE_FORMAT['schema'], separators=(',', ':'))}\n\n"
            f"INVALID RESPONSE:\n{raw[:6000]}"
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return tool_search_tokens(text)

    def _read_only_operation_specs(self) -> list[Any]:
        return self.tool_router.read_only_operation_specs()

    def _candidate_operations(self, goal: str) -> list[Any]:
        return self.tool_router.candidate_operations(goal)

    def _latest_operation_grant(self, session_id: str, operation: str) -> dict[str, Any] | None:
        return self.tool_router.grant_ledger.latest_grant(session_id, operation)

    @staticmethod
    def _invalid_find_tool_goal(goal: str) -> tuple[bool, str | None, str | None]:
        return invalid_find_tool_goal(goal)

    def _resolver_prompt(self, goal: str, candidates: list[Any]) -> str:
        return resolver_prompt(goal, candidates)

    @staticmethod
    def _selection_correctness(
        *,
        status: str,
        selected_operation: str | None,
        expected_operation: str | None,
        expected_status: str | None,
    ) -> str:
        return selection_correctness(
            status=status,
            selected_operation=selected_operation,
            expected_operation=expected_operation,
            expected_status=expected_status,
        )

    def _resolvable_experiment_ids(self, text: str) -> list[str]:
        return self.tool_router.resolvable_experiment_ids(text)

    def _find_tool_not_found_metadata(
        self,
        task_text: str,
        goal: str,
        related_operations: list[str],
    ) -> dict[str, Any]:
        return self.tool_router.find_tool_not_found_metadata(
            task_text, goal, related_operations
        )

    @staticmethod
    def _terminal_not_found_observation(
        goal: str,
        observations: list[dict[str, Any]],
        state_token: str,
    ) -> dict[str, Any] | None:
        return terminal_not_found_observation(goal, observations, state_token)

    @staticmethod
    def _terminal_not_found_guidance(prior: dict[str, Any]) -> dict[str, Any]:
        return terminal_not_found_guidance(prior)

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
        """The controller's inference path, as the tool router expects it.

        The router owns the resolver prompt and schema; this keeps the
        controller-specific parts - a freshly computed memory budget and the
        external-provider fallback - on the controller side, and translates
        their failure modes into the router's one "resolver unavailable"
        signal.
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
                "name": TOOL_FIND_TOOL,
                "description": (
                    "Resolve one needed read-only Nexus operation by goal. Returns either "
                    "one full operation schema, invalid_goal for placeholder/generic requests, "
                    "or actionable non-retryable not_found with related operations and direct "
                    "controller capabilities; it never implements missing tools."
                ),
                "arguments": {"goal": "run a shell command in the repository"},
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
            {
                "name": "memory_search",
                "description": (
                    "Search Nexus memory: structured knowledge, previous task/dialog "
                    "history and project memory documents."
                ),
                "arguments": {"query": "string", "limit": "1..10"},
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
                    "Local development tool: execute one shell command through the "
                    "trusted development command primitive, observe its real output, "
                    "then keep control and decide again."
                ),
                "arguments": {"command": "shell command"},
            },
            {
                "name": TOOL_PYTHON_RUN,
                "description": (
                    "Run project Python deterministically: Nexus resolves its own "
                    "interpreter, so never write an interpreter name or path yourself. "
                    "Give either a repository-relative script or an importable module "
                    "plus arguments."
                ),
                "arguments": {
                    "script": "repository-relative .py path",
                    "module": "importable module name",
                    "argv": "list of arguments",
                },
            },
            {"name": ACTION_NEW_EXPERIMENT, "description": "Codex development path: start a new isolated implementation experiment for large/complex repository changes."},
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment."},
            {"name": ACTION_ANSWER, "description": "Finish directly when Nexus itself or its tool/delegate observations have solved the request."},
            {"name": ACTION_MISSING_CAPABILITY, "description": "State that no available capability can complete the request and record a structured requirement."},
            {"name": ACTION_CLARIFICATION, "description": "Pause the persistent task and ask one concise question when a material ambiguity/information gap cannot be resolved with tools or memory."},
        ]

    def _build_prompt(
        self,
        session: Any,
        current_input: str,
        history: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        budget_plan: PromptBudgetPlan,
    ) -> tuple[str, dict[str, Any]]:
        """Compact observations/current input to fit ``budget_plan`` and render.

        Prevents nested duplication (e.g. a ``memory_search`` observation
        surfacing content already present in persisted history or in an
        earlier observation of this same run) and deterministically bounds
        any individually oversized observation before the next model call.
        """
        truncated_input, input_truncated = truncate_text(current_input, budget_plan.current_input_chars)
        seen: set[str] = set()
        for event in history:
            content = str(event.get("content", "")).strip()[:400]
            if content:
                seen.add(content)
        deduped_observations = dedupe_nested_results(observations, already_seen=seen)
        compacted_observations, omitted_indices = compact_observations(
            deduped_observations,
            budget_chars=budget_plan.observation_chars,
            keep_recent_full=2,
        )
        prompt = self._prompt(session, truncated_input, history, compacted_observations)
        meta = {
            "input_truncated": input_truncated,
            "omitted_observation_indices": omitted_indices,
            "deduped_observation_count": sum(
                int(obs.get("deduped", 0)) for obs in deduped_observations if isinstance(obs, dict)
            ),
        }
        return prompt, meta

    def _prompt(self, session: Any, current_input: str, history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> str:
        return f"""You are Nexus, the LOCAL persistent controller. You decide WHAT Nexus does next.
You are not merely an intent classifier. Work on the request yourself when practical, use tools and memory, consult other LLMs when that is valuable, and keep control after every observation.

SOURCE:
{json.dumps({'type': session.source_type, 'id': session.source_id, 'title': session.title}, separators=(',', ':'))}

CURRENT INPUT:
{current_input}

PERSISTENT TASK/DISCUSSION HISTORY:
{json.dumps(history, separators=(',', ':'))}

AVAILABLE CAPABILITIES:
{json.dumps(self._capabilities(), separators=(',', ':'))}

OBSERVATIONS FROM TOOLS USED IN THIS RUN:
{json.dumps(observations, separators=(',', ':'))}

Choose exactly one next step using ONE unambiguous `action` field.
Tool discovery/execution:
{{"action":"find_tool","goal":"run a shell command in the repository","reason":"I need to inspect the repository state"}}
{{"action":"call_tool","tool":"<tool returned by find_tool>","arguments":{{...}},"reason":"..."}}
Controller-native capabilities:
{{"action":"memory_search","query":"...","limit":6,"reason":"..."}}
{{"action":"delegate","provider":"local_worker|codex_readonly|azure_openai","task":"bounded sub-problem","context":"optional","reason":"..."}}
{{"action":"command","command":"<shell command>","reason":"..."}}
{{"action":"python_run","script":"<repo-relative .py>","argv":["--version"],"reason":"..."}}
Final action:
{{"action":"new_experiment|resume_experiment|answer|missing_capability|clarification","experiment_id":null,"message":"...","reason":"..."}}

Model policy:
- `goal` for `find_tool` must describe the concrete capability needed for the current task; it must not be a generic placeholder or copied verbatim from an example.
- LOCAL is a real development path, not merely a fallback. For small/simple changes, inspect the repository and implement with the command tool directly when practical.
- Prefer LOCAL for simple analysis, small fixes, inexpensive work, and whenever external providers are unavailable or quota/token limited.
- Prefer `azure_openai` for research, difficult reasoning and medium programming work. Azure returns specialist guidance; Nexus keeps control and can apply the resulting implementation with local command tools.
- Prefer `new_experiment` (Codex implementation) for large, broad or complex repository changes where an isolated workspace-writing worker is justified.
- Do not choose Codex merely because it is strongest. Use the cheapest capable route first.
- A failed delegate is not a final failure: provider fallback is automatic. Inspect which provider actually succeeded and continue from that observation.

Rules:
- The source may be a task, GitHub issue, idea or a reply in an ongoing discussion; treat all as work to understand and advance.
- Search memory when previous project decisions, experiments, discussions or ideas may materially help; do not search mechanically.
- Use find_tool when you need an OperationsRegistry capability that has not already been resolved in this session; then invoke only the returned identifier with call_tool. Never guess concrete tool names.
- A find_tool observation with status=not_found and retryable=false is terminal for that exact goal while state is unchanged. Never repeat the identical lookup. Prefer its direct_capabilities/recommended_next_action, resolve a more specific related operation, use command/python_run, or choose missing_capability. related_operations are suggestions only and are not call_tool grants.
- Prefer cheap/local reasoning and existing tools first. Delegate only when another model materially improves a difficult analysis, review or research step.
- Delegation is a tool call, never a transfer of control. Inspect its result and decide again.
- Commands are tool calls, never final actions. Inspect command output and decide again before finishing.
- Use `python_run` for every Python execution; Nexus supplies the interpreter. Never write an interpreter name or path in a command.
- When an observation reports command_not_found, do not repeat the command and do not repeat memory searches: switch to `python_run`, another capability, or the missing_capability action.
- For an existing experiment, inspect state if useful and select resume_experiment with its real id/prefix.
- Solve small requests with reasoning/operations/commands where practical, then answer instead of creating an experiment.
- Ask clarification only when missing information materially changes the result and cannot be obtained from tools or memory; the session persists for the reply.
- If a capability is genuinely missing, choose missing_capability and describe exactly what is needed.
- Never invent tool names, provider names, operation results or experiment IDs.
- When an observation reports duplicate_tool_call, non_progressing_cycle or retry_reason_required, do not repeat it. Choose a materially different tool or a controlled final action.
- Return JSON only.
"""

    def _action_to_tool_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Translate the action-only controller protocol to internal tool dispatch.

        The outer LLM never needs the legacy type/tool/operation envelope. Native
        controller capabilities remain internal actions; OperationsRegistry calls
        always use find_tool -> call_tool.
        """
        action = str(decision.get("action", "")).strip().lower()
        normalized = dict(decision)
        if action == ACTION_CALL_TOOL:
            normalized["tool"] = "operation"
            normalized["operation"] = str(decision.get("tool", "")).strip()
            normalized["args"] = decision.get("arguments") or {}
            return normalized
        normalized["tool"] = action
        return normalized

    def _tool_identity(self, decision: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        tool = str(decision.get("tool", "")).strip()
        if tool == "operation":
            return tool, {
                "operation": str(decision.get("operation", "")).strip(),
                "args": decision.get("args") or {},
            }
        if tool == "memory_search":
            try:
                limit = int(decision.get("limit", 6))
            except (TypeError, ValueError):
                limit = 6
            return tool, {"query": str(decision.get("query", "")).strip(), "limit": limit}
        if tool == TOOL_FIND_TOOL:
            return tool, {"goal": str(decision.get("goal", "")).strip()}
        if tool == "delegate":
            return tool, {
                "provider": str(decision.get("provider", "")).strip(),
                "task": str(decision.get("task", "")).strip(),
                "context": str(decision.get("context", "")),
            }
        if tool == ACTION_COMMAND:
            return tool, {"command": str(decision.get("command", "")).strip()}
        if tool == TOOL_PYTHON_RUN:
            # The working directory changes module/script resolution, so two
            # otherwise identical python_run calls in different directories are
            # materially different calls and must not collapse into one
            # duplicate-tool-call signature.
            try:
                cwd = str(self._resolve_cwd(decision.get("cwd")))
            except TaskOrchestratorError:
                cwd = str(decision.get("cwd", "")).strip()
            return tool, {
                "script": str(decision.get("script", "")).strip(),
                "module": str(decision.get("module", "")).strip(),
                "argv": [str(item) for item in (decision.get("argv") or [])],
                "cwd": cwd,
            }
        return tool, dict(decision.get("args") or {})

    def _state_token(self, current_input: str) -> str:
        root = Path(getattr(self.operations_registry, "repo_root", "/agent"))
        repo_state = "not-a-git-repository"
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0 and head.returncode == 0:
                repo_state = f"{head.stdout.strip()}\n{completed.stdout}"
        except (OSError, subprocess.SubprocessError):
            pass
        payload = f"{current_input.strip()}\0{repo_state}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _loop_limit_result(
        self,
        session_id: str,
        task: str,
        observations: list[dict[str, Any]],
        reason: str,
        task_logger: Any | None,
    ) -> TaskDecision:
        result = TaskDecision(
            action=ACTION_CLARIFICATION,
            reason=reason,
            message="Nexus stopped a non-progressing tool loop. Choose a different tool or provide changed state before retrying.",
            observations=observations,
            session_id=session_id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "tool_loop_stopped",
                reason,
                action=result.action,
                reason=reason,
                session_id=session_id,
            )
        self._persist_final(session_id, task, result)
        return result

    def _budget_exceeded_result(
        self,
        session_id: str,
        task: str,
        observations: list[dict[str, Any]],
        reason: str,
        task_logger: Any | None,
    ) -> TaskDecision:
        """Controlled, non-crashing outcome when no provider can safely take this turn.

        The session and its full history/observations remain persisted so a
        human can resolve capacity/configuration and resume; nothing required
        to continue is silently discarded.
        """
        # Counted like any other turn: a statistic that only records the turns
        # that succeeded would answer the wrong question.
        self.task_store.record_controller_turn(
            session_id,
            provider_used="none",
            outcome="budget_exceeded",
            detail=reason,
        )
        result = TaskDecision(
            action=ACTION_MISSING_CAPABILITY,
            reason=reason,
            message=(
                "Nexus could not safely take this controller turn: the compact "
                "prompt still exceeds the safe local budget and no external "
                "provider is available. This needs human review before retrying."
            ),
            observations=observations,
            session_id=session_id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "controller_budget_exceeded",
                reason,
                action=result.action,
                reason=reason,
                session_id=session_id,
            )
        self._persist_final(session_id, task, result)
        return result

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        try:
            data = extract_json(raw)
        except LlamaCppClientError as exc:
            raise TaskOrchestratorError(str(exc)) from exc
        if not isinstance(data, dict):
            raise TaskOrchestratorError("orchestrator response must be a JSON object")

        # Temporary read compatibility for persisted tests/sessions created before
        # issue #111. The controller schema and prompt no longer expose this shape.
        response_type = str(data.get("type", "")).strip().lower()
        if response_type in {"tool", "action"}:
            legacy = dict(data)
            legacy.pop("type", None)
            if response_type == "tool":
                legacy_tool = str(data.get("tool", "")).strip()
                if not legacy_tool:
                    raise TaskOrchestratorError("tool response requires tool")
                if legacy_tool == "operation":
                    legacy["action"] = ACTION_CALL_TOOL
                    legacy["tool"] = str(data.get("operation", "")).strip()
                    legacy["arguments"] = data.get("args") or {}
                    legacy.pop("operation", None)
                    legacy.pop("args", None)
                elif legacy_tool in _ORCHESTRATOR_TOOLS:
                    legacy["action"] = legacy_tool
                else:
                    legacy["action"] = ACTION_CALL_TOOL
                    legacy["tool"] = legacy_tool
                    legacy["arguments"] = data.get("args") or {}
                    legacy.pop("args", None)
            data = legacy
        elif response_type:
            raise TaskOrchestratorError(f"unknown response type {response_type!r}")

        action = str(data.get("action", "")).strip().lower()
        if not action:
            raise TaskOrchestratorError("action response requires action")
        if action == TOOL_FIND_TOOL and not str(data.get("goal", "")).strip():
            raise TaskOrchestratorError("find_tool requires goal")
        if action == ACTION_CALL_TOOL:
            if not str(data.get("tool", "")).strip():
                raise TaskOrchestratorError("call_tool requires tool")
            if "arguments" not in data:
                raise TaskOrchestratorError("call_tool requires arguments")
            if not isinstance(data.get("arguments"), dict):
                raise TaskOrchestratorError("call_tool arguments must be an object")
        data["action"] = action
        return data

    async def _execute_tool(
        self,
        decision: dict[str, Any],
        *,
        budget_plan: PromptBudgetPlan | None = None,
        task_logger: Any | None = None,
    ) -> dict[str, Any]:
        tool = str(decision.get("tool", "")).strip()
        if tool == TOOL_FIND_TOOL:
            return await self._resolve_find_tool(
                str(decision.get("goal", "")).strip(),
                session_id=str(decision.get("session_id", "")),
                current_task=str(decision.get("current_task", "")),
                budget_plan=budget_plan,
                task_logger=task_logger,
            )
        if tool == "operation":
            return self._execute_operation(decision)
        available_operations = {spec.name for spec in self._read_only_operation_specs()}
        if tool in available_operations:
            normalized = dict(decision)
            normalized["tool"] = "operation"
            normalized["operation"] = tool
            return self._execute_operation(normalized)
        if tool == ACTION_COMMAND:
            command = str(decision.get("command", "")).strip()
            if not command:
                raise TaskOrchestratorError("command tool requires command")
            logger.info(
                "llm_provider selected provider=local role=developer reason=%s command_chars=%s",
                str(decision.get("reason", ""))[:500],
                len(command),
            )
            return self._run_command_with_recovery(command, tool=ACTION_COMMAND)
        if tool == TOOL_PYTHON_RUN:
            return self._execute_python_run(decision)
        if tool == "memory_search":
            query = str(decision.get("query", "")).strip()
            if not query:
                raise TaskOrchestratorError("memory_search requires query")
            try:
                limit = int(decision.get("limit", 6))
            except (TypeError, ValueError):
                limit = 6
            return {"tool": "memory_search", "query": query[:500], "results": self.memory.search(query, limit=limit)}
        if tool == "delegate":
            provider = str(decision.get("provider", "")).strip()
            subtask = str(decision.get("task", "")).strip()
            context = str(decision.get("context", ""))[:6000]
            if not subtask:
                raise TaskOrchestratorError("delegate requires task")
            return await self._delegate_with_fallback(
                provider,
                subtask,
                context=context,
                reason=str(decision.get("reason", "")),
            )
        raise TaskOrchestratorError(f"unknown tool {tool!r}")

    def _repo_root(self) -> Path:
        return Path(getattr(self.operations_registry, "repo_root", "/agent"))

    def _resolve_cwd(self, raw: Any) -> Path:
        repo_root = self._repo_root()
        relative = str(raw or "").strip()
        if not relative:
            return repo_root
        candidate = (repo_root / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
        root = repo_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise TaskOrchestratorError("working directory must stay inside the repository")
        return candidate

    def _run_command(self, command: str, cwd: Path) -> tuple[str | None, str | None]:
        try:
            result, _ = process_command(SimpleNamespace(body=command, meta={}), cwd, None, 0)
        except (OSError, ValueError) as exc:
            return None, str(exc)[:4000]
        return result, None

    def _run_command_with_recovery(
        self,
        command: str,
        *,
        tool: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one command and permit at most one deterministic corrected retry."""
        working_directory = cwd or self._repo_root()
        interpreter = resolve_python_interpreter(self._repo_root())
        payload: dict[str, Any] = {
            "tool": tool,
            "command": redact_secrets(command)[:2000],
            "cwd": str(working_directory),
            "interpreter": interpreter,
            "provider_used": "local",
            **(extra or {}),
        }

        result, error = self._run_command(command, working_directory)
        if error is not None:
            return {**payload, "error": error}

        failure = classify_command_failure(result)
        if failure is None:
            return {**payload, "result": result[:10000]}

        mapping = corrected_command(
            command,
            missing_executable=failure.executable,
            interpreter=interpreter,
        )
        if mapping is None:
            logger.warning(
                "command_resolution original=%s missing_executable=%s mapping=none outcome=missing_capability",
                redact_secrets(command)[:500],
                failure.executable[:120],
            )
            return {
                **payload,
                "event": "command_not_found",
                "missing_executable": failure.executable[:120],
                "detail": failure.detail,
                "result": result[:4000],
                "message": (
                    "No known runtime mapping exists for this executable. Use python_run for Python, "
                    "select another capability, or choose missing_capability. Do not repeat this command "
                    "and do not repeat memory searches for it."
                ),
            }

        corrected, mapping_name = mapping
        logger.info(
            "command_resolution original=%s missing_executable=%s mapping=%s corrected=%s",
            redact_secrets(command)[:500],
            failure.executable[:120],
            mapping_name,
            redact_secrets(corrected)[:500],
        )
        retried, retry_error = self._run_command(corrected, working_directory)
        recovery = {
            "event": "command_resolution_retry",
            "original_command": redact_secrets(command)[:2000],
            "missing_executable": failure.executable[:120],
            "mapping": mapping_name,
            "attempts": 2,
        }
        payload = {**payload, "command": redact_secrets(corrected)[:2000], "recovery": recovery}
        if retry_error is not None:
            logger.warning("command_resolution mapping=%s outcome=retry_error", mapping_name)
            return {**payload, "error": retry_error}
        logger.info(
            "command_resolution mapping=%s outcome=%s",
            mapping_name,
            "unresolved" if classify_command_failure(retried) else "recovered",
        )
        return {**payload, "result": (retried or "")[:10000]}

    def _execute_python_run(self, decision: dict[str, Any]) -> dict[str, Any]:
        argv = decision.get("argv")
        if argv is None and isinstance(decision.get("args"), list):
            argv = decision.get("args")
        if argv is not None and not isinstance(argv, list):
            raise TaskOrchestratorError("python_run argv must be a list")
        cwd = self._resolve_cwd(decision.get("cwd"))
        try:
            plan = build_python_command(
                script=decision.get("script"),
                module=decision.get("module"),
                args=argv or (),
                repo_root=self._repo_root(),
            )
        except RuntimeExecutionError as exc:
            raise TaskOrchestratorError(str(exc)) from exc

        logger.info(
            "python_run target=%s argument_count=%s cwd=%s reason=%s",
            plan.target[:200],
            len(argv or []),
            cwd,
            str(decision.get("reason", ""))[:500],
        )
        result, error = self._run_command(plan.command, cwd)
        payload: dict[str, Any] = {
            "tool": TOOL_PYTHON_RUN,
            "target": plan.target[:500],
            "command": redact_secrets(plan.command)[:2000],
            "interpreter": plan.interpreter,
            "cwd": str(cwd),
            "provider_used": "local",
        }
        if error is not None:
            return {**payload, "error": error}
        return {**payload, "result": (result or "")[:10000]}

    async def _delegate_with_fallback(
        self,
        provider: str,
        task: str,
        *,
        context: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        available = {item["provider"] for item in self.delegation.catalog()}
        if provider not in available:
            raise TaskOrchestratorError(f"delegation provider {provider!r} is not available")

        order = [p for p in _PROVIDER_FALLBACKS.get(provider, (provider,)) if p in available]
        attempts: list[dict[str, Any]] = []
        previous_error = ""

        for index, candidate in enumerate(order):
            if index == 0:
                logger.info(
                    "llm_provider selected provider=%s role=delegate reason=%s",
                    candidate,
                    reason[:500],
                )
            else:
                logger.warning(
                    "llm_provider fallback from=%s to=%s reason=%s",
                    order[index - 1],
                    candidate,
                    previous_error[:500] or "previous_provider_failed",
                )

            result = await self.delegation.delegate(candidate, task, context=context)
            payload = result.to_dict()
            attempts.append(
                {
                    "provider": candidate,
                    "success": bool(payload.get("success")),
                    "metadata": payload.get("metadata") or {},
                }
            )
            if payload.get("success"):
                logger.info(
                    "llm_provider completed provider=%s role=delegate fallback_count=%s",
                    candidate,
                    index,
                )
                return {
                    "tool": "delegate",
                    "requested_provider": provider,
                    "provider_used": candidate,
                    "fallback_count": index,
                    "attempts": attempts,
                    "result": payload,
                }

            metadata = payload.get("metadata") or {}
            previous_error = str(metadata.get("error") or "provider returned unsuccessful result")

        logger.error(
            "llm_provider exhausted requested=%s attempts=%s reason=%s",
            provider,
            [item["provider"] for item in attempts],
            previous_error[:500],
        )
        return {
            "tool": "delegate",
            "requested_provider": provider,
            "provider_used": None,
            "fallback_count": max(0, len(attempts) - 1),
            "attempts": attempts,
            "result": {
                "provider": provider,
                "success": False,
                "content": "",
                "metadata": {"error": previous_error or "all available providers failed"},
            },
        }

    def _execute_operation(self, decision: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.tool_router.execute_operation(decision)
        except ToolRouterError as exc:
            raise TaskOrchestratorError(str(exc)) from exc

    def _persist_final(self, session_id: str, task: str, decision: TaskDecision) -> None:
        if decision.action == ACTION_CLARIFICATION:
            status = "waiting_for_user"
        elif decision.action == ACTION_MISSING_CAPABILITY:
            status = "blocked_missing_capability"
        else:
            status = "action_selected"
        # Journal, status and claim release in one ownership-scoped write. The
        # turn ended on purpose, so the claim ends with a stated outcome rather
        # than by going quiet - and if this worker was taken over meanwhile,
        # none of it lands on the new owner's session (#162).
        owner = self._claim_owner(session_id)
        if owner is None:
            logger.warning("refusing unowned final write for session %s", session_id)
            return
        recorded = self.task_store.finalize_turn(
            session_id,
            worker_id=owner,
            status=status,
            reason=decision.action,
            event_role="assistant",
            event_kind=decision.action,
            event_content=decision.message or decision.reason or decision.action,
            event_metadata={
                "reason": decision.reason,
                "experiment_id": decision.experiment_id,
                "has_command": bool(decision.command),
            },
        )
        if not recorded:
            logger.warning(
                "discarding final decision for %s: the claim moved to another worker",
                session_id,
            )
            return
        if decision.action == ACTION_MISSING_CAPABILITY:
            self._record_missing_capability(task, decision)

    def _record_missing_capability(self, task: str, decision: TaskDecision) -> None:
        repo_root = getattr(self.operations_registry, "repo_root", None)
        if repo_root is None:
            return
        try:
            root = Path(repo_root) / "state" / "requirements"
            root.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            path = root / f"{now.strftime('%Y%m%d%H%M%S%f')}-missing-capability.json"
            path.write_text(
                json.dumps({"timestamp": now.isoformat(), "type": "missing_capability", "session_id": decision.session_id, "trigger_task": task[:4000], "requirement": decision.message[:3000], "reason": decision.reason[:1000], "source": "local_task_orchestrator", "status": "new"}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return
