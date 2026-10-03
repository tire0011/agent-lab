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
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
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
from modules.escalation_probes import (
    ProbeResult,
    issue_text,
    logprob_decision,
    memory_entry_text,
    same_requirement_logprob_prompt,
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
from modules.llama_cpp_client import json_schema_response_format
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESOLVE_RELATED_OPERATION,
    ACTION_RESUME_EXPERIMENT,
    EXPERIMENT_ID_PATTERN,
    FIND_TOOL_GOAL_EXAMPLE,
    FIND_TOOL_GOAL_PLACEHOLDER,
    MAX_EXPERIMENT_ID_CANDIDATES,
    MAX_RESOLVER_CANDIDATES,
    RESUME_INTENT_PATTERN,
    TOOL_ACCURACY_CORRECT,
    TOOL_ACCURACY_INCORRECT,
    TOOL_ACCURACY_UNKNOWN,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    TOOL_RESOLVER_RESPONSE_FORMAT,
    TOOL_SEARCH_STOP_WORDS,
    NexusToolRouter,
    ToolResolverUnavailableError,
    ToolRouterError,
    controller_native_capabilities,
    invalid_find_tool_goal,
    operation_already_resolved,
    related_operation_lookup_goal,
    resolver_prompt,
    selection_correctness,
    terminal_not_found_guidance,
    terminal_not_found_observation,
    tokens as tool_search_tokens,
)
from modules.controller_observations import _args_signature, controller_observation_view
from modules.experiment_handover import experiment_handover
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetPlan,
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    prompt_json,
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
    exit_code_of,
    resolve_python_interpreter,
)
from modules.tool_loop_guard import PersistentToolLoopGuard

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_ANSWER = "answer"
ACTION_CLARIFICATION = "clarification"
ACTION_KNOWLEDGE_COLLECTOR = "knowledge_collector"

# Delegates the local controller may fall back to after a local inference
# failure. ``local_worker`` is deliberately excluded because it uses the same
# local llama-server and cannot recover from that failure.
_EXTERNAL_FALLBACK_ORDER: tuple[str, ...] = ("azure_openai", "codex_readonly")
# External fallback prompts are cut to this length, so callers that cannot
# lose their tail size the prompt below it first (#297).
_EXTERNAL_PROMPT_CHARS = 16000
# Room for the role preamble that _external_fallback_completion puts in front.
_EXTERNAL_PREAMBLE_CHARS = 300
# Distinct memory entries checked one by one against a fetched issue (#297).
# Each check is one short local call (about 10-25 s on the VM).
_MEMORY_CHECK_MAX_ENTRIES = 4


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
    ACTION_KNOWLEDGE_COLLECTOR,
    "delegate",
    ACTION_COMMAND,
    TOOL_PYTHON_RUN,
}

logger = logging.getLogger(__name__)

# Decision fields worth journalling. Not the whole decision: `message` can
# be a page of prose and adds nothing to "why did it choose this".
_DECISION_FIELDS = (
    "tool", "goal", "query", "request", "operation", "provider", "command",
    "script", "module", "experiment_id", "capability",
)

_PROVIDER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "local_worker": ("local_worker", "azure_openai", "codex_readonly"),
    "azure_openai": ("azure_openai", "local_worker", "codex_readonly"),
    "codex_readonly": ("codex_readonly", "azure_openai", "local_worker"),
}

_ORCHESTRATOR_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    TOOL_FIND_TOOL,
                    ACTION_CALL_TOOL,
                    "memory_search",
                    ACTION_KNOWLEDGE_COLLECTOR,
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
            "request": {"type": "string"},
            "limit": {"type": "integer"},
            "provider": {"type": "string"},
            "task": {"type": "string"},
            "context": {"type": "string"},
            "command": {"type": "string"},
            "script": {"type": "string"},
            "module": {"type": "string"},
            "argv": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "effect": {"type": "string", "enum": ["read", "change", "verify"]},
            "experiment_id": {"type": ["string", "null"]},
            "message": {"type": "string"},
            "verified_noop": {"type": "boolean"},
            "reason": {"type": "string"},
            "information_gap": {"type": "string"},
            "gap_kind": {
                "type": "string",
                "enum": [
                    "user_intent",
                    "user_preference",
                    "user_authorization",
                    "external_information",
                    "missing_capability",
                ],
            },
            "evidence_event_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": 10,
            },
            "why_human_required": {"type": "string"},
            "capability": {"type": "string"},
            "required_inputs": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
        },
    "required": ["action"],
    "additionalProperties": False,
}
_ORCHESTRATOR_RESPONSE_FORMAT: dict[str, Any] = json_schema_response_format(
    "nexus_controller_decision", _ORCHESTRATOR_SCHEMA
)


# Nexus receives only the collector's final compact factual knowledge product.
TOOL_KNOWLEDGE_BRIEFING = ACTION_KNOWLEDGE_COLLECTOR

_KNOWLEDGE_LOOKUP_REASON = "knowledge collector lookup"

_KNOWLEDGE_COLLECTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["lookup", "briefing"]},
        "source": {"type": "string", "enum": ["none", "memory", "read_only"]},
        "query": {"type": "string", "maxLength": 120},
        "briefing": {"type": "string", "maxLength": 5000},
    },
    "required": ["action", "source", "query", "briefing"],
    "additionalProperties": False,
}
_KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT: dict[str, Any] = json_schema_response_format(
    "nexus_knowledge_collector", _KNOWLEDGE_COLLECTOR_SCHEMA
)

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
    capability: str | None = None
    required_inputs: list[str] = field(default_factory=list)
    # What Nexus already learned, handed to a new_experiment with the task so
    # the coding agent does not begin from the bare task text (#263).
    experiment_handover: str | None = None


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

    def resume_available_capabilities(self, *, limit: int = 1) -> list[TaskDecision]:
        """Resume blocked sessions whose exact registry capability newly appeared."""
        limit = max(1, min(limit, 20))
        resumed: list[TaskDecision] = []
        current_catalog = {
            str(getattr(spec, "name", "")).strip()
            for spec in self.operations_registry.catalog()
            if str(getattr(spec, "name", "")).strip()
        }
        for blocker in self.task_store.blocked_missing_capabilities():
            capability = str(blocker["capability"]).strip()
            catalog_at_block = {
                str(name).strip()
                for name in blocker.get("catalog_at_block") or []
                if str(name).strip()
            }
            if capability not in current_catalog or capability in catalog_at_block:
                continue
            session = blocker["session"]
            try:
                decision = asyncio.run(
                    self._resume_blocked_session_async(
                        session,
                        str(blocker["trigger_task"]),
                        capability,
                        int(blocker["blocker_event_id"]),
                    )
                )
            except TaskOrchestratorError as exc:
                if "could not claim Nexus task session" in str(exc):
                    continue
                raise
            resumed.append(decision)
            if len(resumed) >= limit:
                break
        return resumed

    async def _resume_blocked_session_async(
        self,
        session: Any,
        task: str,
        capability: str,
        blocker_event_id: int,
    ) -> TaskDecision:
        worker = heartbeat_worker_id()
        if not self.task_store.claim_blocked_missing_capability(
            session.id, worker_id=worker
        ):
            raise TaskOrchestratorError(
                f"could not claim Nexus task session {session.id!r} for capability resume"
            )
        self._claims[session.id] = worker
        self._apply_completion_requirement(session, task)
        heartbeat = Heartbeat(
            lambda: self.task_store.heartbeat(session.id, worker_id=worker),
            name=f"nexus-heartbeat-{session.id[:8]}",
        )
        heartbeat.start()
        try:
            return await self._run_claimed(
                session,
                task,
                resume_capability=capability,
                blocker_event_id=blocker_event_id,
            )
        except asyncio.CancelledError:
            self._restore_missing_capability_block(
                session.id,
                capability,
                "resume cancelled",
            )
            raise
        except BaseException as exc:
            self._restore_missing_capability_block(
                session.id,
                capability,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            heartbeat.stop()
            self._claims.pop(session.id, None)

    def _apply_completion_requirement(self, session: Any, task: str) -> None:
        """Record that *task* owes completion evidence, once this worker owns it.

        Called only after the claim succeeds: writing the requirement first
        would mutate a session another worker holds whenever the claim then
        fails.

        The key is refreshed on every new implementation request, so a
        follow-up that is not itself a request ("yes, go ahead") keeps the
        original requirement together with the evidence already gathered for
        it, while a genuinely new request arriving in the same session starts
        from no evidence.
        """
        if not self._is_implementation_intent(task):
            return
        intent_key = self._intent_key(task)
        if session.completion_intent_key == intent_key:
            return
        self.task_store.set_requires_completion_evidence(
            session.id, True, intent_key=intent_key
        )
        session.requires_completion_evidence = True
        session.completion_intent_key = intent_key

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
        self._apply_completion_requirement(session, task)
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

    def resume_after_experiment(
        self, experiment_id: str, result: dict[str, Any]
    ) -> list[TaskDecision]:
        """Continue every session parked on *experiment_id* with its result (#263).

        Called the moment the experiment ends, and by the polling safety net
        after a restart lost that moment; the claim makes sure only one of
        them continues a session.
        """
        resumed: list[TaskDecision] = []
        for session in self.task_store.sessions_waiting_for_experiments(experiment_id):
            worker = heartbeat_worker_id()
            if not self.task_store.claim_waiting_for_experiment(
                session.id, experiment_id, worker_id=worker
            ):
                continue
            inputs = self.task_store.recent_events(
                session.id, role="user", kind="input", limit=1
            )
            task = str(inputs[0]["content"]) if inputs else (session.title or "")
            self._claims[session.id] = worker
            heartbeat = Heartbeat(
                lambda sid=session.id: self.task_store.heartbeat(sid, worker_id=worker),
                name=f"nexus-heartbeat-{session.id[:8]}",
            )
            heartbeat.start()
            try:
                resumed.append(
                    asyncio.run(
                        self._run_claimed(session, task, experiment_result=dict(result))
                    )
                )
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
        return resumed

    def _restore_missing_capability_block(
        self,
        session_id: str,
        capability: str,
        detail: str,
    ) -> None:
        owner = self._claim_owner(session_id)
        if owner is None:
            return
        self.task_store.finalize_turn(
            session_id,
            worker_id=owner,
            status="blocked_missing_capability",
            reason=ACTION_MISSING_CAPABILITY,
            event_role="system",
            event_kind="capability_resume_failed",
            event_content=detail[:2000],
            event_metadata={"capability": capability},
        )

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
        resume_capability: str | None = None,
        blocker_event_id: int | None = None,
        experiment_result: dict[str, Any] | None = None,
    ) -> TaskDecision:
        self._claimed_status(session.id, "active")
        if experiment_result is not None:
            if experiment_result.get("status") == "succeeded":
                # The experiment changed the code and passed its verification;
                # without this the controller's answer is rejected for lack of
                # completion evidence, turn after turn (#263).
                experiment_result = dict(experiment_result)
                experiment_result["completion_evidence"] = self._record_evidence(
                    session, kind="change", source=ACTION_NEW_EXPERIMENT
                )
            run_start_event_id = self.task_store.append_event(
                session.id,
                role="tool",
                kind=ACTION_NEW_EXPERIMENT,
                content=json.dumps(experiment_result, separators=(",", ":")),
                metadata={"experiment_id": experiment_result.get("experiment_id")},
            )
        elif resume_capability:
            run_start_event_id = self.task_store.append_event(
                session.id,
                role="system",
                kind="capability_available",
                content=f"Capability now available: {resume_capability}",
                metadata={
                    "capability": resume_capability,
                    "blocker_event_id": blocker_event_id,
                },
            )
        else:
            run_start_event_id = self.task_store.append_event(
                session.id,
                role="user",
                kind="input",
                content=task.strip(),
                metadata={"source_type": session.source_type, "source_id": session.source_id},
            )
        observations: list[dict[str, Any]] = (
            [dict(experiment_result)] if experiment_result is not None else []
        )
        # Counted so a finished run says plainly whether the controller used
        # the results it already had. See `_log_result_reuse`.
        redundant_requests = 0
        # The budget is logged when it changes, not every turn; see below.
        last_budget_shape: tuple[Any, ...] | None = None

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
            # The plan is a dozen numbers, and repeating them every turn buries
            # the two that actually change. It goes out once, and again only
            # when the budget really shifts.
            #
            # Compared on what was decided, not on what was measured:
            # `mem_available_kb` moves every read, so comparing the whole plan
            # would log on every turn again while looking like it does not.
            budget_shape = (
                budget_plan.effective_budget_chars,
                budget_plan.observation_chars,
                budget_plan.history_chars,
                budget_plan.low_memory,
            )
            if budget_shape != last_budget_shape:
                logger.info(
                    "controller_prompt_budget session_id=%s %s",
                    session.id,
                    budget_plan.as_log_fields(),
                )
                last_budget_shape = budget_shape
            logger.info(
                "controller_prompt session_id=%s prompt_chars=%s estimated_tokens=%s "
                "history_events=%s observation_count=%s omitted_observations=%s "
                "superseded_observations=%s",
                session.id,
                len(prompt),
                len(prompt) // CHARS_PER_TOKEN,
                len(history),
                len(observations),
                prompt_meta["omitted_observation_indices"],
                prompt_meta["superseded_observation_count"],
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
            self._journal_decision(session.id, decision)
            satisfied_by = self._already_satisfied(decision, observations)
            if satisfied_by is not None:
                redundant_requests += 1
                self._journal_redundant_request(session.id, decision, satisfied_by)
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
            related_goal = related_operation_lookup_goal(terminal_not_found or {})
            if related_goal == str(decision.get("goal", "")).strip():
                # The dead lookup already *was* the narrowed one. Redirecting it
                # to itself would only relabel the repeat, so it goes to the loop
                # guard unchanged - which is the point at which there really is
                # nothing left to offer.
                related_goal = ""
            already_resolved = operation_already_resolved(
                str((terminal_not_found or {}).get("recommended_operation") or ""),
                observations,
            )
            if already_resolved is not None:
                # The redirect has already done its job for this goal once. Doing
                # it again re-asks a question this run answered, and the guard
                # refuses that as a duplicate - the same loop, one step later.
                # The refusal below says what was resolved instead.
                related_goal = ""
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
            elif terminal_not_found is not None and related_goal:
                # The lookup is dead but its answer was not empty: the terminal
                # not_found already described an operation that fits. Asking for
                # that operation by name is a different, answerable question, so
                # the session moves on instead of spending its remaining turns
                # re-asking the dead one until the loop guard ends it (#218).
                recommended_operation = str(
                    terminal_not_found.get("recommended_operation") or ""
                )
                turn_detail = (
                    f"repeated terminal {TOOL_FIND_TOOL} lookup redirected to the "
                    f"related operation {recommended_operation}"
                )
                logger.info(
                    "find_tool terminal not_found redirected session_id=%s goal=%s "
                    "next_action=%s operation=%s",
                    session.id,
                    str(decision.get("goal", ""))[:500],
                    ACTION_RESOLVE_RELATED_OPERATION,
                    recommended_operation,
                    extra={"session_id": session.id},
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
                        "next_action": ACTION_RESOLVE_RELATED_OPERATION,
                        "operation": recommended_operation,
                        "related_goal": related_goal[:500],
                    },
                )
                decision = {
                    "action": TOOL_FIND_TOOL,
                    "goal": related_goal,
                    "reason": (
                        "identical find_tool lookup was terminal after not_found; "
                        f"resolving the related operation {recommended_operation} "
                        "it offered instead"
                    ),
                }
                # Still find_tool, so the redirected lookup goes through the
                # loop guard like any other: a second redirect to the same
                # related goal is a duplicate and is counted as one. What the
                # guard would then refuse is the *redirect target*, so the
                # guidance carried into its verdict has to be that goal's own
                # terminal not_found, not the dead goal's. Nulling it instead
                # would hand the controller the bare "duplicate_tool_call" that
                # #164 exists to prevent.
                terminal_not_found = self._terminal_not_found_observation(
                    related_goal, observations, state_token
                )
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
                    if tool == "operation":
                        # The guard identifies a call by hash, so its refusal
                        # names no operation. The controller needs the name and
                        # arguments to recognise which answer it already holds,
                        # and the prompt view uses them to point at the event
                        # that carries it (#223).
                        observation = {**observation, **arguments}
                    if terminal_not_found is not None:
                        # The guard already refuses the repeat and counts it, so
                        # pre-empting it would only hide the repeat from the
                        # budget that is supposed to end this loop. Make its
                        # verdict actionable instead (#164).
                        observation = {
                            **observation,
                            **self._terminal_not_found_guidance(
                                terminal_not_found, already_resolved
                            ),
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
                        self._log_result_reuse(
                            session.id, observations, redundant_requests
                        )
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
                    # Completion evidence is measured, never accepted on the
                    # controller's word: `effect` says what the call is meant
                    # to do, and the snapshots either bear that out or do not.
                    # Only executions can change anything, so only they are
                    # measured.
                    measured = tool in {ACTION_COMMAND, TOOL_PYTHON_RUN}
                    before = self._worktree_snapshot(
                        self._decision_cwd(execution_decision)
                    ) if measured else None
                    observation = await self._execute_tool(
                        execution_decision,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                    )
                    if measured and self._execution_succeeded(observation):
                        after = self._worktree_snapshot(
                            self._decision_cwd(execution_decision)
                        )
                        # An execution that changed the checkout is completion
                        # evidence. One that succeeded without changing it -
                        # or whose effect this process cannot see, such as a
                        # restarted service or a remote call - is recorded as
                        # a verification instead, which sends the completion
                        # claim to the user rather than closing the task.
                        kind = (
                            "change"
                            if self._observed_state_change(before, after)
                            else "verify"
                        )
                        observation = dict(observation)
                        observation[
                            "completion_evidence" if kind == "change"
                            else "verification_evidence"
                        ] = self._record_evidence(
                            session,
                            kind=kind,
                            source=str(observation.get("tool", tool)),
                        )
                    if (
                        tool == TOOL_FIND_TOOL
                        and observation.get("status") in {"not_found", "unavailable"}
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
                executed = self._execute_resolved_tool(
                    session_id=session.id,
                    state_token=state_token,
                    observation=observation,
                    reason=str(decision.get("reason", "")),
                    task_logger=task_logger,
                )
                if executed is not None:
                    # Say on the lookup itself that it already ran, and where
                    # the result is. The controller follows "find_tool then
                    # call_tool", and a lookup that reports `resolved_tool`
                    # and nothing else reads as an invitation to make that
                    # call - so it does, and the guard refuses a call that
                    # was never needed. Measured in session
                    # e33be32780f747de8c6a85f0ba6af00d: find_tool ran the
                    # operation, the controller called it anyway, and only
                    # after the refusal did it use the result it already had.
                    observations[-1] = {
                        **observations[-1],
                        "already_executed": True,
                        "result_in_event": executed.get("event_id"),
                        "next_step": (
                            "this operation has already run; read its result below "
                            "and choose a different next step"
                        ),
                    }
                    observations.append(executed)
                continue

            if action not in _FINAL_ACTIONS:
                raise TaskOrchestratorError(f"unknown final action {action!r}")

            terminal_rejection = self._validate_evidence_sensitive_terminal(
                session_id=session.id,
                action=action,
                decision=decision,
                observations=observations,
            )
            if terminal_rejection is not None:
                event_id = self.task_store.append_event(
                    session.id,
                    role="system",
                    kind="terminal_decision_rejected",
                    content=json.dumps(terminal_rejection, separators=(",", ":")),
                    metadata={"controller_action": action},
                )
                terminal_rejection["event_id"] = event_id
                observations.append(terminal_rejection)
                continue

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

            self._log_result_reuse(session.id, observations, redundant_requests)
            raw_required_inputs = decision.get("required_inputs")
            required_inputs = [
                str(item)[:200]
                for item in (
                    raw_required_inputs[:20]
                    if isinstance(raw_required_inputs, list)
                    else []
                )
                if str(item).strip()
            ]
            capability = None
            if action == ACTION_MISSING_CAPABILITY:
                capability = str(decision.get("capability") or "").strip()[:300] or None
            result = TaskDecision(
                action=action,
                reason=str(decision.get("reason", ""))[:1000],
                message=str(decision.get("message", ""))[:3000],
                experiment_id=experiment_id,
                observations=observations,
                session_id=session.id,
                capability=capability,
                required_inputs=required_inputs,
                experiment_handover=(
                    experiment_handover(decision.get("goal"), observations) or None
                    if action == ACTION_NEW_EXPERIMENT
                    else None
                ),
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

        self._log_result_reuse(session.id, observations, redundant_requests)
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

        Returns ``(raw_response, provider_used)``. The budget plan controls
        prompt compaction and diagnostics, but does not preemptively block
        llama-server. External providers are used only after a real local
        inference failure.
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
        local_failure: str = "",
    ) -> tuple[str, str]:
        """Delegate externally after local inference has actually failed.

        Never falls back to ``local_worker``: that delegate also calls the
        same local llama-server and cannot recover from the local failure.
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
                "Return exactly one JSON object matching the schema/instructions below; "
                "no Markdown, no prose.\n\n" + prompt
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
                "%s_local_inference_failed session_id=%s error=%s; attempting external fallback",
                role,
                session_id,
                failure,
            )
            if task_logger is not None:
                task_logger.error(
                    "orchestration",
                    f"{role}_local_inference_failed",
                    failure,
                    session_id=session_id,
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
                "name": ACTION_NEW_EXPERIMENT,
                "description": (
                    "Development path for every repository code change, small ones "
                    "included: start a new isolated implementation experiment. What you "
                    "already found in this run is handed over with your goal."
                ),
                "arguments": {"goal": "the concrete change to make and why"},
            },
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment."},
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
                # A description, never a usable goal. The local model copies
                # whatever concrete goal this prompt shows it - twice over, in
                # two different runs, with two different examples - and this
                # was the last place still offering one (#218).
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

        The controller follows "find_tool then call_tool", and a lookup that
        reports `resolved_tool` and nothing else reads as an invitation to
        make that call - so it does, and the guard refuses a call that was
        never needed. Measured in session
        e33be32780f747de8c6a85f0ba6af00d: find_tool ran the operation, the
        controller called it anyway, and only after the refusal did it use the
        result it already had (#223).

        Shared with the knowledge collector, which resolves and executes through
        the same two calls and therefore hands the controller the same trap.
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
    def _knowledge_collector_max_rounds() -> int:
        """Bound research turns; the final synthesis turn is separate."""
        raw = os.environ.get("NEXUS_KNOWLEDGE_COLLECTOR_MAX_ROUNDS", "").strip()
        try:
            configured = int(raw) if raw else 3
        except ValueError:
            logger.warning(
                "invalid NEXUS_KNOWLEDGE_COLLECTOR_MAX_ROUNDS=%r; falling back to 3",
                raw[:100],
            )
            configured = 3
        return max(1, min(configured, 5))

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
                # Read the fields one by one: redaction replaces the URL and
                # everything after it on the line, so the line is no longer
                # valid JSON (#292, seen on the VM).
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

        The external path cuts prompts from the end, which would drop the
        current issue and the output instruction. Memory is shortened first:
        it is the source that disagreed. If the prompt is still too long,
        compaction shrinks the remaining sources, the current issue last.
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

        The answer's token probability is the signal: the model's plain
        answer is "yes" even for outdated memory, but its p_yes drops. Each
        distinct memory entry is asked about on its own, against the issue
        title and body, and the lowest p_yes decides: joined entries and the
        raw output lines hid an outdated entry in the live #288 run. None
        when there is nothing to compare or a probe failed; the briefing
        then runs locally as before.
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
            for entry in entries[:_MEMORY_CHECK_MAX_ENTRIES]:
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
        lowest.details["entries_p_yes"] = [result.details.get("p_yes") for result in results]
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

            # Project history is a complementary source for an explicitly
            # referenced issue. It is gathered by Nexus, not selected by the
            # collector model, so a failed current GitHub read can still be
            # complemented by prior task knowledge without another planning loop.
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
                    # Placed first and marked as history: earlier runs may have
                    # recorded an older version of the issue, and the current
                    # text must win and survive compaction as the newest source (#292).
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
            # Generic tasks currently have no deterministic extractor. The
            # collector must summarize only the request itself rather than
            # inventing external facts.
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
                    "escalated": memory_check.escalate,
                    "reason": memory_check.reason,
                }
            if memory_check is not None and memory_check.escalate:
                # Memory and the current issue disagree: a stronger model
                # writes the briefing, through the same external chain the
                # controller uses when local inference fails (#297).
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
                    )
                    self._parse_knowledge_collector(raw)
                except (TaskOrchestratorError, ControllerBudgetExceededError) as exc:
                    escalation["escalation_failed"] = str(exc)[:500]
                    raw = ""
                logger.info(
                    "knowledge_escalation session_id=%s %s provider=%s failed=%s",
                    session.id,
                    memory_check.reason,
                    provider_used or "-",
                    bool(escalation.get("escalation_failed")),
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
        # Prepare semantic content before applying the size budget.
        viewed_observations = controller_observation_view(deduped_observations)
        compacted_observations, omitted_indices = compact_observations(
            viewed_observations,
            budget_chars=budget_plan.observation_chars,
            keep_recent_full=2,
        )
        prompt = self._prompt(session, truncated_input, history, compacted_observations)
        meta = {
            "input_truncated": input_truncated,
            # Positions in the *viewed* list, which is what was budgeted. The
            # view can drop a superseded failed attempt, so these are not
            # positions in ``observations``; `superseded_observation_count`
            # says how far the two have diverged.
            "omitted_observation_indices": omitted_indices,
            "superseded_observation_count": len(deduped_observations) - len(viewed_observations),
            "deduped_observation_count": sum(
                int(obs.get("deduped", 0)) for obs in deduped_observations if isinstance(obs, dict)
            ),
        }
        return prompt, meta

    def _prompt(self, session: Any, current_input: str, history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> str:
        """Render one controller prompt, stable parts first.

        The order is a cost decision, not a style one. llama-server reuses the
        KV cache for the longest prefix a request shares with the previous one,
        and stops at the first difference - so everything *after* the first
        changed byte is recomputed. Within a run, only the observations change
        between turns; source, input, history and the capability catalog do
        not. With the observations in the middle, every turn re-processed them
        *and* the whole policy/rules block behind them: measured on the VM,
        1222 tokens at 7.18 tokens/second, about 170 seconds of the ~190 a turn
        took (#218).

        With them at the end, the reusable prefix covers the rules too, and
        only the observations and the closing line are new. The closing
        instruction stays last on purpose: a small model handed a wall of JSON
        as the final thing it reads is measurably worse at answering in the
        requested shape, and those few tokens are cheap to recompute.
        """
        tool_results = "\n\n".join(
            f"{str(observation.get('tool') or observation.get('event') or 'tool')} result:\n"
            f"{prompt_json(observation)}"
            for observation in observations
        )

        # Without it a reply to a clarification arrives as the whole task and
        # the original request is gone. The current input is already shown.
        earlier = [
            event for event in history
            if str(event.get("content", "")).strip() != current_input.strip()
        ]
        history_block = (
            f"\nPERSISTENT TASK/DISCUSSION HISTORY:\n{json.dumps(earlier, separators=(',', ':'))}\n"
            if earlier
            else ""
        )

        return f"""You are Nexus, an autonomous agent.
Own the task from understanding through execution and verification.

INPUT:
{current_input}
{history_block}
RULES:
- Reason about the task and choose the next useful action.
- A knowledge_collector briefing is current factual context. used_sources lists sources already consulted.
- Repository code changes of any size go through new_experiment. Use command or python_run for machine work and for reading.
- find_tool only discovers a concrete read-only OperationsRegistry operation; then use call_tool.
- Implementation tasks are complete only after change + verification.
- Ask clarification only when essential information is really missing.
- An observation with `"status":"succeeded"` already carries that call's result. Never call that operation again with the same arguments; use the result and choose a different next step, or `answer` if the request is satisfied.
- Return exactly one next action.

AVAILABLE CAPABILITIES:
{prompt_json(self._capabilities())}

OBSERVATIONS FROM TOOLS USED IN THIS RUN:
{tool_results or "none"}

Decide the next step now. Return JSON only.
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
        if tool == ACTION_KNOWLEDGE_COLLECTOR:
            return tool, {"request": str(decision.get("request", "")).strip()}
        if tool == "delegate":
            return tool, {
                "provider": str(decision.get("provider", "")).strip(),
                "task": str(decision.get("task", "")).strip(),
                "context": str(decision.get("context", "")),
            }
        if tool == ACTION_COMMAND:
            # `effect` is the controller's claim about the call, not part of
            # its identity: including it would let the same command run twice
            # by relabelling it read -> change, which both defeats the #164
            # loop guard and mints completion evidence from a read-only call.
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
        capability_names = sorted(
            str(getattr(spec, "name", "")).strip()
            for spec in self.operations_registry.catalog()
            if str(getattr(spec, "name", "")).strip()
        )
        payload = (
            f"{current_input.strip()}\0{repo_state}\0"
            f"{json.dumps(capability_names, separators=(',', ':'))}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_resolution_evidence(observation: dict[str, Any]) -> bool:
        """Whether an observation proves that Nexus actually tried to resolve a gap."""
        tool = str(observation.get("tool", "")).strip()
        if not tool:
            return False
        # A plan is a statement of intent, never an attempt. The knowledge collector
        # stub written when the planner is unavailable carries the reason it
        # failed, and any truthy failure key below would otherwise let that
        # stub stand in for a capability nobody tried - the exact bypass the
        # #244 guard exists to prevent.
        if tool == TOOL_KNOWLEDGE_BRIEFING:
            return False
        if tool == TOOL_FIND_TOOL:
            return str(observation.get("status", "")).lower() in {
                "not_found", "unavailable", "invalid_goal",
            }
        result = observation.get("result")
        if isinstance(result, dict):
            return True
        return bool(
            observation.get("error")
            or observation.get("success") is False
            or str(observation.get("status", "")).lower()
            in {"failed", "failure", "error", "unavailable", "not_found"}
            or tool in {"memory_search", "delegate", ACTION_COMMAND, TOOL_PYTHON_RUN}
        )

    @staticmethod
    def _supports_missing_capability(observation: dict[str, Any]) -> bool:
        # See `_is_resolution_evidence`: a knowledge collector plan never evidences an
        # attempted capability, however it ended.
        if observation.get("tool") == TOOL_KNOWLEDGE_BRIEFING:
            return False
        if observation.get("tool") == TOOL_FIND_TOOL:
            return str(observation.get("status", "")).lower() in {
                "not_found", "unavailable",
            }
        result = observation.get("result")
        if isinstance(result, dict) and result.get("success") is False:
            return True
        return bool(
            observation.get("error")
            or observation.get("success") is False
            or str(observation.get("status", "")).lower()
            in {"failed", "failure", "error", "unavailable", "not_found"}
        )

    def _terminal_evidence(
        self,
        session_id: str,
        observations: list[dict[str, Any]],
        requested_event_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Return real in-run/persisted observations, optionally limited to cited IDs."""
        by_id: dict[int, dict[str, Any]] = {}
        uncited_current: list[dict[str, Any]] = []
        for observation in observations:
            event_id = observation.get("event_id")
            if isinstance(event_id, int):
                by_id[event_id] = observation
            else:
                uncited_current.append(observation)
        for event in self.task_store.recent_events(session_id, role="tool", limit=50):
            try:
                payload = json.loads(event.get("content") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            payload["event_id"] = int(event["id"])
            by_id.setdefault(int(event["id"]), payload)
        if requested_event_ids:
            return [by_id[event_id] for event_id in requested_event_ids if event_id in by_id]
        return [*by_id.values(), *uncited_current]

    @staticmethod
    def _evidence_matches_gap(observation: dict[str, Any], information_gap: str) -> bool:
        gap_tokens = tool_search_tokens(information_gap)
        if not gap_tokens:
            return False
        observation_tokens = tool_search_tokens(
            json.dumps(observation, ensure_ascii=False, sort_keys=True)
        )
        return bool(gap_tokens & observation_tokens)

    # A request for news about work is not a request to do it. These are
    # matched anywhere in the text, unlike the start-anchored informational
    # openers, because "... and please update me on #42" reads as a status
    # request wherever it sits.
    _STATUS_REQUEST_RE = re.compile(
        r"\b(?:status\s+update|update\s+(?:me|us)\b|keep\s+(?:me|us)\s+posted)"
    )
    _INFORMATIONAL_OPENER_RE = re.compile(
        r"^(?:#*\s*)?(?:how|why|what|explain|summarize|inspect|show|read|describe)\b"
    )
    _CHANGE_INTENT_RE = re.compile(
        r"\b(?:solve|fix|implement|resolve|address|apply|update)\b|"
        r"\bmake\b.{0,80}\bwork\b|\bwork\s+on\b"
    )

    @classmethod
    def _is_implementation_intent(cls, task: str) -> bool:
        """Recognize explicit requests whose requested outcome is a state change."""
        normalized = " ".join(str(task or "").strip().lower().split())
        if not normalized:
            return False
        if cls._INFORMATIONAL_OPENER_RE.match(normalized):
            return False
        if cls._STATUS_REQUEST_RE.search(normalized):
            return False
        return bool(cls._CHANGE_INTENT_RE.search(normalized))

    @staticmethod
    def _intent_key(task: str) -> str:
        """Stable identity of one implementation request within a session."""
        normalized = " ".join(str(task or "").strip().lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _command_exit_code(output: Any) -> int | None:
        """Read the exit code out of a command result.

        Delegates to the shared parser rather than matching a second format:
        ``process_command`` renders ``**Exit code:** 0`` (Markdown, capitalised,
        spaced), so a stricter pattern of this module's own would silently
        never match and no evidence would ever be recorded.
        """
        return exit_code_of(str(output or ""))

    # Paths inside the code checkout that this process and its loggers write
    # on their own. A turn touches them whether or not it changed anything,
    # so they can never stand as evidence of requested work.
    _RUNTIME_OWNED_PREFIXES = ("state/", "logs/", ".git/")

    def _decision_cwd(self, decision: dict[str, Any]) -> Path | None:
        """Working directory of an execution decision, or None when unusable."""
        try:
            return self._resolve_cwd(decision.get("cwd"))
        except (TaskOrchestratorError, OSError):
            return None

    def _execution_succeeded(self, observation: dict[str, Any]) -> bool:
        """Whether a command/python action ran and reported success."""
        tool = str(observation.get("tool", "")).strip()
        if tool not in {ACTION_COMMAND, TOOL_PYTHON_RUN}:
            return False
        if observation.get("error") or observation.get("event") == "command_not_found":
            return False
        return self._command_exit_code(observation.get("result")) == 0

    def _worktree_snapshot(self, cwd: Path | None = None) -> str | None:
        """Executor-side view of the code checkout, scoped to *cwd*.

        Deliberately narrow. The comparison is only ever made across a single
        command execution, over the subtree that command ran in, and with this
        process's own artefacts excluded - a whole-repository fingerprint taken
        around every tool call credits any concurrent writer's work to the call
        in flight. The mailbox daemons write their own separate checkout
        (`AGENT_MAILBOX_REPO_PATH`), so they are outside this view entirely.

        Returns ``None`` when the state cannot be read, which is never treated
        as evidence.
        """
        root = self._repo_root()
        try:
            status = subprocess.run(
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
        except (OSError, subprocess.SubprocessError):
            return None
        if status.returncode != 0 or head.returncode != 0:
            return None
        try:
            scope = str(Path(cwd or root).resolve().relative_to(Path(root).resolve()))
        except ValueError:
            scope = ""
        scope = "" if scope == "." else f"{scope}/"
        entries = []
        for line in status.stdout.splitlines():
            path = line[3:].strip().strip('"')
            if not path.startswith(scope):
                continue
            relative = path[len(scope):]
            if relative.startswith(self._RUNTIME_OWNED_PREFIXES) or path.startswith(
                self._RUNTIME_OWNED_PREFIXES
            ):
                continue
            entries.append(line)
        return "\n".join([head.stdout.strip(), *sorted(entries)])

    @staticmethod
    def _observed_state_change(before: str | None, after: str | None) -> bool:
        """Whether the executor itself saw the checkout change."""
        return before is not None and after is not None and before != after

    _EVIDENCE_LABELS = {"change": "state_change", "verify": "verified_state"}

    def _record_evidence(self, session: Any, *, kind: str, source: str) -> dict[str, Any]:
        """Persist evidence of *kind* for the session's current request.

        Persisted only for a session that owes evidence; for any other session
        the observation still carries the record, but there is no requirement
        for it to answer.
        """
        intent_key = getattr(session, "completion_intent_key", None) or ""
        if intent_key:
            self.task_store.record_completion_evidence(session.id, intent_key, kind)
            if kind == "change":
                session.completion_evidence_key = intent_key
            else:
                session.verification_evidence_key = intent_key
        return {
            "kind": self._EVIDENCE_LABELS[kind],
            "effect": kind,
            "source": source,
            "intent_key": intent_key,
        }

    @staticmethod
    def _evidence_satisfies(session: Any, *, kind: str) -> bool:
        """Whether recorded evidence belongs to the request now in force.

        Read from the session row rather than re-scanned out of the event log:
        the log view is bounded, so evidence in a long session would age out
        and leave it unable to ever finish. Comparing intent keys is also what
        stops one task's evidence from completing a later, different one that
        arrived through ``continue_session``.
        """
        required = getattr(session, "completion_intent_key", None) or ""
        column = (
            "completion_evidence_key" if kind == "change" else "verification_evidence_key"
        )
        observed = getattr(session, column, None) or ""
        return bool(required) and observed == required

    def _promotes_completion_rejection(
        self,
        observations: list[dict[str, Any]],
    ) -> bool:
        """Whether the latest unresolved evidence gate is being called a capability gap.

        The controller is free to paraphrase an observation, so matching its next
        ``information_gap`` string cannot reliably preserve the distinction between
        unfinished work and a missing capability.  Use the structured rejection that
        Nexus itself emitted instead.  A failed tool attempt is independent evidence
        for a real blocker; when one exists, normal missing-capability validation still
        applies.
        """
        latest_completion_rejection = -1
        for index, observation in enumerate(observations):
            if (
                observation.get("event") == "terminal_decision_rejected"
                and observation.get("gap_kind") == "completion_evidence"
            ):
                latest_completion_rejection = index

        if latest_completion_rejection < 0:
            return False
        has_failed_attempt = any(
            self._supports_missing_capability(item)
            or (
                item.get("tool") == ACTION_COMMAND
                and self._command_exit_code(item.get("result")) not in {None, 0}
            )
            for item in observations
        )
        return not has_failed_attempt

    def _validate_evidence_sensitive_terminal(
        self,
        *,
        session_id: str,
        action: str,
        decision: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Reject terminal decisions that are unsupported by the session's evidence."""
        session = self.task_store.get(session_id)
        requires_completion = bool(
            session is not None and session.requires_completion_evidence
        )
        if action == ACTION_ANSWER and requires_completion:
            if self._evidence_satisfies(session, kind="change"):
                return None
            # A run that succeeded without changing anything the executor can
            # see is not completion: `true` does exactly that, and so does a
            # genuine check of work that was already done. The two are
            # indistinguishable from here, and an effect this process cannot
            # observe - a restarted service, a remote call - looks the same
            # again. All three go to the user to confirm, carrying the output,
            # instead of the controller certifying its own claim or the turn
            # burning its remaining steps.
            if self._evidence_satisfies(session, kind="verify"):
                return {
                    "event": "terminal_decision_rejected",
                    "terminal_action": action,
                    "information_gap": "human confirmation that no change is required",
                    "gap_kind": "user_intent",
                    "reason": (
                        "A completion with no observed state change cannot be "
                        "self-certified: the run only shows that a command succeeded "
                        "without changing anything this process can see. Ask the user "
                        "to confirm the requested outcome, citing the command output."
                    ),
                    "candidate_operations": [],
                    "retryable": True,
                    "recommended_next_action": ACTION_CLARIFICATION,
                }
            return {
                "event": "terminal_decision_rejected",
                "terminal_action": action,
                "information_gap": "implementation completion evidence",
                "gap_kind": "completion_evidence",
                "reason": (
                    "Implementation-intent tasks cannot finish from read-only context alone; "
                    "no state change was observed by the executor after any command. Continue "
                    "working on the original task; this meta-gap is not a missing capability."
                ),
                "candidate_operations": [],
                "retryable": True,
                "recommended_next_action": "continue_work",
            }

        if action not in {ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY}:
            return None

        if (
            action == ACTION_MISSING_CAPABILITY
            and requires_completion
            and self._promotes_completion_rejection(observations)
        ):
            return {
                "event": "terminal_decision_rejected",
                "terminal_action": action,
                "information_gap": "implementation completion evidence",
                "gap_kind": "completion_evidence",
                "reason": (
                    "Missing implementation completion evidence means the task is unfinished; "
                    "it does not prove a capability is missing. Continue the original task."
                ),
                "candidate_operations": [],
                "retryable": True,
                "recommended_next_action": "continue_work",
                "direct_capabilities": [ACTION_COMMAND, TOOL_PYTHON_RUN],
            }

        gap_kind = str(decision.get("gap_kind", "")).strip().lower()
        information_gap = str(
            decision.get("information_gap")
            or decision.get("message")
            or decision.get("reason")
            or ""
        ).strip()[:1000]
        why_human_required = str(decision.get("why_human_required", "")).strip()
        user_only_kinds = {"user_intent", "user_preference", "user_authorization"}
        if action == ACTION_CLARIFICATION and gap_kind in user_only_kinds:
            if why_human_required:
                return None
            rejection_reason = "A user-only clarification must explain why only the user can decide."
        else:
            raw_ids = decision.get("evidence_event_ids")
            requested_ids = [
                value for value in (raw_ids if isinstance(raw_ids, list) else [])[:10]
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            ]
            cited_evidence = self._terminal_evidence(session_id, observations, requested_ids)
            evidence = [
                item for item in cited_evidence
                if self._evidence_matches_gap(item, information_gap)
            ]
            supported = (
                any(self._supports_missing_capability(item) for item in evidence)
                if action == ACTION_MISSING_CAPABILITY
                else any(self._is_resolution_evidence(item) for item in evidence)
            )
            candidates = self.tool_router.candidate_operations(information_gap)
            native_candidates = (
                controller_native_capabilities(information_gap)
                if action == ACTION_MISSING_CAPABILITY
                else []
            )
            terminal_evidence = self._terminal_evidence(
                session_id, observations, requested_ids
            )
            native_failure = any(
                (
                    str(item.get("tool", "")).strip() == ACTION_COMMAND
                    and ACTION_COMMAND in native_candidates
                    and (
                        item.get("event") == "command_not_found"
                        or (
                            self._command_exit_code(item.get("result")) is not None
                            and self._command_exit_code(item.get("result")) != 0
                        )
                        or bool(item.get("error"))
                    )
                )
                or (
                    str(item.get("tool", "")).strip() == TOOL_PYTHON_RUN
                    and TOOL_PYTHON_RUN in native_candidates
                    and self._supports_missing_capability(item)
                )
                for item in terminal_evidence
            )
            # An empty result from the authoritative read-only catalog only
            # rules a capability out when the controller has no native path
            # for the same gap. command/python_run deliberately do not live in
            # OperationsRegistry, so treating an empty read-only catalog as
            # proof of absence is the #244 failure.
            catalog_rules_out_capability = (
                action == ACTION_MISSING_CAPABILITY
                and not candidates
                and not native_candidates
            )
            native_rejection = (
                action == ACTION_MISSING_CAPABILITY
                and bool(native_candidates)
                and not native_failure
            )
            if native_rejection:
                rejection_reason = (
                    "The terminal claim is unsupported: a controller-native LOCAL "
                    "capability can address this gap and has not failed yet."
                )
            elif supported or catalog_rules_out_capability or (
                action == ACTION_CLARIFICATION and not candidates and not gap_kind
            ):
                return None
            elif requested_ids and len(evidence) != len(set(requested_ids)):
                rejection_reason = "The cited evidence_event_ids do not all identify persisted tool observations."
            elif candidates:
                rejection_reason = (
                    "The terminal claim is unsupported: a plausible read-only capability remains "
                    "discoverable and no relevant attempted resolution was observed."
                )
            else:
                rejection_reason = (
                    "The terminal claim is unsupported by an actual tool, memory, or capability-search observation."
                )

        candidate_names = [spec.name for spec in candidates][:3]
        recommended_next_action = (
            native_candidates[0]
            if action == ACTION_MISSING_CAPABILITY and native_candidates
            else (TOOL_FIND_TOOL if candidate_names else "memory_search")
        )
        return {
            "event": "terminal_decision_rejected",
            "terminal_action": action,
            "information_gap": information_gap,
            "gap_kind": gap_kind or "unspecified",
            "reason": rejection_reason[:1000],
            "candidate_operations": candidate_names,
            "retryable": True,
            "recommended_next_action": recommended_next_action,
            "direct_capabilities": native_candidates,
        }

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
        """Controlled, non-crashing outcome when no provider can take this turn.

        The session and its full history/observations remain persisted so a
        human can resolve capacity/configuration and resume; nothing required
        to continue is silently discarded.
        """
        # Counted like any other turn: a statistic that only records the turns
        # that succeeded would answer the wrong question.
        self.task_store.record_controller_turn(
            session_id,
            provider_used="none",
            outcome="inference_failed",
            detail=reason,
        )
        result = TaskDecision(
            action=ACTION_MISSING_CAPABILITY,
            reason=reason,
            message=(
                "Nexus could not take this controller turn: local inference "
                "failed and no external provider is available. This needs "
                "human review before retrying."
            ),
            observations=observations,
            session_id=session_id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "controller_inference_failed",
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
        # The capability list shows every action's fields under "arguments", and
        # the local model answers in exactly that shape (#257). call_tool keeps
        # its own "arguments" object for the resolved operation.
        nested = data.get("arguments")
        if action != ACTION_CALL_TOOL and isinstance(nested, dict):
            data = {**nested, **data}
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
        if tool == ACTION_KNOWLEDGE_COLLECTOR:
            request = str(decision.get("request", "")).strip()
            if not request:
                raise TaskOrchestratorError("knowledge_collector requires request")
            session_id = str(decision.get("session_id", "")).strip()
            session = self.task_store.get(session_id)
            if session is None:
                raise TaskOrchestratorError("knowledge_collector requires a valid task session")
            if budget_plan is None:
                budget_plan = compute_budget(
                    mem_available_kb=read_mem_available_kb(),
                    swap_free_kb=read_swap_free_kb(),
                    swap_total_kb=read_swap_total_kb(),
                )
            return await self._run_knowledge_collector(
                session,
                request,
                budget_plan,
                task_logger=task_logger,
            )
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

    @staticmethod
    def _rejected_python_run(decision: dict[str, Any], error: str) -> dict[str, Any]:
        """An invalid python_run request, returned to the controller to correct.

        Raising here ended the whole task on one malformed call, and the
        request itself was never recorded, so the failure could not be
        diagnosed afterwards.
        """
        received = {
            key: decision.get(key)
            for key in ("script", "module", "argv", "args", "cwd")
            if key in decision
        }
        logger.warning(
            "python_run rejected error=%s received=%s",
            error,
            redact_secrets(json.dumps(received, default=str))[:2000],
        )
        return {
            "tool": TOOL_PYTHON_RUN,
            "args": received,
            "error": "invalid_tool_request",
            "reason": error[:1000],
            "recovery": "correct the arguments or choose another capability",
        }

    def _execute_python_run(self, decision: dict[str, Any]) -> dict[str, Any]:
        argv = decision.get("argv")
        if argv is None and isinstance(decision.get("args"), list):
            argv = decision.get("args")
        if argv is not None and not isinstance(argv, list):
            return self._rejected_python_run(decision, "python_run argv must be a list")
        cwd = self._resolve_cwd(decision.get("cwd"))
        try:
            plan = build_python_command(
                script=decision.get("script"),
                module=decision.get("module"),
                args=argv or (),
                repo_root=self._repo_root(),
            )
        except RuntimeExecutionError as exc:
            return self._rejected_python_run(decision, str(exc))

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

    def _execute_resolved_tool(
        self,
        *,
        session_id: str,
        state_token: str,
        observation: dict[str, Any],
        reason: str,
        task_logger: Any | None = None,
    ) -> dict[str, Any] | None:
        """Run a read-only operation ``find_tool`` already resolved *and* bound.

        The controller pays the same formality failure analysis did (#177):
        after the resolver has returned one operation and read its arguments
        straight out of the goal, asking the model to confirm "now call it"
        buys nothing and costs a full decision on the local model.

        Executed only when the router bound the arguments against that goal.
        ``resolved_arguments`` is ``None`` whenever a value could not be tied
        back to it, and that must never be read as "use the defaults" - it is
        how a goal naming ``llama-server`` would be answered with
        ``agent-api``. Everything else is the ordinary path: the same loop
        guard, the same grant rule, the same read-only boundary in
        ``execute_operation``, the same durable event.
        """
        if observation.get("tool") != TOOL_FIND_TOOL or observation.get("status") != "found":
            return None
        operation = str(observation.get("resolved_tool") or "").strip()
        arguments = observation.get("resolved_arguments")
        if not operation or not isinstance(arguments, dict):
            return None

        # The same identity `_tool_identity` gives a controller `call_tool`.
        # The guard recognises a repeat by hashing (tool, arguments), so
        # spelling this call `call_tool {"tool":..., "arguments":...}` while the
        # controller's own spells it `operation {"operation":..., "args":...}`
        # made one call hash two ways: the guard could not see that a
        # subsequent call_tool was re-running what this already ran. Measured
        # on the VM, git_status resolved here, ran, and the controller's next
        # decision ran it again unrefused (#223).
        check = self.loop_guard.check(
            session_id,
            "operation",
            {"operation": operation, "args": dict(arguments)},
            state_token=state_token,
            reason=f"executing the read-only operation find_tool resolved: {reason}"[:1000],
        )
        if not check.allowed:
            return None
        try:
            result = self._execute_operation(
                {"operation": operation, "args": dict(arguments), "session_id": session_id}
            )
        except TaskOrchestratorError as exc:
            # A refused execution is an observation about this turn, not a
            # failure of it: the model still sees why and can choose again.
            # Same shape as a refused call_tool, `recovery` included: the
            # controller should not have to recognise two spellings of one
            # answer depending on which path ran the operation.
            result = {
                "tool": "operation",
                "operation": operation,
                "args": dict(arguments),
                "error": "invalid_tool_request",
                "reason": str(exc)[:1000],
                "recovery": "correct the arguments or choose another capability",
            }
        self.loop_guard.record_result(check.call_id, result)
        if task_logger is not None:
            task_logger.info(
                "orchestration",
                "resolved_tool_executed",
                f"executed {operation} directly from its find_tool resolution",
                tool=operation,
                session_id=session_id,
            )
        event_id = self.task_store.append_event(
            session_id,
            role="tool",
            kind=str(result.get("tool", "operation")),
            content=json.dumps(result, separators=(",", ":")),
            metadata={
                "reason": f"resolved by find_tool: {reason}"[:1000],
                **(
                    {"knowledge_collector_internal": True}
                    if reason == _KNOWLEDGE_LOOKUP_REASON
                    else {}
                ),
            },
        )
        result = dict(result)
        result["event_id"] = event_id
        return result

    def _execute_operation(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Run one resolved capability, reporting a refusal rather than raising.

        A capability that rejects its arguments is answering the caller:
        "issue_number must be a positive integer" is a fact the controller can
        act on by calling again with a real number. Raising instead ended a
        whole Nexus task with a traceback and lost every observation gathered
        before it (#218).

        Deliberately narrower than catching every tool error. ``python_run``
        refusing a working directory outside the repository is a boundary being
        tested, not an argument being corrected, and that still stops the run.
        """
        try:
            return self.tool_router.execute_operation(decision)
        except ToolRouterError as exc:
            operation = str(decision.get("operation", "")).strip()
            logger.warning(
                "operation call rejected operation=%s error=%s",
                operation,
                str(exc)[:500],
            )
            return {
                "tool": "operation",
                "operation": operation,
                "args": decision.get("args") or {},
                "error": "invalid_tool_request",
                "reason": str(exc)[:1000],
                "recovery": "correct the arguments or choose another capability",
            }

    @staticmethod
    def _already_satisfied(
        decision: dict[str, Any], observations: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """The successful observation this decision asks to produce again.

        Distinct from what the loop guard catches. The guard refuses any
        repeat, including a retry of something that failed - a legitimate
        move. This names the one case #223 is about: asking for a result that
        is already in hand, which is never progress and never necessary.
        """
        if str(decision.get("action", "")).strip().lower() != ACTION_CALL_TOOL:
            return None
        operation = str(decision.get("tool", "")).strip()
        if not operation:
            return None
        arguments = decision.get("arguments")
        wanted = _args_signature(operation, arguments if isinstance(arguments, dict) else {})
        for observation in reversed(observations):
            if observation.get("tool") != "operation" or observation.get("event"):
                continue
            result = observation.get("result")
            if not (isinstance(result, dict) and result.get("success")):
                continue
            if _args_signature(str(observation.get("operation", "")), observation.get("args")) == wanted:
                return observation
        return None

    def _journal_redundant_request(
        self,
        session_id: str,
        decision: dict[str, Any],
        satisfied_by: dict[str, Any],
    ) -> None:
        """Record that the controller asked for something it already had."""
        operation = str(decision.get("tool", ""))
        event_id = satisfied_by.get("event_id")
        logger.warning(
            "controller_redundant_request session_id=%s operation=%s "
            "already_succeeded_in_event=%s reason=%s",
            session_id,
            operation[:120],
            event_id,
            redact_secrets(str(decision.get("reason", "")))[:300],
            extra={"session_id": session_id, "event": "controller_redundant_request"},
        )
        try:
            self.task_store.append_event(
                session_id,
                role="system",
                kind="redundant_tool_request",
                content=(
                    f"{operation} was requested again although event {event_id} "
                    "already holds its result"
                ),
                metadata={
                    "operation": operation[:120],
                    "arguments": decision.get("arguments") or {},
                    "already_succeeded_in_event": event_id,
                    "controller_reason": redact_secrets(str(decision.get("reason", "")))[:500],
                },
            )
        except Exception:  # noqa: BLE001 - instrumentation never breaks a turn
            logger.debug("could not journal the redundant request", exc_info=True)

    @staticmethod
    def _log_result_reuse(
        session_id: str, observations: list[dict[str, Any]], redundant_requests: int
    ) -> None:
        """One line per run saying whether results were reused.

        Green has to be observable, not only red. Without this a correct run
        is silent and indistinguishable from one nobody looked at, so the only
        way to know was to read the journal back with a script afterwards.
        """
        successes = sum(
            1
            for observation in observations
            if observation.get("tool") == "operation"
            and not observation.get("event")
            and isinstance(observation.get("result"), dict)
            and observation["result"].get("success")
        )
        logger.info(
            "controller_result_reuse session_id=%s successful_operations=%s "
            "redundant_requests=%s verdict=%s",
            session_id,
            successes,
            redundant_requests,
            "clean" if redundant_requests == 0 else "reused_nothing",
            extra={"session_id": session_id, "event": "controller_result_reuse"},
        )

    def _journal_decision(self, session_id: str, decision: dict[str, Any]) -> None:
        """Record what the controller chose and the reason it gave.

        `nexus_controller_turns` keeps the outcome of a turn; the model's own
        stated reason was not kept anywhere. Reading a finished session back
        therefore showed which tools ran and nothing about why - so a
        controller that asked for a result it already had could be observed
        doing it, and never asked what it thought it was doing.

        Best-effort, like every other piece of instrumentation here: this must
        never be the reason a turn fails.
        """
        try:
            metadata: dict[str, Any] = {"action": str(decision.get("action", ""))[:64]}
            for field in _DECISION_FIELDS:
                value = decision.get(field)
                if value in (None, "", {}, []):
                    continue
                metadata[field] = (
                    redact_secrets(str(value))[:300] if isinstance(value, str) else value
                )
            arguments = decision.get("arguments")
            if isinstance(arguments, dict) and arguments:
                metadata["arguments"] = arguments
            self.task_store.append_event(
                session_id,
                role="system",
                kind="controller_decision",
                content=redact_secrets(str(decision.get("reason", "")))[:1000],
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - instrumentation never breaks a turn
            logger.debug("could not journal the controller decision", exc_info=True)

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
                "experiment_handover": decision.experiment_handover,
                "has_command": bool(decision.command),
                "capability": decision.capability,
                "required_inputs": decision.required_inputs,
                "trigger_task": task[:4000] if decision.action == ACTION_MISSING_CAPABILITY else None,
                "catalog_at_block": (
                    sorted(
                        str(getattr(spec, "name", "")).strip()
                        for spec in self.operations_registry.catalog()
                        if str(getattr(spec, "name", "")).strip()
                    )
                    if decision.action == ACTION_MISSING_CAPABILITY
                    else None
                ),
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
