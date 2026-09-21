"""One MCP-style tool mechanism, shared by every Nexus LLM role (#172).

Tool discovery (``find_tool``) and read-only tool execution (``call_tool``)
used to live inside :class:`~modules.task_orchestrator.NexusTaskOrchestrator`,
which meant only the persistent controller could reach them. Every other
Nexus role - failure analysis first among them - could describe evidence it
had been handed but never go and *check* anything, so a stale persisted
error string was indistinguishable from a current one.

This module holds that mechanism instead, so the controller and the other
roles drive the same code: the same ``OperationsRegistry`` catalog, the same
declared read-only boundary, the same candidate scoring, the same resolver
prompt and schema, the same "``call_tool`` needs a prior ``find_tool`` grant"
rule, and the same registry-side validation, redaction and audit logging.
Roles differ only in what they are *allowed* to do with it, not in how it
works.

What "read-only" means is declared by each capability (``mutates_state`` on
its :class:`~modules.operations_registry.OperationSpec`) and never inferred
from a list of known-mutating names. A denylist would make a capability
added later read-only by default, so a role that may only read would gain
privileges nobody granted it; declaring access per capability means a new
mutating operation is invisible to that role with no central list to
maintain.

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

from modules.llama_cpp_client import (
    LlamaCppClientError,
    extract_json,
    json_schema_response_format,
)
from modules.prompt_budget import prompt_json
from modules.redaction import redact_secrets
# From the leaf, never from `operations_registry` itself: that module imports
# mailbox_processor, which reaches back here, so importing it from this side
# closes the cycle and half-builds whichever module loaded first.
from modules.operation_contract import OperationValidationError, normalize_integer_argument

logger = logging.getLogger(__name__)

TOOL_FIND_TOOL = "find_tool"
ACTION_CALL_TOOL = "call_tool"

# Action names the not-found metadata below can point a caller at. Defined
# here rather than imported from the orchestrator so the dependency runs one
# way only (orchestrator -> router); the orchestrator re-exports them.
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_MISSING_CAPABILITY = "missing_capability"
ACTION_COMMAND = "command"
TOOL_PYTHON_RUN = "python_run"
ACTION_RESOLVE_RELATED_OPERATION = "resolve_related_operation"

MAX_RESOLVER_CANDIDATES = 5

# How many alternatives a terminal not_found carries, and how much of each
# description it keeps. The caller is a small local model reading this inside
# an already-budgeted prompt, so "everything we know" would push the rest of
# the observations out; the best few, fully described, are what it can act on.
MAX_RELATED_OPERATIONS = 3
MAX_RESOLVER_RAW_CHARS = 600
MAX_RELATED_DESCRIPTION_CHARS = 300
MAX_WHY_RELEVANT_TERMS = 5

# The example a caller is pointed at when its goal was unusable. It must name
# a capability find_tool can actually resolve: the goal that produced #218
# ("run a shell command in the repository") was copied verbatim out of this
# guidance and out of the controller prompt's example, and shell execution is
# a controller-native action that the read-only OperationsRegistry will never
# contain - so the illustration itself guaranteed a not_found.
# Shown only *after* a goal was rejected, as the corrective example. It has to
# be a goal the resolver actually resolves: the previous one was rejected by
# the resolver itself as "too generic", so the correction pointed at another
# dead end. This wording is one of the standing evaluation cases and resolves
# to git_status.
FIND_TOOL_GOAL_EXAMPLE = "inspect whether the Nexus repository has local changes"
# What the controller prompt shows in its place. The small local model copies
# the example goal verbatim - it did so with the old one, and it did so again
# with this one, which the resolver then rejected as too generic after a
# three-minute call. A placeholder that cannot be mistaken for a goal is caught
# here instead, in microseconds, with guidance to describe the real capability.
FIND_TOOL_GOAL_PLACEHOLDER = "<the concrete capability this task needs>"
FIND_TOOL_GOAL_GUIDANCE = (
    f"Describe the operation you need, e.g. '{FIND_TOOL_GOAL_EXAMPLE}'."
)

# What a terminal not_found tells the caller to do instead of asking again.
NOT_FOUND_GUIDANCE = (
    "Do not repeat find_tool with the same goal. Choose one of related_operations "
    "by its description and argument schema, or a listed direct capability, or "
    "choose missing_capability when none of them can satisfy the need."
)

# A goal that asks to *run* something rather than to read a specific piece of
# Nexus state. find_tool searches the read-only OperationsRegistry only, so it
# can never resolve one of these - but the controller owns both natively, and
# saying which one stops the caller searching for it a second time.
#
# Anchored on the verb, never on the noun. "inspect the python module versions
# installed" and "show the command line arguments of the running process" are
# read-only lookups that merely mention python and commands; answering either
# with "execute this yourself" would turn a request to read state into a
# request to run something, which is the one direction that must never be
# guessed.
_EXECUTION_VERBS = r"(?:run|runs|running|execute|executes|executing|invoke|invokes|invoking)"
_SHELL_GOAL_PATTERN = re.compile(
    rf"\b{_EXECUTION_VERBS}\b[^.]{{0,24}}\b(?:shell|bash|zsh|terminal|commands?|command[- ]line)\b",
    re.IGNORECASE,
)
_PYTHON_GOAL_PATTERN = re.compile(
    rf"\b{_EXECUTION_VERBS}\b[^.]{{0,24}}\bpython\b",
    re.IGNORECASE,
)

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

TOOL_RESOLVER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["found", "invalid_goal", "not_found", "unavailable"]},
        "operation": {"type": "string"},
        "reason": {"type": "string"},
        "related_tools": {"type": "array", "items": {"type": "string"}},
        "arguments": {"type": "object"},
        "error": {"type": "string"},
        "guidance": {"type": "string"},
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}
TOOL_RESOLVER_RESPONSE_FORMAT: dict[str, Any] = json_schema_response_format(
    "tool_resolver", TOOL_RESOLVER_SCHEMA
)

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


def is_read_only(spec: Any) -> bool:
    """Whether *spec* has declared itself safe for a read-only role.

    Read-only is a declared property of a capability, never one inferred from
    a list of known-bad names. A denylist gets the default backwards: an
    operation added later is read-only until somebody remembers to exclude
    it, so the privilege of a role that may only read grows on its own. Here
    a spec that does not declare ``mutates_state`` is treated as mutating -
    the direction that needs a decision is the unsafe one - which is why a
    new mutating operation is invisible to such a role without any central
    list being edited.
    """
    declared = getattr(spec, "mutates_state", None)
    if not isinstance(declared, bool):
        return False
    return not declared


def tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in TOOL_SEARCH_STOP_WORDS
    }


def invalid_find_tool_goal(goal: str) -> tuple[bool, str | None, str | None]:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(goal or "").lower()).strip()
    if not normalized:
        return True, "The goal is empty or whitespace only.", FIND_TOOL_GOAL_GUIDANCE

    placeholder_values = {
        "the concrete capability this task needs",
        "one concrete capability this task needs in your own words",
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
        return True, "The goal is a placeholder, not a concrete capability request.", FIND_TOOL_GOAL_GUIDANCE

    if "plain language" in normalized or "plain-language" in normalized:
        if "capability needed" in normalized or "goal" in normalized:
            return True, "The goal is a placeholder, not a concrete capability request.", FIND_TOOL_GOAL_GUIDANCE
    if "placeholder" in normalized or "example" in normalized:
        if "goal" in normalized or "capability" in normalized or "request" in normalized:
            return True, "The goal is a placeholder, not a concrete capability request.", FIND_TOOL_GOAL_GUIDANCE
    if "capability needed" in normalized and ("plain" in normalized or "placeholder" in normalized or "example" in normalized):
        return True, "The goal is a placeholder, not a concrete capability request.", FIND_TOOL_GOAL_GUIDANCE

    generic_tokens = {"capability", "goal", "operation", "request", "tool", "task", "need", "needs"}
    goal_tokens = normalized.split()
    if len(goal_tokens) <= 2 and set(goal_tokens) <= generic_tokens:
        return True, "The goal is too generic to describe a concrete capability request.", FIND_TOOL_GOAL_GUIDANCE
    return False, None, None


def controller_native_capabilities(goal: str) -> list[str]:
    """Controller actions a goal is asking for that find_tool cannot resolve.

    ``find_tool`` only ever searches the read-only OperationsRegistry, so a
    goal about *running* a shell command or a Python script is unresolvable by
    construction however it is worded. Naming the action that does own it turns
    a dead end into a next step - and it is exactly the step missing from #218,
    where the controller asked for shell execution, was told "not found", and
    asked again.
    """
    text = str(goal or "")
    native: list[str] = []
    if _PYTHON_GOAL_PATTERN.search(text):
        native.append(TOOL_PYTHON_RUN)
    if _SHELL_GOAL_PATTERN.search(text):
        native.append(ACTION_COMMAND)
    return native


def why_relevant(goal: str, spec: Any) -> str:
    """Why this alternative was offered, in terms the caller can check.

    Derived from the overlap the candidate scorer itself used, so the answer is
    the real reason the operation is on the list rather than a rationalisation
    written afterwards.
    """
    spec_tags = " ".join(str(tag) for tag in getattr(spec, "tags", []) or [])
    haystack = f"{spec.name} {spec.description} {spec_tags}"
    shared = sorted(tokens(goal) & tokens(haystack))[:MAX_WHY_RELEVANT_TERMS]
    if shared:
        return "Matches the requested goal on: " + ", ".join(shared) + "."
    return "Offered by the tool resolver as one of the closest available capabilities."


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
    """The unresolved answer this exact lookup already produced, if any.

    ``not_found`` and ``unavailable`` both count. Only the first is settled,
    but the caller reaches this function having asked the identical question a
    second time with nothing about the state changed - which the loop guard
    refuses either way. Recognising both is what lets that refusal carry the
    alternatives instead of a bare verdict, and what lets the repeat be
    redirected rather than spent.
    """
    return next(
        (
            observation
            for observation in reversed(observations)
            if observation.get("tool") == TOOL_FIND_TOOL
            and observation.get("status") in {"not_found", "unavailable"}
            and observation.get("state_token") == state_token
            and str(observation.get("goal", "")).strip() == goal
        ),
        None,
    )


def terminal_not_found_guidance(
    prior: dict[str, Any],
    resolved: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a refused repeat into an answer the caller can act on.

    The loop guard says "you already called this"; on its own that leaves
    the caller to guess what else to do, which is how the loop in #164
    kept going. This carries the terminal lookup's alternatives back.
    """
    guidance: dict[str, Any] = {
        "status": "not_found",
        "retryable": False,
        "goal": str(prior.get("goal", "")),
        "related_tools": list(prior.get("related_tools") or []),
        "related_operations": list(prior.get("related_operations") or []),
        "direct_capabilities": list(prior.get("direct_capabilities") or []),
        "recommended_next_action": prior.get("recommended_next_action"),
        "guidance": str(prior.get("guidance") or NOT_FOUND_GUIDANCE),
        "reason": (
            "An identical find_tool lookup already returned not_found "
            "without a relevant state change."
        ),
    }
    recommended_operation = str(prior.get("recommended_operation") or "").strip()
    if recommended_operation:
        guidance["recommended_operation"] = recommended_operation
    if resolved is not None:
        # The alternative is not merely suggested any more - it was resolved,
        # and in the ordinary path already executed. Saying so is the
        # difference between "try this" and "you have already done this".
        guidance["already_resolved_operation"] = str(resolved.get("resolved_tool", ""))
        guidance["already_resolved_event_id"] = resolved.get("event_id")
        guidance["reason"] = (
            "An identical find_tool lookup already returned not_found, and the "
            f"operation it recommended ({resolved.get('resolved_tool')}) has already "
            "been resolved in this run. Use that result instead of looking again."
        )
    return guidance


def operation_already_resolved(
    operation: str,
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The lookup that already resolved *operation* in this run, if any.

    A redirect exists to turn a dead question into an answerable one. Once the
    operation it points at has been resolved - and, in the ordinary path, run -
    pointing at it again asks a question this run has already answered, and the
    loop guard refuses the repeat as a duplicate. That is the loop the redirect
    was supposed to prevent, arriving one step later (#218).
    """
    if not operation:
        return None
    return next(
        (
            observation
            for observation in reversed(observations)
            if observation.get("tool") == TOOL_FIND_TOOL
            and observation.get("status") == "found"
            and str(observation.get("resolved_tool", "")) == operation
        ),
        None,
    )


def related_operation_lookup_goal(prior: dict[str, Any]) -> str:
    """A concrete find_tool goal aimed at the alternative already offered.

    The dead goal cannot be asked again, but the operation it was answered with
    can be looked up on its own terms. Built from the descriptor the terminal
    lookup already returned, so the redirected lookup can only ever target an
    operation this session was actually offered.
    """
    operation = str(prior.get("recommended_operation") or "").strip()
    if not operation:
        return ""
    descriptor = next(
        (
            item
            for item in prior.get("related_operations") or []
            if isinstance(item, dict) and str(item.get("operation", "")) == operation
        ),
        None,
    )
    description = str((descriptor or {}).get("description") or "").strip()
    goal = f"{operation}: {description}" if description else operation
    return goal[:1000]


# A number the goal names must be consumed by some argument. If one is left
# over while an integer argument stayed unbound, a value was missed - and the
# unbound argument would silently fall back to its default.
_GOAL_NUMBER_PATTERN = re.compile(r"\d+")


def _coerce_argument(argument: Any, value: Any) -> tuple[Any, str]:
    """One resolver-supplied value, checked against its declared schema."""
    name = getattr(argument, "name", "argument")
    if (getattr(argument, "type", "") or "").strip().lower() == "integer":
        try:
            coerced = normalize_integer_argument(argument, value, validate_bounds=False)
        except OperationValidationError:
            return None, f"{name} must be an integer"
        minimum = getattr(argument, "minimum", None)
        maximum = getattr(argument, "maximum", None)
        if minimum is not None and coerced < minimum:
            return None, f"{name} is below its minimum"
        if maximum is not None and coerced > maximum:
            return None, f"{name} is above its maximum"
        return coerced, ""
    if not isinstance(value, str) or not value.strip():
        return None, f"{name} must be a non-empty string"
    return value.strip(), ""


def _evidenced_in_goal(value: Any, goal: str) -> bool:
    """Whether *value* was read out of *goal* rather than invented.

    The schema can say a value is well-formed; only the goal can say it is the
    value that was asked for. ``service="agent-api"`` validates perfectly while
    the goal said ``llama-server``, so well-formedness alone is not a check.
    Numbers must match as whole tokens - otherwise ``limit=5`` would find its
    evidence inside the ``50`` of "the last 50 commits".
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return re.search(rf"(?<!\d){value}(?!\d)", goal) is not None
    text = str(value).strip().lower()
    return bool(text) and text in goal.lower()


def bind_resolved_arguments(
    goal: str, spec: Any, raw: Any
) -> tuple[dict[str, Any] | None, str]:
    """Argument values a resolved operation may be executed with, or ``None``.

    ``None`` means the values cannot be trusted to represent the goal, and the
    caller must fall back to asking rather than run something plausible. Every
    rule here exists because the alternative is evidence that looks right about
    the wrong subject - which a diagnosis cannot detect (#177 review):

    * an unknown argument name, or a value the declared schema rejects;
    * a value that does not appear in the goal, i.e. one the resolver invented;
    * a required argument left unbound;
    * *nothing* bound for an operation that takes arguments. Binding nothing
      would mean executing on defaults, and "the goal did not say" is not the
      same claim as "the goal asked for the default": that is exactly how
      ``service_status for llama-server`` would have read ``agent-api``;
    * a number left over in the goal while an integer argument stayed unbound,
      which says a value was named but not picked up.

    An operation that declares no arguments binds ``{}`` and stays directly
    executable, since there is nothing about it a goal could disagree with.
    """
    by_name = {
        getattr(argument, "name", ""): argument
        for argument in (getattr(spec, "arguments", None) or [])
    }
    supplied = raw if isinstance(raw, dict) else {}
    unknown = sorted(name for name in supplied if name not in by_name)
    if unknown:
        return None, f"resolver returned arguments that do not exist: {', '.join(unknown[:5])}"
    if not by_name:
        return {}, ""

    bound: dict[str, Any] = {}
    for name, value in supplied.items():
        coerced, error = _coerce_argument(by_name[name], value)
        if error:
            return None, error
        if not _evidenced_in_goal(coerced, goal):
            return None, f"the goal does not contain the resolved value for {name}"
        bound[name] = coerced

    missing = sorted(
        name for name, argument in by_name.items()
        if getattr(argument, "required", False) and name not in bound
    )
    if missing:
        return None, f"required arguments were not bound: {', '.join(missing)}"
    if not bound:
        return None, "the operation takes arguments and the goal bound none of them"

    leftover = set(_GOAL_NUMBER_PATTERN.findall(goal)) - {str(value) for value in bound.values()}
    unbound_integers = [
        name for name, argument in by_name.items()
        if name not in bound and (getattr(argument, "type", "") or "").lower() == "integer"
    ]
    if leftover and unbound_integers:
        return None, "the goal names a number that no argument accounted for"
    return bound, ""


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
{prompt_json(candidate_summaries)}

Rules:
- Choose status "found" only when one candidate directly satisfies the goal.
- Choose status "invalid_goal" when the goal is empty, generic, or clearly a placeholder/example rather than a concrete capability request.
- Choose status "not_found" when none is suitable.
- If found, set operation to the exact candidate operation name.
- If found and the goal names concrete values for that candidate's arguments,
  set "arguments" to exactly those values, keyed by the argument names in that
  candidate's schema. Copy each value from the goal; never invent one, and
  never fill one in from a default.
- Leave "arguments" empty when the goal names no argument values.
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
        """The capabilities a read-only role may discover and call.

        The whole surface of that role: ``find_tool`` resolves against this
        list and ``call_tool`` refuses anything outside it, so an operation
        that has not declared itself read-only is neither discoverable nor
        executable here.
        """
        return [
            spec for spec in self.operations_registry.catalog()
            if is_read_only(spec)
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

    def related_operation_descriptors(
        self,
        goal: str,
        related_operations: list[str],
    ) -> list[dict[str, Any]]:
        """The alternatives, described well enough to be chosen between.

        A bare name is not an alternative: a small local model handed
        ``["git_status", "git_log"]`` has nothing to compare against the goal
        it just failed to resolve, which is why #218's controller asked the
        same dead question again instead of picking one. The description,
        the relevance and the argument schema all come from the registry's own
        :class:`~modules.operations_registry.OperationSpec`, so they cannot
        drift from what the operation actually does or actually takes - and a
        name with no spec behind it is dropped rather than described from
        guesswork.
        """
        specs = {spec.name: spec for spec in self.read_only_operation_specs()}
        descriptors: list[dict[str, Any]] = []
        for name in list(related_operations)[:MAX_RELATED_OPERATIONS]:
            spec = specs.get(str(name))
            if spec is None:
                continue
            descriptors.append(
                {
                    "operation": spec.name,
                    "description": str(spec.description or "")[:MAX_RELATED_DESCRIPTION_CHARS],
                    "why_relevant": why_relevant(goal, spec),
                    "arguments": [
                        argument.to_dict()
                        for argument in getattr(spec, "arguments", []) or []
                    ],
                }
            )
        return descriptors

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
        # Read out of the goal alone. The task text describes the work, not the
        # lookup, and "fix github issue #196" mentioning a command somewhere in
        # its body says nothing about what this find_tool asked for.
        native_capabilities = [
            capability
            for capability in controller_native_capabilities(goal)
            if capability not in direct_capabilities
        ]
        direct_capabilities = direct_capabilities + native_capabilities
        descriptors = self.related_operation_descriptors(goal or "", list(related_operations))
        if ACTION_RESUME_EXPERIMENT in direct_capabilities:
            recommended_next_action = ACTION_RESUME_EXPERIMENT
        elif native_capabilities:
            # Ahead of the related operations on purpose. When the goal asks to
            # run something, the action that owns it is the answer; the scorer
            # will still have produced candidates ("run a shell command to
            # execute the test suite" scores every git_* operation), and
            # recommending one of those would answer a request to run the tests
            # with the output of `git status` - right-looking evidence about the
            # wrong subject.
            recommended_next_action = native_capabilities[0]
        elif descriptors:
            recommended_next_action = ACTION_RESOLVE_RELATED_OPERATION
        else:
            recommended_next_action = ACTION_MISSING_CAPABILITY
        metadata: dict[str, Any] = {
            "retryable": False,
            "related_operations": descriptors,
            "direct_capabilities": direct_capabilities,
            "recommended_next_action": recommended_next_action,
            "guidance": NOT_FOUND_GUIDANCE,
        }
        if recommended_next_action == ACTION_RESOLVE_RELATED_OPERATION:
            # The single best alternative, named once. Whoever acts on
            # ``resolve_related_operation`` must not have to re-rank the list
            # and must not be able to pick a name that is not on it. Set only
            # when resolving one of them really is the recommendation, so a
            # goal answered with a native capability cannot be redirected into
            # an operation nobody recommended.
            metadata["recommended_operation"] = descriptors[0]["operation"]
        if ACTION_RESUME_EXPERIMENT in direct_capabilities:
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
        # Case-folded: a small local model that answers "Found" has given the
        # same answer as one that answers "found", and throwing its correct
        # selection away over the spelling is not a safety property.
        status = str(parsed.get("status", "")).strip().lower()
        operation = str(parsed.get("operation", "")).strip()
        reason = str(parsed.get("reason", "")).strip()[:1000] or "Tool resolver did not provide a reason."
        resolver_output_chars = len(raw)
        resolver_llm_calls = 0 if provider_used == "none" else 1
        if status == "found" and operation in candidate_by_name:
            selected = candidate_by_name[operation].to_dict()
            resolved_arguments, binding_reason = bind_resolved_arguments(
                clean_goal, candidate_by_name[operation], parsed.get("arguments")
            )
            observation = {
                "tool": TOOL_FIND_TOOL,
                "status": "found",
                "goal": clean_goal,
                "resolved_tool": operation,
                "arguments_schema": selected.get("arguments", []),
                # The values this operation may run with, or None when they
                # could not be tied back to the goal. A caller that executes
                # without asking first must consult this, not the schema.
                "resolved_arguments": resolved_arguments,
                "arguments_binding_reason": binding_reason,
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
            final_guidance = parsed.get("guidance") or FIND_TOOL_GOAL_GUIDANCE
            final_error = parsed.get("error") or final_reason
        else:
            final_status = "not_found" if status == "not_found" and not resolver_unavailable else "unavailable"
            if final_status == "not_found" or resolver_unavailable:
                final_reason = reason
            elif operation and operation not in candidate_by_name:
                # The one case the old wording described. Kept exact, because
                # "it named something that does not exist" is a real and
                # different failure from the one below.
                final_reason = (
                    f"Resolver selected {operation!r}, which was not among the "
                    f"candidates it was offered."
                )
            elif operation:
                # What actually happened in #218's manual run: the resolver
                # named a perfectly available operation, but its status field
                # was not one this contract accepts. The old wording blamed the
                # operation - "Resolver selected unavailable operation
                # 'git_status'" - while git_status was right there in the
                # candidate list, which sent the diagnosis after the wrong bug.
                final_reason = (
                    f"Resolver returned status {status!r}, which is not one of "
                    f"found/not_found/invalid_goal, while naming the available "
                    f"operation {operation!r}. Its answer could not be used."
                )
            else:
                final_reason = (
                    f"Resolver returned status {status!r}, which is not one of "
                    f"found/not_found/invalid_goal, and named no operation."
                )
            final_guidance = FIND_TOOL_GOAL_GUIDANCE
            final_error = None
        return {
            "tool": TOOL_FIND_TOOL,
            "status": final_status,
            "goal": clean_goal,
            "reason": final_reason,
            "error": final_error,
            "guidance": final_guidance,
            "related_tools": related_tools,
            # Both unresolved outcomes carry the alternatives. A caller handed
            # "unavailable" with nothing but a bare name list has exactly the
            # dead end #218 was about - it cannot tell what git_status does or
            # what it takes - and the resolver having produced an unusable
            # answer is no reason to withhold the candidates it was choosing
            # between. `retryable` is what separates them: not_found is settled
            # for this goal, unavailable may yet succeed on a fresh attempt.
            **(
                {
                    **self.find_tool_not_found_metadata(current_task, clean_goal, related_tools),
                    **({"retryable": True} if final_status == "unavailable" else {}),
                }
                if final_status in {"not_found", "unavailable"}
                else {}
            ),
            # Bounded and redacted: when an answer could not be used, what the
            # model actually said is the only thing that explains why, and
            # reconstructing it from a reason string is guesswork.
            **(
                {"resolver_raw": redact_secrets(raw)[:MAX_RESOLVER_RAW_CHARS]}
                if final_status != "found"
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
        try:
            result = self.operations_registry.execute(
                operation,
                args,
                caller=self.caller,
            )
        except ValueError as exc:
            # The registry answers a failing operation with a failed result and
            # re-raises only argument validation, which is `OperationValidationError`
            # - a ValueError. Caught by base class rather than by import, to keep
            # the dependency running one way (registry is injected, never imported).
            #
            # "issue_number must be a positive integer" is a fact the caller can
            # act on: call it again with a real number. Letting it escape ended a
            # whole Nexus task with a traceback (#218).
            raise ToolRouterError(str(exc)) from exc
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
