"""Controller turn loop, loop-guard outcomes and answer parsing of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import logging
import json
from typing import Any
from modules.llama_cpp_client import (
    LlamaCppClientError,
    extract_json,
)
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESOLVE_RELATED_OPERATION,
    ACTION_RESUME_EXPERIMENT,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    operation_already_resolved,
    related_operation_lookup_goal,
)
from modules.experiment_handover import experiment_handover
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetExhaustedError,
    compute_budget,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
)
from modules.tool_result_reader import (
    TOOL_READ_TOOL_RESULT,
    duplicate_read_guidance,
    normalize_arguments as read_tool_result_arguments,
)
from modules.task_orchestrator_parts.core import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_NEW_EXPERIMENT,
    ControllerBudgetExceededError,
    TaskDecision,
    TaskOrchestratorError,
    _FINAL_ACTIONS,
    _ORCHESTRATOR_TOOLS,
    _issue_briefing_decision,
)
logger = logging.getLogger("modules.task_orchestrator")


class ControllerLoopMixin:
    """Controller turn loop and answer parsing methods of :class:`NexusTaskOrchestrator`."""

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
                if action == TOOL_READ_TOOL_RESULT:
                    # Reduce limit to the cap before the guard sees the call, so a read above
                    # the cap and one at it are the same range (#562).
                    normalized, _ = read_tool_result_arguments(
                        execution_decision, self._read_tool_result_cap(budget_plan)
                    )
                    if normalized is not None:
                        execution_decision.update(normalized)
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
                    if tool == TOOL_READ_TOOL_RESULT:
                        # Another range of the event is progress; send the controller on to the
                        # next range, not to another tool (#562).
                        observation = {**observation, **duplicate_read_guidance(arguments)}
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
                    try:
                        observation = await self._execute_tool(
                            execution_decision,
                            budget_plan=budget_plan,
                            task_logger=task_logger,
                        )
                    except TaskOrchestratorError as exc:
                        if " requires " not in str(exc):
                            raise
                        # A tool call with a missing argument is the model's
                        # slip, not a crash of the task: let it correct the call.
                        observation = {
                            "tool": tool,
                            "status": "invalid_arguments",
                            "error": str(exc),
                            "message": f"{exc}. Call the tool again with that argument, or choose another action.",
                        }
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
                if self._adoption_decided(experiment_id):
                    # Its adoption is over (merged, or stopped by the review): there is
                    # nothing left to resume. #642 resumed one every tick for hours.
                    rejection = {
                        "event": "terminal_decision_rejected",
                        "terminal_action": ACTION_RESUME_EXPERIMENT,
                        "reason": (
                            f"experiment {experiment_id[:8]} is finished and its adoption is "
                            "decided; it cannot be resumed. To fix review findings, choose "
                            "new_experiment."
                        ),
                        "retryable": True,
                    }
                    rejection["event_id"] = self.task_store.append_event(
                        session.id,
                        role="system",
                        kind="terminal_decision_rejected",
                        content=json.dumps(rejection, separators=(",", ":")),
                        metadata={"controller_action": action, "experiment_id": experiment_id},
                    )
                    observations.append(rejection)
                    continue
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
                complexity=(
                    "small" if decision.get("complexity") == "small" else "large"
                    if action == ACTION_NEW_EXPERIMENT else None
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

