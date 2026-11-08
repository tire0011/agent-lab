"""Tool execution, delegation and journaling of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import logging
import json
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from modules.mailbox.development import process_command
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    ToolRouterError,
)
from modules.controller_observations import _args_signature
from modules.prompt_budget import (
    PromptBudgetPlan,
    compute_budget,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
)
from modules.redaction import redact_secrets
from modules.runtime_execution import (
    RuntimeExecutionError,
    build_python_command,
    classify_command_failure,
    corrected_command,
    equivalent_command,
    resolve_python_interpreter,
)
from modules.tool_result_reader import (
    TOOL_READ_TOOL_RESULT,
    read_tool_result,
)
from modules.task_orchestrator_parts.core import (
    ACTION_CLARIFICATION,
    ACTION_KNOWLEDGE_COLLECTOR,
    TaskDecision,
    TaskOrchestratorError,
    _DECISION_FIELDS,
    _GITHUB_WRITE_COMMAND,
    _KNOWLEDGE_LOOKUP_REASON,
    _PROVIDER_FALLBACKS,
)
logger = logging.getLogger("modules.task_orchestrator")


class ExecutionMixin:
    """Tool execution and journaling methods of :class:`NexusTaskOrchestrator`."""

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
            return self._run_command_with_recovery(
                command,
                tool=ACTION_COMMAND,
                extra={"session_id": str(decision.get("session_id", ""))},
            )
        if tool == TOOL_PYTHON_RUN:
            return self._execute_python_run(decision)
        if tool == TOOL_READ_TOOL_RESULT:
            return read_tool_result(
                self.task_store,
                str(decision.get("session_id", "")),
                decision,
                cap=self._read_tool_result_cap(budget_plan),
            )
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
            mapping = equivalent_command(command, missing_executable=failure.executable)
        capability = self._missing_tool_capability(failure.executable)
        recovery_name = mapping[1] if mapping else "no equivalent capability available"
        durable_gap = Path(failure.executable).name.lower() not in {"python", "python3", "pip", "pip3"}
        capability_issue = (
            self._ensure_capability_issue(
                capability=capability,
                missing_executable=failure.executable,
                command=command,
                recovery=recovery_name,
                origin=str((extra or {}).get("session_id") or "") or None,
            )
            if durable_gap
            else {"status": "not_required", "reason": "resolved runtime alias is not a durable gap"}
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
                "capability_gap": {
                    "capability": capability,
                    "normalized_identity": Path(failure.executable).name.lower(),
                    "alternatives": ["existing_equivalent", "install_or_provision", "capability_issue", "reusable_operation"],
                    "chosen": "missing_capability",
                    "installation_attempted": False,
                    "issue": capability_issue,
                },
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
            "choice": "existing_equivalent" if " -> " in mapping_name and "python" not in mapping_name else "runtime_mapping",
            "capability_issue": capability_issue,
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
    def _missing_tool_capability(executable: str) -> str:
        if Path(executable).name.lower() == "rg":
            return "repository text-search capability"
        return f"development capability provided by {Path(executable).name}"

    def _ensure_capability_issue(self, **details: Any) -> dict[str, Any]:
        """Best-effort durable tracking; issue creation never blocks recovery."""
        factory = getattr(self.operations_registry, "github_work_tracking_factory", None)
        if not callable(factory):
            return {"status": "unavailable"}
        service = None
        try:
            service = factory(
                db_path=getattr(self.operations_registry, "db_path", "state/agent.db"),
                repo_root=str(self._repo_root()),
            )
            result = service.ensure_capability_issue(**details)
            return {"status": "created" if result.get("created") else "reused", **result}
        except Exception as exc:  # noqa: BLE001 - tracking must not block current work
            logger.warning("capability issue tracking failed: %s", str(exc)[:500])
            return {"status": "failed", "reason": str(exc)[:500]}
        finally:
            if service is not None:
                try:
                    service.close()
                except Exception:  # noqa: BLE001
                    logger.debug("could not close capability issue service", exc_info=True)

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
        if provider == "local_worker" and not self._controller_forced_local():
            # A free registry model before a paid one (#552).
            if "free_tier" in available:
                routed = "free_tier"
            elif "azure_openai" in available:
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
