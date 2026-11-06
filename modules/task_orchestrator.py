"""Agentic local-LLM controller for free-form Nexus work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
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
    local_llm_watchdog_seconds,
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
    operation_already_resolved,
    related_operation_lookup_goal,
    terminal_not_found_guidance,
    terminal_not_found_observation,
    tokens as tool_search_tokens,
)
from modules.controller_observations import _args_signature, controller_observation_view
from modules.experiment_handover import experiment_handover
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetExhaustedError,
    PromptBudgetPlan,
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    fit_history,
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

# Fallback delegates after a local inference failure. ``local_worker`` is
# excluded - same llama-server, no recovery.
_EXTERNAL_FALLBACK_ORDER: tuple[str, ...] = ("azure_openai", "codex_readonly")

# Who drives the controller (#337): Azure takes the turns when configured;
# the local model only covers an Azure failure.
_CONTROLLER_PROVIDER_ENV = "NEXUS_CONTROLLER_PROVIDER"
# Room for a routed reasoning model to think before it writes the JSON answer.
_AZURE_CONTROLLER_MAX_TOKENS = 2000

# GitHub-write commands go through new_experiment + PR review, not straight
# from the controller (#337).
_GITHUB_WRITE_COMMAND = re.compile(
    r"\bgh\s+(?:issue|pr|label|release|repo)\s+"
    r"(?:close|comment|create|delete|edit|lock|merge|reopen|review|transfer|unlock)\b"
    r"|\bgh\s+api\b.*(?:\s(?:-X|--method)\s*(?:POST|PATCH|PUT|DELETE)\b"
    r"|\s(?:-f|-F|--field|--raw-field|--input)\b)",
    re.IGNORECASE,
)
# External fallback prompts are cut to this length; size the prompt below it
# first (#297).
_EXTERNAL_PROMPT_CHARS = 16000
# Opens the controller prompt's history section; its length counts against the budget.
_HISTORY_HEADER = "\nPERSISTENT TASK/DISCUSSION HISTORY:\n"
# Room for the role preamble that _external_fallback_completion puts in front.
_EXTERNAL_PREAMBLE_CHARS = 300


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

# Decision fields worth journalling; `message` can be a page of prose and
# adds nothing to "why did it choose this".
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

# Re-exports from the shared tool mechanism (#172) under historical names.
# No read-only denylist to re-export: each capability declares whether it
# mutates state.
_TOOL_RESOLVER_RESPONSE_FORMAT = TOOL_RESOLVER_RESPONSE_FORMAT
_MAX_RESOLVER_CANDIDATES = MAX_RESOLVER_CANDIDATES
_TOOL_ACCURACY_UNKNOWN = TOOL_ACCURACY_UNKNOWN
_TOOL_ACCURACY_CORRECT = TOOL_ACCURACY_CORRECT
_TOOL_ACCURACY_INCORRECT = TOOL_ACCURACY_INCORRECT
_TOOL_SEARCH_STOP_WORDS = TOOL_SEARCH_STOP_WORDS


_SOLVE_ISSUE_RE = re.compile(r"^\s*solve github issue #\d+\s*$", re.IGNORECASE)


def _issue_briefing_decision(task: str, observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """An issue to solve goes to an experiment once its briefing is there (#311)."""
    if not _SOLVE_ISSUE_RE.match(task) or not observations:
        return None
    last = observations[-1]
    if last.get("tool") != ACTION_KNOWLEDGE_COLLECTOR or not last.get("knowledge_briefing"):
        return None
    return {
        "action": ACTION_NEW_EXPERIMENT,
        "goal": task.strip(),
        "reason": "issue briefing collected; solving an issue starts an experiment",
    }


def _guidance_core(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(word for word in words if word not in {"please", "could", "you"})


def _repeats_tool_guidance(question: str, observations: list[dict[str, Any]]) -> bool:
    """Whether a clarification only passes on a tool's guidance to Nexus (#312)."""
    asked = _guidance_core(question)
    if not asked:
        return False
    for observation in observations:
        guidance = _guidance_core(str(observation.get("guidance") or ""))
        if guidance and guidance in asked:
            return True
    return False


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
    budget and no external provider is available or succeeds.

    Callers must turn this into a controlled decision (``missing_capability``/needs_review),
    not call llama-server.
    """


_EXPERIMENT_ID_PATTERN = EXPERIMENT_ID_PATTERN
_MAX_EXPERIMENT_ID_CANDIDATES = MAX_EXPERIMENT_ID_CANDIDATES
_RESUME_INTENT_PATTERN = RESUME_INTENT_PATTERN


class _TaskStoreGrantLedger:
    """``find_tool`` grants as persistent controller-session events.

    Recording is a no-op: grants are already durable in the session event log,
    and a second copy could disagree with the history it describes.
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
        self.llama = llama_client or LlamaCppClient(timeout=local_llm_watchdog_seconds())
        self.max_steps = max(1, min(max_steps, 10))
        db_path = getattr(operations_registry, "db_path", DEFAULT_DB_PATH)
        self.loop_guard = PersistentToolLoopGuard(
            db_path=db_path,
            max_total_steps=max_total_steps or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_TOTAL_STEPS", "20")),
            max_identical_calls=max_identical_calls or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_IDENTICAL_CALLS", "3")),
            max_non_progressing_cycles=max_non_progressing_cycles or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_CYCLES", "2")),
        )
        # Shared local-LLM execution path (#167): all local-model callers queue on one
        # worker per database, so they serialize instead of racing llama-server.
        self.llm_jobs = llm_job_service or acquire_local_llm_job_service(
            db_path=db_path, llama_client=llama_client
        )
        self._owns_llm_jobs = llm_job_service is None
        if llama_client is None and llm_job_service is None:
            # One client for the whole process, owned by the shared job service,
            # watchdog timeout included.
            self.llama = self.llm_jobs.llama
        repo_root = str(getattr(operations_registry, "repo_root", "/agent"))
        self.task_store = task_store or NexusTaskStore(db_path=db_path)
        self._owns_task_store = task_store is None
        self.memory = memory or NexusMemory(db_path=db_path, repo_root=repo_root, task_store=self.task_store)
        self._owns_memory = memory is None
        # Delegation shares the orchestrator's job service, so it can never open a
        # second request path to llama-server (#167).
        self.delegation = delegation_service or NexusDelegationService(
            llama_client=self.llama,
            db_path=db_path,
            repo_root=repo_root,
            llm_job_service=self.llm_jobs,
        )
        self._owns_delegation = delegation_service is None
        # One shared tool mechanism (#172): the router owns catalog, resolver, grant
        # rule and validation/redaction/audit.
        self.tool_router = NexusToolRouter(
            operations_registry=operations_registry,
            experiment_service=experiment_service,
            schema_completion=self._router_schema_completion,
            grant_ledger=_TaskStoreGrantLedger(self.task_store),
            caller="local task orchestrator",
            controller=True,
        )
        # session_id -> worker id of the current claim. Status writes during a claimed
        # turn are scoped to it, so a reaped or taken-over worker cannot drive the session.
        self._claims: dict[str, str] = {}

    def _claim_owner(self, session_id: str) -> str | None:
        """The worker id this process holds the session under, if it does."""
        return self._claims.get(session_id)

    def _claimed_status(self, session_id: str, status: str) -> None:
        """Set a status, but only while this process still owns the session."""
        owner = self._claim_owner(session_id)
        if owner is None:
            # Bug guard, not a fallback: unreachable from a claimed turn. Fails closed -
            # an unowned write is what is being prevented.
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

        Called only after the claim succeeds - writing first would mutate a session
        another worker holds. The key is refreshed per implementation request, so a
        follow-up keeps the evidence already gathered.
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
        # Claim the session and heartbeat for the whole turn (#162): a turn can spend
        # minutes in one inference call, so the heartbeat - not ``updated_at`` - proves
        # an owner. A failed claim must refuse: treating it as survivable would switch
        # off the ownership rules exactly when persistence misbehaves.
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

        Called when the experiment ends and by the polling safety net after a restart;
        the claim ensures only one of them continues a session.
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
        """Record a turn that died in the open instead of letting it go quiet.

        Otherwise the row would end up as ``heartbeat_timeout`` - a weaker fact than
        "this turn raised".
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
                # The experiment changed code and passed verification; without this the answer
                # is rejected for lack of completion evidence, turn after turn (#263).
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
        # Counted so a finished run says whether results were reused; see `_log_result_reuse`.
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
                fixed_chars=self._controller_fixed_chars(session),
                mem_available_kb=read_mem_available_kb(),
                swap_free_kb=read_swap_free_kb(),
                swap_total_kb=read_swap_total_kb(),
            )
            # Fetched up to the whole variable room; `_build_prompt` trims it to what
            # the observations leave, since history is the re-readable section.
            history = self.task_store.history(
                session.id,
                limit=18,
                max_chars=budget_plan.variable_chars,
                max_event_id=run_start_event_id,
            )
            try:
                prompt, prompt_meta = self._build_prompt(
                    session, task, history, observations, budget_plan
                )
            except PromptBudgetExhaustedError as exc:
                logger.error(
                    "controller_prompt_budget_exhausted session_id=%s %s error=%s",
                    session.id,
                    budget_plan.as_log_fields(),
                    exc,
                )
                return self._budget_exceeded_result(
                    session.id, task, observations, str(exc), task_logger
                )
            # Log the plan once, and again only when the budget really shifts - compared on
            # what was decided, not measured (`mem_available_kb` moves every read).
            budget_shape = (
                budget_plan.effective_budget_chars,
                budget_plan.fixed_chars,
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
                "controller_prompt session_id=%s prompt_chars=%s budget_chars=%s "
                "estimated_tokens=%s history_events=%s observation_count=%s "
                "omitted_observations=%s superseded_observations=%s",
                session.id,
                len(prompt),
                budget_plan.effective_budget_chars,
                len(prompt) // CHARS_PER_TOKEN,
                prompt_meta.get("history_events", len(history)),
                len(observations),
                prompt_meta["omitted_observation_indices"],
                prompt_meta["superseded_observation_count"],
            )
            logger.info(
                "llm_provider selected provider=%s role=controller session_id=%s reason=persistent_controller",
                "azure_openai" if self._azure_drives_controller() else "local",
                session.id,
            )
            issue_decision = _issue_briefing_decision(task, observations)
            try:
                if issue_decision is not None:
                    raw, provider_used = json.dumps(issue_decision), "issue_rule"
                else:
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
            if provider_used not in {"local", "issue_rule"} and not (
                provider_used == "azure_openai" and self._azure_drives_controller()
            ):
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
            # The controller's own choice: a redirect below may change the executed action,
            # but the turn record keeps the model's actual decision (#158).
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
                # The dead lookup already was the narrowed one; redirecting it to itself would
                # only relabel the repeat, so it goes to the loop guard unchanged.
                related_goal = ""
            already_resolved = operation_already_resolved(
                str((terminal_not_found or {}).get("recommended_operation") or ""),
                observations,
            )
            if already_resolved is not None:
                # The redirect already did its job for this goal once; repeating it is the same
                # duplicate the guard refuses, one step later.
                related_goal = ""
            if (
                terminal_not_found is not None
                and resume_experiment_id
                and self._may_resume_experiment(session.id, task, resume_experiment_id)
            ):
                # Terminal lookup, and the resolver already established which experiment this
                # task is about - go there directly instead of re-asking the dead question.
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
                # The dead lookup's answer was not empty: the terminal not_found already named a
                # fitting operation, so ask for that by name instead of re-asking until the
                # guard ends it (#218).
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
                # Still find_tool, so the redirected lookup goes through the loop guard: carry
                # the redirect target's own terminal not_found in its verdict, not the dead
                # goal's - else the guard hands out the bare duplicate_tool_call that #164
                # exists to prevent.
                terminal_not_found = self._terminal_not_found_observation(
                    related_goal, observations, state_token
                )
            # One durable row per turn (#158), so which model decided a turn is answerable
            # without grepping the journal.
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
                        # The guard refuses by hash and names no operation; the controller needs name +
                        # arguments to recognise the answer it already holds (#223).
                        observation = {**observation, **arguments}
                    if terminal_not_found is not None:
                        # The guard already refuses and counts the repeat; pre-empting it would hide it
                        # from the budget that ends the loop - make the verdict actionable instead (#164).
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
                    # Completion evidence is measured, never taken on the controller's word; only
                    # executions can change anything, so only they are measured.
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
                        # An execution that changed the checkout is completion evidence; one that
                        # succeeded without a visible change (restarted service, remote call) is a
                        # verification and goes to the user to confirm.
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
                    # Say on the lookup that it already ran and where the result is: a lookup
                    # reporting only `resolved_tool` reads as an invitation to call it, and the
                    # guard then refuses a call that was never needed (#223).
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
                reboot = executed or observation
                if self._successful_machine_reboot(reboot):
                    # Scheduling the reboot is itself the requested state change - do not queue
                    # another inference, the host may go down during it and recovery would repeat
                    # the reboot forever (#326).
                    reboot = dict(reboot)
                    reboot["completion_evidence"] = self._record_evidence(
                        session, kind="change", source="machine_reboot"
                    )
                    observations[-1] = reboot
                    result = TaskDecision(
                        action=ACTION_ANSWER,
                        reason="machine reboot was scheduled successfully",
                        message="The machine reboot has been scheduled.",
                        observations=observations,
                        session_id=session.id,
                    )
                    self._log_result_reuse(session.id, observations, redundant_requests)
                    self._persist_final(session.id, task, result)
                    return result
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
                    # The controller sometimes leaves the id out. Tell it, and let
                    # it decide again, instead of failing the whole task.
                    rejection = {
                        "event": "terminal_decision_rejected",
                        "terminal_action": ACTION_RESUME_EXPERIMENT,
                        "reason": (
                            "resume_experiment needs experiment_id. Name the experiment "
                            "this task started, or choose new_experiment."
                        ),
                        "retryable": True,
                    }
                    rejection["event_id"] = self.task_store.append_event(
                        session.id,
                        role="system",
                        kind="terminal_decision_rejected",
                        content=json.dumps(rejection, separators=(",", ":")),
                        metadata={"controller_action": action},
                    )
                    observations.append(rejection)
                    continue
                try:
                    experiment_id = self.experiment_service.resolve_experiment(str(experiment_id)).id
                except Exception as exc:
                    raise TaskOrchestratorError(f"unknown experiment {experiment_id!r}") from exc
                if not self._may_resume_experiment(session.id, task, experiment_id):
                    # An experiment named in a refusal is someone else's work - resuming it turned
                    # a refused reboot into an unrelated merged change (#330).
                    rejection = self._foreign_resume_rejection(experiment_id)
                    event_id = self.task_store.append_event(
                        session.id,
                        role="system",
                        kind="terminal_decision_rejected",
                        content=json.dumps(rejection, separators=(",", ":")),
                        metadata={
                            "controller_action": action,
                            "experiment_id": experiment_id,
                        },
                    )
                    logger.warning(
                        "resume_experiment rejected session_id=%s experiment_id=%s "
                        "reason=not_started_by_session",
                        session.id,
                        experiment_id,
                    )
                    rejection["event_id"] = event_id
                    observations.append(rejection)
                    continue
            else:
                experiment_id = None

            if action == ACTION_NEW_EXPERIMENT:
                verified = self._succeeded_issue_experiment(session.id)
                if verified is not None:
                    # Verified work for this issue already waits for adoption and review; a second
                    # experiment raced it (#530).
                    result = TaskDecision(
                        action=ACTION_ANSWER,
                        reason="second experiment refused (#530)",
                        message=(
                            f"Experiment {verified} already succeeded for this issue. "
                            "Adoption and its review decide what happens next."
                        ),
                        observations=observations,
                        session_id=session.id,
                    )
                    self._persist_final(session.id, task, result)
                    return result
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
                "name": ACTION_NEW_EXPERIMENT,
                "description": (
                    "Development path for every repository code change, small ones "
                    "included: start a new isolated implementation experiment. What you "
                    "already found in this run is handed over with your goal."
                ),
                "arguments": {"goal": "the concrete change to make and why"},
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

    def _build_prompt(
        self,
        session: Any,
        current_input: str,
        history: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        budget_plan: PromptBudgetPlan,
    ) -> tuple[str, dict[str, Any]]:
        """Compact input, observations and history so the whole prompt fits the budget.

        The rendered prompt - fixed sections included - never exceeds
        ``budget_plan.effective_budget_chars`` (#230). Priority when room is short:
        the current input (capped), then the observations, then the persisted history,
        which is re-readable and so gives way first. Raises
        ``PromptBudgetExhaustedError`` rather than sending a prompt in which the
        controller cannot see its own tool results.
        """
        budget_plan.require_usable()
        budget = budget_plan.effective_budget_chars
        capabilities_json = prompt_json(self._capabilities())
        truncated_input, input_truncated = truncate_text(current_input, budget_plan.current_input_chars)
        seen: set[str] = set()
        for event in history:
            content = str(event.get("content", "")).strip()[:400]
            if content:
                seen.add(content)
        deduped_observations = dedupe_nested_results(observations, already_seen=seen)
        # Prepare semantic content before applying the size budget.
        viewed_observations = controller_observation_view(deduped_observations)
        # Input room the task did not use goes to the observations, not to history.
        observation_budget = budget_plan.observation_chars + max(
            0, budget_plan.current_input_chars - len(truncated_input)
        )
        # Compaction bounds the serialised observations; per-result headers and stub
        # minimums can still overshoot, so the rendered length is what is checked.
        for _ in range(4):
            compacted_observations, omitted_indices = compact_observations(
                viewed_observations,
                budget_chars=observation_budget,
                keep_recent_full=2,
            )
            without_history = self._prompt(
                session, truncated_input, [], compacted_observations,
                capabilities_json=capabilities_json,
            )
            overflow = len(without_history) - budget
            if overflow <= 0:
                break
            observation_budget = max(0, observation_budget - overflow - 200)
        else:
            raise PromptBudgetExhaustedError(
                f"prompt budget exhausted: {len(without_history)} characters without any "
                f"history exceed the {budget}-character budget even with observations "
                "compacted to stubs"
            )
        earlier = [
            event for event in history
            if str(event.get("content", "")).strip() != truncated_input.strip()
        ]
        history_room = budget - len(without_history) - len(_HISTORY_HEADER) - 1
        fitted_history = fit_history(earlier, history_room) if history_room > 0 else []
        prompt = self._prompt(
            session, truncated_input, fitted_history, compacted_observations,
            capabilities_json=capabilities_json,
        )
        if len(prompt) > budget:
            # Not expected - fit_history measures the same serialisation - but the
            # bound is a correctness requirement, so history yields entirely.
            fitted_history = []
            prompt = without_history
        meta = {
            "history_events": len(fitted_history),
            "history_events_dropped": len(earlier) - len(fitted_history),
            "input_truncated": input_truncated,
            # Positions in the *viewed* list, which is what was budgeted - not in
            # ``observations``; `superseded_observation_count` says how far they diverged.
            "omitted_observation_indices": omitted_indices,
            "superseded_observation_count": len(deduped_observations) - len(viewed_observations),
            "deduped_observation_count": sum(
                int(obs.get("deduped", 0)) for obs in deduped_observations if isinstance(obs, dict)
            ),
        }
        return prompt, meta

    def _controller_fixed_chars(self, session: Any) -> int:
        """Measured cost of the controller prompt sections that are never compacted.

        The rules block, the capability catalog and the framing, rendered with an
        empty input, no history and no observations - ``len()``, not a share (#230).
        """
        return len(self._prompt(session, "", [], []))

    def _prompt(
        self,
        session: Any,
        current_input: str,
        history: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        *,
        capabilities_json: str | None = None,
    ) -> str:
        """Render one controller prompt, stable parts first.

        llama-server reuses the KV cache only up to the first changed byte, so the
        observations - the only part that changes between turns - go last, and the
        closing instruction stays last on purpose (#218).
        """
        if capabilities_json is None:
            capabilities_json = prompt_json(self._capabilities())
        tool_results = "\n\n".join(
            f"{str(observation.get('tool') or observation.get('event') or 'tool')} result:\n"
            f"{prompt_json(observation)}"
            for observation in observations
        )

        # Without it a reply to a clarification arrives as the whole task and the original
        # request is lost.
        earlier = [
            event for event in history
            if str(event.get("content", "")).strip() != current_input.strip()
        ]
        history_block = (
            f"{_HISTORY_HEADER}{json.dumps(earlier, separators=(',', ':'))}\n"
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
- Nexus services: agent-api, nexus-mailbox, llama-server.
- find_tool discovers a concrete OperationsRegistry operation (read-only, plus service_restart and machine_reboot); then use call_tool.
- Implementation tasks are complete only after change + verification.
- If a tool is refused because of another experiment, tell the user which experiment blocks it. Never resume that experiment: resume_experiment is only for an experiment this task started or the user asked to resume.
- Ask clarification only when essential information is really missing.
- An observation with `"status":"succeeded"` already carries that call's result. Never call that operation again with the same arguments; use the result and choose a different next step, or `answer` if the request is satisfied.
- Return exactly one next action.

AVAILABLE CAPABILITIES:
{capabilities_json}

OBSERVATIONS FROM TOOLS USED IN THIS RUN:
{tool_results or "none"}

Decide the next step now. Return JSON only.
"""

    def _action_to_tool_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Translate the action-only controller protocol to internal tool dispatch.

        Native controller capabilities stay internal actions; registry calls always
        use find_tool -> call_tool.
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
            # `effect` is the controller's claim, not part of the call's identity: including
            # it would let the same command run twice by relabelling read -> change, defeating
            # the #164 guard and minting evidence from a read-only call.
            return tool, {"command": str(decision.get("command", "")).strip()}
        if tool == TOOL_PYTHON_RUN:
            # Different working directories make two python_run calls materially different;
            # they must not collapse into one duplicate signature.
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
        # A plan is intent, never an attempt: a truthy failure key in the planner stub
        # would stand in for a capability nobody tried - the #244 bypass.
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
        # A knowledge collector plan never evidences an attempted capability; see
        # `_is_resolution_evidence`.
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

    # Status-request phrases match anywhere in the text, unlike start-anchored openers:
    # "... and please update me on #42" reads as a status request wherever it sits.
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

        Must match the shared parser's ``**Exit code:** 0`` rendering - a stricter
        pattern of this module's own would never match.
        """
        return exit_code_of(str(output or ""))

    # Paths this process writes on its own; a turn touches them regardless, so they
    # are never evidence of requested work.
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

    @staticmethod
    def _successful_machine_reboot(observation: dict[str, Any]) -> bool:
        """Whether an operation observation confirms a scheduled reboot."""
        result = observation.get("result")
        return (
            observation.get("tool") == "operation"
            and observation.get("operation") == "machine_reboot"
            and isinstance(result, dict)
            and result.get("success") is True
        )

    def _worktree_snapshot(self, cwd: Path | None = None) -> str | None:
        """Executor-side view of the code checkout, scoped to *cwd*.

        Narrow on purpose: comparisons span one command over its subtree, excluding
        this process's own artifacts - a whole-repo fingerprint would credit a
        concurrent writer. Returns ``None`` when unreadable, never treated as evidence.
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

        Persisted only for a session that owes evidence.
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

        Read from the session row, not the bounded log view; comparing intent keys
        stops one task's evidence from completing a different later task.
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

        Match the structured rejection Nexus itself emitted, not a paraphrased
        ``information_gap`` string; a failed tool attempt still counts as a real blocker.
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

    def _succeeded_issue_experiment(self, session_id: str) -> str | None:
        """A succeeded experiment of this session, if the session works on an issue.

        Only issue sessions count - chat sessions may ask for follow-up work.
        """
        inputs = self.task_store.recent_events(session_id, role="user", kind="input", limit=100)
        if not any((e.get("metadata") or {}).get("source_type") == "issue" for e in inputs):
            return None
        for experiment_id in sorted(self._session_experiment_ids(session_id)):
            try:
                status = self.experiment_service.get_experiment(experiment_id).status
            except Exception:  # noqa: BLE001 - an unknown experiment refuses nothing
                continue
            if status == "succeeded" and not self._adoption_decided(experiment_id):
                return experiment_id
        return None

    def _adoption_decided(self, experiment_id: str) -> bool:
        """Whether adoption already judged *experiment_id* (#546).

        A review that blocked the merge sends the issue back for another
        attempt in the same session; only work still waiting for adoption
        refuses a second experiment.
        """
        conn = getattr(self.experiment_service, "_conn", None)
        if not isinstance(conn, sqlite3.Connection):
            return False
        try:
            row = conn.execute(
                "SELECT status FROM experiment_adoptions WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        except sqlite3.Error:
            return False
        return bool(row) and str(row[0]) in {"failed", "needs_review", "adopted"}

    def _session_experiment_ids(self, session_id: str) -> set[str]:
        """Experiments this session started (#330).

        Park and journal share the id, covering the experiment's whole life.
        """
        started: set[str] = set()
        session = self.task_store.get(session_id)
        waiting = getattr(session, "waiting_experiment_id", None) if session else None
        if waiting:
            started.add(str(waiting))
        for event in self.task_store.recent_events(
            session_id, role="tool", kind=ACTION_NEW_EXPERIMENT, limit=100
        ):
            experiment_id = (event.get("metadata") or {}).get("experiment_id")
            if experiment_id:
                started.add(str(experiment_id))
        return started

    def _may_resume_experiment(self, session_id: str, task: str, experiment_id: str) -> bool:
        """Whether resume_experiment may continue *experiment_id* here (#330).

        Only the session's own experiments, plus one the user explicitly asked to
        resume - an id that merely turned up in an observation does not qualify.
        """
        experiment_id = str(experiment_id)
        if experiment_id in self._session_experiment_ids(session_id):
            return True
        text = task or ""
        if not _RESUME_INTENT_PATTERN.search(text):
            return False
        return any(
            experiment_id.lower().startswith(token.lower())
            for token in _EXPERIMENT_ID_PATTERN.findall(text)
        )

    @staticmethod
    def _foreign_resume_rejection(experiment_id: str) -> dict[str, Any]:
        return {
            "event": "terminal_decision_rejected",
            "terminal_action": ACTION_RESUME_EXPERIMENT,
            "experiment_id": experiment_id,
            "information_gap": "whether the user wants this other experiment touched",
            "gap_kind": "user_intent",
            "reason": (
                f"Experiment {experiment_id} was not started by this task and the "
                "user did not ask to resume it. Do not resume it. If a tool was "
                "refused because of this experiment, tell the user which experiment "
                "blocks the request and let them decide."
            ),
            "candidate_operations": [],
            "retryable": False,
            "recommended_next_action": ACTION_CLARIFICATION,
        }

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
            # A run that succeeded without a visible change is not completion (`true` does
            # exactly that): send it to the user to confirm rather than letting the controller
            # certify its own claim or burn its remaining steps.
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

        if action == ACTION_CLARIFICATION and _repeats_tool_guidance(
            str(decision.get("message", "")), observations
        ):
            return {
                "event": "terminal_decision_rejected",
                "terminal_action": action,
                "information_gap": str(decision.get("message", ""))[:1000],
                "gap_kind": "tool_guidance",
                "reason": (
                    "The question repeats guidance a tool gave to Nexus; the user cannot "
                    "answer it. Continue the original task."
                ),
                "candidate_operations": [],
                "retryable": True,
                "recommended_next_action": "continue_work",
            }

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
            # An empty read-only catalog only rules out a capability when there is no native
            # path for the same gap - command/python_run deliberately live outside
            # OperationsRegistry (#244).
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

        Session and history stay persisted so a human can resume; nothing needed to
        continue is silently discarded.
        """
        # Counted like any other turn: a statistic of only successful turns answers the
        # wrong question.
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
                f"Nexus could not take this controller turn: {reason}. "
                "This needs human review before retrying."
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

        # Temporary read compatibility for sessions persisted before #111; the controller
        # schema no longer exposes this shape.
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
        # The capability list shows each action's fields under "arguments", the shape the
        # model answers in (#257).
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
            if _GITHUB_WRITE_COMMAND.search(command):
                return {
                    "tool": ACTION_COMMAND,
                    "command": command,
                    "error": "github_write_refused",
                    "reason": (
                        "Commands that write to GitHub (close, comment, create, edit, merge, "
                        "or gh api writes) are not run from the controller. Use new_experiment "
                        "so the change is made in a reviewable PR."
                    ),
                }
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

        Raising ended the whole task on one malformed call without recording the request.
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
        catalog = [item["provider"] for item in self.delegation.catalog()]
        available = set(catalog)
        if not provider and catalog:
            # The controller sometimes leaves the provider out: take the local worker or the
            # first available.
            provider = "local_worker" if "local_worker" in available else catalog[0]
        if provider not in available:
            raise TaskOrchestratorError(f"delegation provider {provider!r} is not available")

        # Judgement goes to a strong model (#337): the local worker is the same small
        # model and answers confidently when wrong.
        routed = provider
        if provider == "local_worker" and "azure_openai" in available and not self._controller_forced_local():
            routed = "azure_openai"
        order = [p for p in _PROVIDER_FALLBACKS.get(routed, (routed,)) if p in available]
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

        Executed only when the router bound the arguments to the goal.
        ``resolved_arguments`` is ``None`` when a value could not be tied back to it -
        never read that as "use the defaults".
        """
        if observation.get("tool") != TOOL_FIND_TOOL or observation.get("status") != "found":
            return None
        operation = str(observation.get("resolved_tool") or "").strip()
        arguments = observation.get("resolved_arguments")
        if not operation or not isinstance(arguments, dict):
            return None

        # Same identity as a controller call_tool: the guard hashes (tool, arguments), and
        # spelling the call two ways made one call hash differently, so a repeat went
        # unrefused (#223).
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
            # A refused execution is an observation about this turn, not a failure of it -
            # same shape as a refused call_tool, so the controller sees one spelling.
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

        An argument rejection is an answer the controller can act on; a boundary
        refusal (e.g. python_run outside the repository) still stops the run.
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

        Asking for a result already in hand is never progress - the case #223 is about.
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

        Green has to be observable too, not only red.
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

        Best-effort: instrumentation must never fail a turn.
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
        # Journal, status and claim release in one ownership-scoped write; if this worker
        # was taken over, none of it lands on the new owner's session (#162).
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
