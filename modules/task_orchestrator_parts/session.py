"""Session lifecycle and public API of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import logging
import asyncio
import os
from typing import Any
from modules.llama_cpp_client import LlamaCppClient
from modules.heartbeat import (
    Heartbeat,
    worker_id as heartbeat_worker_id,
)
from modules.local_llm_jobs import (
    LocalLlmJobService,
    acquire_local_llm_job_service,
    local_llm_watchdog_seconds,
    release_local_llm_job_service,
)
from modules.nexus_delegation import NexusDelegationService
from modules.nexus_memory import NexusMemory
from modules.nexus_task_store import (
    STATUS_FAILED,
    TERMINAL_REASON_TURN_CANCELLED,
    TERMINAL_REASON_TURN_FAILED,
    NexusTaskStore,
)
from modules.nexus_tool_router import (
    ACTION_MISSING_CAPABILITY,
    NexusToolRouter,
)
from modules.run_store import DEFAULT_DB_PATH
from modules.tool_loop_guard import PersistentToolLoopGuard
from modules.task_orchestrator_parts.core import (
    TaskDecision,
    TaskOrchestratorError,
    _TaskStoreGrantLedger,
)
logger = logging.getLogger("modules.task_orchestrator")


class SessionMixin:
    """Session lifecycle and public API methods of :class:`NexusTaskOrchestrator`."""

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

