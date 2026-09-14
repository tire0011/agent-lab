"""One MCP-style tool mechanism, shared by every Nexus LLM role (#172).

Tool discovery (``find_tool``) and read-only tool execution (``call_tool``)
used to live inside :class:`~modules.task_orchestrator.NexusTaskOrchestrator`,
which meant only the persistent controller could reach them. Every other
Nexus role - failure analysis first among them - could describe evidence it
had been handed but never go and *check* anything, so a stale persisted
error string was indistinguishable from a current one.

This module holds that mechanism instead, so the controller and the other
roles drive the same code: the same ``OperationsRegistry`` catalog, the same
read-only denylist, the same candidate scoring, the same resolver prompt and
schema, the same "``call_tool`` needs a prior ``find_tool`` grant" rule, and
the same registry-side validation, redaction and audit logging. Roles differ
only in what they are *allowed* to do with it, not in how it works.

Two things are deliberately injected rather than owned here:

* **Inference** (*schema_completion*). The controller routes its resolver
  calls through a memory budget with an external-provider fallback; failure
  analysis just queues a job on the shared local-LLM service. Both end up
  calling the same resolver prompt with the same response schema.
* **Grants** (*grant_ledger*). The controller's grants are persistent
  session events; a one-shot diagnostic role only needs the grants issued
  during its own bounded run (:class:`InMemoryToolGrantLedger`). The rule
  itself - no ``call_tool`` without a matching ``find_tool`` result - is
  enforced here for everyone.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Protocol

from modules.llama_cpp_client import LlamaCppClientError, extract_json

logger = logging.getLogger(__name__)

TOOL_FIND_TOOL = "find_tool"
ACTION_CALL_TOOL = "call_tool"

# Action names the not-found metadata below can point a caller at. Defined
# here rather than imported from the orchestrator so the dependency runs one
# way only (orchestrator -> router); the orchestrator re-exports them.
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_MISSING_CAPABILITY = "missing_capability"

READ_ONLY_OPERATION_DENYLIST = {"inbox_refresh"}
MAX_RESOLVER_CANDIDATES = 5

TOOL_ACCURACY_UNKNOWN = "unknown"
TOOL_ACCURACY_CORRECT = "correct"
TOOL_ACCURACY_INCORRECT = "incorrect"

TOOL_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "check",
    "for",
    "has",
    "inspect",
    "need",
    "needs",
    "nexus",
    "of",
    "show",
    "the",
    "to",
    "whether",
}

TOOL_RESOLVER_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["found", "invalid_goal", "not_found", "unavailable"]},
            "operation": {"type": "string"},
            "reason": {"type": "string"},
            "related_tools": {"type": "array", "items": {"type": "string"}},
            "error": {"type": "string"},
            "guidance": {"type": "string"},
        },
        "required": ["status", "reason"],
        "additionalProperties": False,
    },
}

# A bare hex run is only a *candidate* experiment id: commit shas and dates match
# it too, so every candidate is resolved against the experiment service before it
# is treated as one (#164 review).
EXPERIMENT_ID_PATTERN = re.compile(r"\b[0-9a-f]{8,32}\b", re.IGNORECASE)
MAX_EXPERIMENT_ID_CANDIDATES = 5
RESUME_INTENT_PATTERN = re.compile(r"\b(resume|continue|continuing)\b", re.IGNORECASE)


class ToolRouterError(RuntimeError):
    """Bad tool arguments or an unavailable operation - a caller-side error."""


class ToolResolverUnavailableError(RuntimeError):
    """Inference for the resolver could not be performed.

    Carries the ``provider`` label and reason wording the caller wants in the
    resulting ``unavailable`` observation, so the injected inference path -
    not this module - decides how its own failure modes read.
    """

    def __init__(self, detail: str, *, provider: str = "local", reason: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.provider = provider
        self.reason = reason or f"Tool resolver LLM job failed: {detail}"


class ToolGrantLedger(Protocol):
    """Where a role's ``find_tool`` grants live."""

    def record_grant(self, session_id: str, observation: dict[str, Any]) -> None:
        ...

    def latest_grant(self, session_id: str, operation: str) -> dict[str, Any] | None:
        ...


class InMemoryToolGrantLedger:
    """Grants that live exactly as long as one bounded run.

    Enough for a role that resolves and calls a tool inside a single
    operation (failure analysis) and has no persistent session to attach the
    grant to. The rule it serves is the shared one: ``call_tool`` only ever
    runs an operation some ``find_tool`` result already returned a schema for.
    """

    def __init__(self) -> None:
        self._grants: dict[str, dict[str, dict[str, Any]]] = {}

    def record_grant(self, session_id: str, observation: dict[str, Any]) -> None:
        if observation.get("status") != "found":
            return
        schema = observation.get("operation")
        if not isinstance(schema, dict):
            return
        operation = str(schema.get("operation", "")).strip()
        if not operation:
            return
        self._grants.setdefault(session_id, {})[operation] = {
            "event_id": None,
            "operation": operation,
            "schema": schema,
        }

    def latest_grant(self, session_id: str, operation: str) -> dict[str, Any] | None:
        return self._grants.get(session_id, {}).get(operation)


def tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in TOOL_SEARCH_STOP_WORDS
    }


def invalid_find_tool_goal(goal: str) -> tuple[bool, str | None, str | None]:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(goal or "").lower()).strip()
    if not normalized:
        return True, "The goal is empty or whitespace only.", "Describe the operation you need, e.g. 'run a shell command in the repository'."

    placeholder_values = {
        "plain language capability needed",
        "plain-language capability needed",
        "capability needed",
        "example goal",
        "placeholder goal",
        "generic capability needed",
        "replace with concrete capability",
        "insert goal here",
        "describe the operation you need",
        "tool goal",
    }
    if normalized in placeholder_values:
        return True, "The goal is a placeholder, not a concrete capability request.", "Describe the operation you need, e.g. 'run a shell command in the repository'."

    if "plain language" in normalized or "plain-language" in normalized:
        if "capability needed" in normalized or "goal" in normalized:
            return True, "The goal is a placeholder, not a concrete capability request.", "Describe the operation you need, e.g. 'run a shell command in the repository'."
    if "placeholder" in normalized or "example" in normalized:
        if "goal" in normalized or "capability" in normalized or "request" in normalized:
            return True, "The goal is a placeholder, not a concrete capability request.", "Describe the operation you need, e.g. 'run a shell command in the repository'."
    if "capability needed" in normalized and ("plain" in normalized or "placeholder" in normalized or "example" in normalized):
        return True, "The goal is a placeholder, not a concrete capability request.", "Describe the operation you need, e.g. 'run a shell command in the repository'."

    generic_tokens = {"capability", "goal", "operation", "request", "tool", "task", "need", "needs"}
    goal_tokens = normalized.split()
    if len(goal_tokens) <= 2 and set(goal_tokens) <= generic_tokens:
        return True, "The goal is too generic to describe a concrete capability request.", "Describe the operation you need, e.g. 'run a shell command in the repository'."
    return False, None, None


def selection_correctness(
    *,
    status: str,
    selected_operation: str | None,
    expected_operation: str | None,
    expected_status: str | None,
) -> str:
    if status == "unavailable":
        return TOOL_ACCURACY_UNKNOWN
    if expected_operation:
        if status != "found":
            return TOOL_ACCURACY_INCORRECT
        return TOOL_ACCURACY_CORRECT if selected_operation == expected_operation else TOOL_ACCURACY_INCORRECT
    if expected_status:
        return TOOL_ACCURACY_CORRECT if status == expected_status else TOOL_ACCURACY_INCORRECT
    return TOOL_ACCURACY_UNKNOWN


def terminal_not_found_observation(
    goal: str,
    observations: list[dict[str, Any]],
    state_token: str,
) -> dict[str, Any] | None:
    """The terminal ``not_found`` this exact lookup already produced, if any."""
    return next(
        (
            observation
            for observation in reversed(observations)
            if observation.get("tool") == TOOL_FIND_TOOL
            and observation.get("status") == "not_found"
            and observation.get("retryable") is False
            and observation.get("state_token") == state_token
            and str(observation.get("goal", "")).strip() == goal
        ),
        None,
    )


def terminal_not_found_guidance(prior: dict[str, Any]) -> dict[str, Any]:
    """Turn a refused repeat into an answer the caller can act on.

    The loop guard says "you already called this"; on its own that leaves
    the caller to guess what else to do, which is how the loop in #164
    kept going. This carries the terminal lookup's alternatives back.
    """
    return {
        "status": "not_found",
        "retryable": False,
        "goal": str(prior.get("goal", "")),
        "related_tools": list(prior.get("related_tools") or []),
        "related_operations": list(prior.get("related_operations") or []),
        "direct_capabilities": list(prior.get("direct_capabilities") or []),
        "recommended_next_action": prior.get("recommended_next_action"),
        "reason": (
            "An identical find_tool lookup already returned not_found "
            "without a relevant state change."
        ),
    }


def resolver_prompt(goal: str, candidates: list[Any]) -> str:
    candidate_summaries = [
        {
            "operation": spec.name,
            "description": spec.description,
            "arguments": [
                argument.to_dict()
                for argument in getattr(spec, "arguments", [])
            ],
            "tags": list(getattr(spec, "tags", []) or []),
        }
        for spec in candidates
    ]
    return f"""TOOL RESOLVER MODE.
You are resolving exactly one requested Nexus capability against a small candidate list.
Return exactly one JSON object. Do not plan the task. Do not implement missing tools.

GOAL:
{goal[:1000]}

CANDIDATE READ-ONLY OPERATIONS:
{json.dumps(candidate_summaries, separators=(',', ':'))}

Rules:
- Choose status "found" only when one candidate directly satisfies the goal.
- Choose status "invalid_goal" when the goal is empty, generic, or clearly a placeholder/example rather than a concrete capability request.
- Choose status "not_found" when none is suitable.
- If found, set operation to the exact candidate operation name.
- If invalid_goal, leave operation empty, set error and guidance, and do not treat it as a missing capability.
- If not_found, leave operation empty and list at most three related candidate names.
"""


# Signature of the injected inference path: one resolver completion,
# returning ``(raw_response, provider_used)``.
SchemaCompletion = Callable[..., Any]


class NexusToolRouter:
    """Capability discovery and read-only capability execution for any role."""

    def __init__(
        self,
        *,
        operations_registry: Any,
        schema_completion: SchemaCompletion,
        experiment_service: Any | None = None,
        grant_ledger: ToolGrantLedger | None = None,
        caller: str = "nexus tool router",
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self._schema_completion = schema_completion
        self.grant_ledger: ToolGrantLedger = grant_ledger or InMemoryToolGrantLedger()
        self.caller = caller

    # ------------------------------------------------------------------
    # Catalog / candidate selection
    # ------------------------------------------------------------------

    def read_only_operation_specs(self) -> list[Any]:
        return [
            spec for spec in self.operations_registry.catalog()
            if spec.name not in READ_ONLY_OPERATION_DENYLIST
        ]

    def candidate_operations(self, goal: str) -> list[Any]:
        goal_tokens = tokens(goal)
        scored: list[tuple[int, str, Any]] = []
        for spec in self.read_only_operation_specs():
            spec_tags = " ".join(str(tag) for tag in getattr(spec, "tags", []) or [])
            haystack = f"{spec.name} {spec.description} {spec_tags}"
            haystack_tokens = tokens(haystack)
            score = len(goal_tokens & haystack_tokens)
            if spec.name.lower() in goal.lower():
                score += 5
            for token in goal_tokens:
                if token and token in spec.name.lower():
                    score += 2
            if score > 0:
                scored.append((score, spec.name, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [spec for _, _, spec in scored[:MAX_RESOLVER_CANDIDATES]]

    # ------------------------------------------------------------------
    # not_found metadata
    # ------------------------------------------------------------------

    def resolvable_experiment_ids(self, text: str) -> list[str]:
        """Hex-looking tokens in ``text`` that are actually experiments.

        "Looks like hex" is not an identity. A commit sha matches the same
        pattern, and so does a date like ``20260913``; handing one to
        ``resume_experiment`` turns an ordinary task ("continue reviewing the
        regression from commit 4f1b3f2a") into ``unknown experiment``. Only ids
        the experiment service can actually resolve count as experiment ids.

        Resolution is bounded: a pathological input full of hex tokens must not
        turn one lookup into dozens of database queries.
        """
        if self.experiment_service is None:
            return []
        resolved: list[str] = []
        candidates = list(dict.fromkeys(EXPERIMENT_ID_PATTERN.findall(text)))
        for candidate in candidates[:MAX_EXPERIMENT_ID_CANDIDATES]:
            try:
                experiment = self.experiment_service.resolve_experiment(candidate)
            except Exception:  # noqa: BLE001 - unknown id, not an error here
                continue
            experiment_id = str(getattr(experiment, "id", "") or "").strip()
            # The resolved id has to look like one too: a service that answers
            # every lookup (a stub, a stale cache) must not be able to turn a
            # commit sha into an "experiment" by returning something arbitrary.
            if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
                continue
            if experiment_id not in resolved:
                resolved.append(experiment_id)
        return resolved

    def find_tool_not_found_metadata(
        self,
        task_text: str,
        goal: str,
        related_operations: list[str],
    ) -> dict[str, Any]:
        """What the caller can do instead, for this task and this goal.

        Deliberately scoped to the current task and lookup rather than to the
        session's history: an experiment id and the word "resume" left over
        from an earlier, unrelated request say nothing about what this task
        needs, and offering a direct capability on that basis points the
        caller at someone else's work.
        """
        context_text = " ".join(part for part in (task_text or "", goal or "") if part)
        experiment_ids = self.resolvable_experiment_ids(context_text)
        resume_relevant = bool(RESUME_INTENT_PATTERN.search(context_text))
        direct_capabilities = (
            [ACTION_RESUME_EXPERIMENT]
            if len(experiment_ids) == 1 and resume_relevant
            else []
        )
        if ACTION_RESUME_EXPERIMENT in direct_capabilities:
            recommended_next_action = ACTION_RESUME_EXPERIMENT
        elif related_operations:
            recommended_next_action = "resolve_related_operation"
        else:
            recommended_next_action = ACTION_MISSING_CAPABILITY
        metadata: dict[str, Any] = {
            "retryable": False,
            "related_operations": list(related_operations),
            "direct_capabilities": direct_capabilities,
            "recommended_next_action": recommended_next_action,
        }
        if direct_capabilities:
            # The id the capability was offered for, already resolved. Whoever
            # acts on it must not have to guess which token was meant, and must
            # never re-derive it from different text.
            metadata["resume_experiment_id"] = experiment_ids[0]
        return metadata

    # ------------------------------------------------------------------
    # find_tool
    # ------------------------------------------------------------------

    async def resolve_find_tool(
        self,
        goal: str,
        *,
        session_id: str,
        current_task: str = "",
        task_logger: Any | None = None,
        expected_operation: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        clean_goal = goal.strip()[:1000]
        started = time.perf_counter()
        legacy_catalog_schema_chars = len(
            json.dumps(
                [spec.to_dict() for spec in self.operations_registry.catalog()],
                separators=(",", ":"),
            )
        )
        invalid_goal, error, guidance = invalid_find_tool_goal(clean_goal)
        if invalid_goal:
            return {
                "tool": TOOL_FIND_TOOL,
                "status": "invalid_goal",
                "goal": clean_goal,
                "error": error,
                "guidance": guidance,
                "reason": error or "The goal is not a concrete capability request.",
                "related_tools": [],
                "metrics": {
                    "candidate_count": 0,
                    "candidate_operations": [],
                    "main_selected_schema_chars": 0,
                    "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                    "resolver_input_chars": 0,
                    "resolver_output_chars": 0,
                    "resolver_llm_calls": 0,
                    "selected_operation": None,
                    "expected_operation": expected_operation,
                    "expected_status": expected_status,
                    "selection_correctness": selection_correctness(
                        status="invalid_goal",
                        selected_operation=None,
                        expected_operation=expected_operation,
                        expected_status=expected_status,
                    ),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            }
        candidates = self.candidate_operations(clean_goal)
        if not candidates:
            return {
                "tool": TOOL_FIND_TOOL,
                "status": "not_found",
                "goal": clean_goal,
                "reason": "No read-only OperationsRegistry candidate matched the requested capability.",
                "related_tools": [],
                **self.find_tool_not_found_metadata(current_task, clean_goal, []),
                "metrics": {
                    "candidate_count": 0,
                    "candidate_operations": [],
                    "main_selected_schema_chars": 0,
                    "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                    "resolver_input_chars": 0,
                    "resolver_output_chars": 0,
                    "resolver_llm_calls": 0,
                    "selected_operation": None,
                    "expected_operation": expected_operation,
                    "expected_status": expected_status,
                    "selection_correctness": selection_correctness(
                        status="not_found",
                        selected_operation=None,
                        expected_operation=expected_operation,
                        expected_status=expected_status,
                    ),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            }
        prompt = resolver_prompt(clean_goal, candidates)
        try:
            raw, provider_used = await self._schema_completion(
                prompt,
                session_id=session_id,
                max_tokens=250,
                response_format=TOOL_RESOLVER_RESPONSE_FORMAT,
                role="tool_resolver",
                task_logger=task_logger,
            )
            resolver_unavailable = False
        except ToolResolverUnavailableError as exc:
            raw = json.dumps(
                {
                    "status": "unavailable",
                    "operation": "",
                    "reason": exc.reason[:500],
                    "related_tools": [spec.name for spec in candidates[:3]],
                },
                separators=(",", ":"),
            )
            provider_used = exc.provider
            resolver_unavailable = True

        candidate_by_name = {spec.name: spec for spec in candidates}
        try:
            parsed = extract_json(raw)
        except LlamaCppClientError:
            parsed = {
                "status": "unavailable",
                "operation": "",
                "reason": "Tool resolver returned invalid JSON.",
                "related_tools": [spec.name for spec in candidates[:3]],
            }
            resolver_unavailable = True
        if not isinstance(parsed, dict):
            parsed = {
                "status": "unavailable",
                "operation": "",
                "reason": "Tool resolver response was not a JSON object.",
                "related_tools": [spec.name for spec in candidates[:3]],
            }
            resolver_unavailable = True
        status = str(parsed.get("status", "")).strip()
        operation = str(parsed.get("operation", "")).strip()
        reason = str(parsed.get("reason", "")).strip()[:1000] or "Tool resolver did not provide a reason."
        resolver_output_chars = len(raw)
        resolver_llm_calls = 0 if provider_used == "none" else 1
        if status == "found" and operation in candidate_by_name:
            selected = candidate_by_name[operation].to_dict()
            observation = {
                "tool": TOOL_FIND_TOOL,
                "status": "found",
                "goal": clean_goal,
                "resolved_tool": operation,
                "arguments_schema": selected.get("arguments", []),
                "operation": selected,
                "reason": reason,
                "metrics": {
                    "candidate_count": len(candidates),
                    "candidate_operations": [spec.name for spec in candidates],
                    "main_selected_schema_chars": len(
                        json.dumps(selected, separators=(",", ":"))
                    ),
                    "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                    "resolver_input_chars": len(prompt),
                    "resolver_output_chars": resolver_output_chars,
                    "resolver_llm_calls": resolver_llm_calls,
                    "resolver_provider": provider_used,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "selected_operation": operation,
                    "expected_operation": expected_operation,
                    "expected_status": expected_status,
                    "selection_correctness": selection_correctness(
                        status="found",
                        selected_operation=operation,
                        expected_operation=expected_operation,
                        expected_status=expected_status,
                    ),
                },
            }
            self.grant_ledger.record_grant(session_id, observation)
            return observation
        related = parsed.get("related_tools")
        if not isinstance(related, list):
            related = [spec.name for spec in candidates[:3]]
        related_tools = [
            str(item) for item in related
            if str(item) in candidate_by_name
        ][:3]
        if status == "invalid_goal":
            final_status = "invalid_goal"
            final_reason = parsed.get("error") or reason or "The goal is not a concrete capability request."
            final_guidance = parsed.get("guidance") or "Describe the operation you need, e.g. 'run a shell command in the repository'."
            final_error = parsed.get("error") or final_reason
        else:
            final_status = "not_found" if status == "not_found" and not resolver_unavailable else "unavailable"
            final_reason = (
                reason
                if final_status == "not_found" or resolver_unavailable
                else f"Resolver selected unavailable operation {operation!r}."
            )
            final_guidance = "Describe the operation you need, e.g. 'run a shell command in the repository'."
            final_error = None
        return {
            "tool": TOOL_FIND_TOOL,
            "status": final_status,
            "goal": clean_goal,
            "reason": final_reason,
            "error": final_error,
            "guidance": final_guidance,
            "related_tools": related_tools,
            **(
                self.find_tool_not_found_metadata(current_task, clean_goal, related_tools)
                if final_status == "not_found"
                else {}
            ),
            "metrics": {
                "candidate_count": len(candidates),
                "candidate_operations": [spec.name for spec in candidates],
                "main_selected_schema_chars": 0,
                "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                "resolver_input_chars": len(prompt),
                "resolver_output_chars": resolver_output_chars,
                "resolver_llm_calls": resolver_llm_calls,
                "resolver_provider": provider_used,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "selected_operation": operation or None,
                "expected_operation": expected_operation,
                "expected_status": expected_status,
                "selection_correctness": selection_correctness(
                    status=final_status,
                    selected_operation=operation or None,
                    expected_operation=expected_operation,
                    expected_status=expected_status,
                ),
            },
        }

    # ------------------------------------------------------------------
    # call_tool
    # ------------------------------------------------------------------

    def execute_operation(self, decision: dict[str, Any]) -> dict[str, Any]:
        operation = str(decision.get("operation", "")).strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            raise ToolRouterError("operation args must be an object")
        available = {spec.name for spec in self.read_only_operation_specs()}
        if operation not in available:
            raise ToolRouterError(f"operation {operation!r} is not available")
        grant = self.grant_ledger.latest_grant(
            str(decision.get("session_id", "")).strip(),
            operation,
        )
        if grant is None:
            return {
                "tool": "operation",
                "operation": operation,
                "args": args,
                "error": "operation_not_granted",
                "reason": (
                    "Operation execution requires a prior successful find_tool result "
                    "for the same persistent Nexus session."
                ),
            }
        result = self.operations_registry.execute(
            operation,
            args,
            caller=self.caller,
        )
        payload = result.to_dict()
        payload["output_lines"] = [
            str(line)[:500]
            for line in list(payload.get("output_lines") or [])[:80]
        ]
        return {
            "tool": "operation",
            "operation": operation,
            "args": args,
            "grant_event_id": grant["event_id"],
            "operation_schema": grant["schema"],
            "result": payload,
        }
