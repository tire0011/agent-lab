"""Prompt/observation building, compaction and evidence gates of NexusTaskOrchestrator (mixin)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESUME_EXPERIMENT,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    controller_native_capabilities,
    tokens as tool_search_tokens,
)
from modules.controller_observations import controller_observation_view
from modules.prompt_budget import (
    PromptBudgetExhaustedError,
    PromptBudgetPlan,
    compact_observations,
    dedupe_nested_results,
    fit_history,
    prompt_json,
    truncate_text,
)
from modules.runtime_execution import exit_code_of
from modules.tool_result_reader import (
    TOOL_READ_TOOL_RESULT,
    limit_cap as read_tool_result_limit_cap,
)
from modules.task_orchestrator_parts.core import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_KNOWLEDGE_COLLECTOR,
    ACTION_NEW_EXPERIMENT,
    TOOL_KNOWLEDGE_BRIEFING,
    TaskOrchestratorError,
    _EXPERIMENT_ID_PATTERN,
    _HISTORY_HEADER,
    _RESUME_INTENT_PATTERN,
    _repeats_tool_guidance,
)


class ContextMixin:
    """Prompt building and evidence-gate methods of :class:`NexusTaskOrchestrator`."""

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

    @staticmethod
    def _read_tool_result_cap(budget_plan: PromptBudgetPlan | None) -> int:
        """``read_tool_result``'s limit cap: one slice fits the observation budget (#562)."""
        return read_tool_result_limit_cap(
            budget_plan.observation_chars if budget_plan is not None else None
        )

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
        if tool == TOOL_READ_TOOL_RESULT:
            # The effective range (limit already reduced to the cap): another range of one
            # event is a different call, the same range is a duplicate (#562).
            return tool, {
                "event_id": decision.get("event_id"),
                "offset": decision.get("offset", 0),
                "limit": decision.get("limit"),
            }
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

