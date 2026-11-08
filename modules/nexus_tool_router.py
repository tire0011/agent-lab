"""One MCP-style tool mechanism, shared by every Nexus LLM role (#172).

Tool discovery (``find_tool``) and read-only execution (``call_tool``) drive the same
``OperationsRegistry`` catalog, resolver prompt/schema, grant rule (``call_tool`` needs
a prior ``find_tool``), validation, redaction and audit for every role. "Read-only" is
declared per capability (``mutates_state``), never inferred from a denylist - a new
mutating operation is invisible to read-only roles by default. Inference
(*schema_completion*) and grants (*grant_ledger*) are injected per role; the grant
rule itself is enforced here for everyone.
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
# Import from the leaf, never from `operations_registry` itself: that module imports
# mailbox_processor, which reaches back here, so importing it from this side closes the
# cycle.
from modules.operation_contract import OperationValidationError, normalize_integer_argument

logger = logging.getLogger(__name__)

TOOL_FIND_TOOL = "find_tool"
ACTION_CALL_TOOL = "call_tool"

# Action names the not-found metadata can point at, defined here (not imported) so the
# dependency runs one way only: orchestrator -> router.
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_MISSING_CAPABILITY = "missing_capability"
ACTION_COMMAND = "command"
TOOL_PYTHON_RUN = "python_run"
ACTION_RESOLVE_RELATED_OPERATION = "resolve_related_operation"

MAX_RESOLVER_CANDIDATES = 5

# How many alternatives a terminal not_found carries and how much of each description: the
# caller is a small model inside an already-budgeted prompt - the best few, fully
# described, are what it can act on.
MAX_RELATED_OPERATIONS = 3
MAX_RESOLVER_RAW_CHARS = 600
MAX_RELATED_DESCRIPTION_CHARS = 300
MAX_WHY_RELEVANT_TERMS = 5

# The corrective example shown after an unusable goal: it must name a capability find_tool
# can actually resolve. The old example ("run a shell command in the repository") was copied
# verbatim by the model and guaranteed a not_found, since shell execution is
# controller-native and never in the read-only registry (#218).
FIND_TOOL_GOAL_EXAMPLE = "inspect whether the Nexus repository has local changes"
# The placeholder the controller prompt shows instead: the small model copies example goals
# verbatim, and one that cannot be mistaken for a goal is caught here in microseconds
# rather than after a three-minute resolver call.
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

# Goals that ask to *run* something, which find_tool can never resolve (read-only
# registry only); the controller owns those natively, and naming the owning action stops a
# second search. Anchored on the verb, never the noun: "inspect the python module versions"
# is a read-only lookup that merely mentions python - answering it with "execute this
# yourself" would turn a read request into a run request, the one direction that must
# never be guessed.
_EXECUTION_VERBS = r"(?:run|runs|running|execute|executes|executing|invoke|invokes|invoking)"
_SHELL_GOAL_PATTERN = re.compile(
    rf"\b{_EXECUTION_VERBS}\b[^.]{{0,24}}\b(?:shell|bash|zsh|terminal|commands?|command[- ]line)\b",
    re.IGNORECASE,
)
_PYTHON_GOAL_PATTERN = re.compile(
    rf"\b{_EXECUTION_VERBS}\b[^.]{{0,24}}\bpython\b",
    re.IGNORECASE,
)
_LOCAL_REPOSITORY_WORK_PATTERN = re.compile(
    r"\b(?:modify|edit|change|update|fix|implement|apply)\b"
    r"[^.]{0,80}\b(?:repository|repo|worktree|checkout)\b",
    re.IGNORECASE,
)
_LOCAL_CODE_CHANGE_PATTERN = re.compile(
    r"\b(?:modify|edit|change|update|fix|implement|apply|exclude|remove|filter|refactor|rewrite)\b"
    r"[^.]{0,100}\b(?:code|source|module|file|index|controller|orchestrator)\b",
    re.IGNORECASE,
)
_REMOTE_TARGET_PATTERN = re.compile(
    r"\b(?:github|confluence|remote\s+host|uploaded\s+spreadsheet|user\s+on\s+the\s+state)\b",
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
    "repository",
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

# A bare hex run is only a *candidate* experiment id: commit shas and dates match it too,
# so every candidate is resolved against the experiment service first (#164 review).
EXPERIMENT_ID_PATTERN = re.compile(r"\b[0-9a-f]{8,32}\b", re.IGNORECASE)
MAX_EXPERIMENT_ID_CANDIDATES = 5
RESUME_INTENT_PATTERN = re.compile(r"\b(resume|continue|continuing)\b", re.IGNORECASE)


class ToolRouterError(RuntimeError):
    """Bad tool arguments or an unavailable operation - a caller-side error."""


class ToolResolverUnavailableError(RuntimeError):
    """Inference for the resolver could not be performed.

    Carries the ``provider`` label and reason wording for the resulting ``unavailable``
    observation - the injected inference path, not this module, decides how its own
    failure modes read.
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
    """Grants that live exactly as long as one bounded run (e.g. failure analysis).

    Serves the shared rule: ``call_tool`` only ever runs an operation some ``find_tool``
    result already returned a schema for.
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
    """Whether *spec* declared itself safe for a read-only role.

    Read-only is a declared property, never inferred from known-bad names: a denylist gets
    the default backwards (a new operation is read-only until someone excludes it). A spec
    without ``mutates_state`` is treated as mutating - the direction that needs a decision
    is the unsafe one.
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
    """Controller actions a goal asks for that find_tool cannot resolve.

    ``find_tool`` only searches the read-only registry, so a goal about *running* something
    is unresolvable by construction however it is worded. Naming the action that owns it
    turns a dead end into a next step (#218).
    """
    text = str(goal or "")
    native: list[str] = []
    if _PYTHON_GOAL_PATTERN.search(text):
        native.append(TOOL_PYTHON_RUN)
    if _SHELL_GOAL_PATTERN.search(text) or (
        (
            _LOCAL_REPOSITORY_WORK_PATTERN.search(text)
            or _LOCAL_CODE_CHANGE_PATTERN.search(text)
        )
        and not _REMOTE_TARGET_PATTERN.search(text)
    ):
        native.append(ACTION_COMMAND)
    return native


def why_relevant(goal: str, spec: Any) -> str:
    """Why this alternative was offered, derived from the overlap the candidate scorer
    itself used - the real reason, not a post-hoc rationalisation.
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

    ``not_found`` and ``unavailable`` both count. Recognising both is what lets the loop
    guard's refusal of the identical repeat carry the alternatives instead of a bare
    verdict, and the repeat be redirected rather than spent.
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

    The loop guard says "you already called this"; without the terminal lookup's
    alternatives the caller just guesses what else to do - which is how the #164 loop
    kept going.
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
        # The alternative was not merely suggested - it was already resolved (and run): "try this"
        # vs "you have already done this".
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

    Once the operation a redirect points at has been resolved (and run), pointing at it
    again asks a question this run already answered, and the loop guard refuses the repeat
    - the loop the redirect was supposed to prevent, one step later (#218).
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
    """A concrete find_tool goal aimed at an alternative already offered.

    Built from the descriptor the terminal lookup returned, so the redirect can only target
    an operation this session was actually offered. The dead goal's wording is carried
    along because the resolver binds argument values only from the goal it is given - a
    descriptor-only redirect dropped the concrete values the caller had named ("read github
    issue #224" resolved without an ``issue_number``, #224).
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
    original_goal = str(prior.get("goal") or "").strip()
    if (descriptor or {}).get("arguments") and original_goal:
        if original_goal.startswith(f"{operation}:"):
            # The dead goal already is a redirect to this operation; wrapping it again would mint a new
            # goal every round and let the redirect chase itself - return it unchanged and let the
            # loop guard see the repeat.
            return original_goal[:1000]
        # The request goes first: the goal is cut at 1000 characters, and the values the request
        # names are what the binding needs to find.
        goal = f"{operation}: {original_goal} ({description})" if description else (
            f"{operation}: {original_goal}"
        )
    return goal[:1000]


# A number the goal names must be consumed by some argument; a leftover number with an
# unbound integer argument means a value was missed and the argument would silently fall
# back to its default.
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

    The schema can say a value is well-formed; only the goal can say it is the value asked
    for (``service="agent-api"`` validates perfectly while the goal said ``llama-server``).
    Numbers must match as whole tokens - otherwise ``limit=5`` would find its evidence
    inside the ``50`` of "the last 50 commits".
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return re.search(rf"(?<!\d){value}(?!\d)", goal) is not None
    text = str(value).strip().lower()
    return bool(text) and text in goal.lower()


def bind_resolved_arguments(
    goal: str, spec: Any, raw: Any
) -> tuple[dict[str, Any] | None, str]:
    """Argument values a resolved operation may be executed with, or ``None``.

    ``None`` means the values cannot be trusted to represent the goal - never run something
    plausible the goal did not ask for (#177 review). Rejected: unknown argument names,
    schema-invalid values, values absent from the goal (invented), unbound required
    arguments, *nothing* bound for an operation that takes arguments (executing on defaults
    is how ``llama-server`` would have read ``agent-api``), and a leftover number in the
    goal while an integer argument stayed unbound. An operation with no declared arguments
    binds ``{}`` and stays directly executable.
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
        controller: bool = False,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self._schema_completion = schema_completion
        self.grant_ledger: ToolGrantLedger = grant_ledger or InMemoryToolGrantLedger()
        self.caller = caller
        self.controller = controller


    def operation_specs(self) -> list[Any]:
        """What this caller may find and call: the read-only operations, plus
        the few marked ``controller_allowed`` when the caller is the controller."""
        return [
            spec for spec in self.operations_registry.catalog()
            if is_read_only(spec) or (self.controller and getattr(spec, "controller_allowed", False) is True)
        ]

    def candidate_operations(self, goal: str) -> list[Any]:
        goal_tokens = tokens(goal)
        scored: list[tuple[int, str, Any]] = []
        for spec in self.operation_specs():
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


    def resolvable_experiment_ids(self, text: str) -> list[str]:
        """Hex-looking tokens in ``text`` that are actually experiments.

        "Looks like hex" is not an identity: a commit sha or a date like ``20260913`` matches
        the same pattern, and only ids the experiment service can resolve count - handing a sha
        to ``resume_experiment`` turns an ordinary task into ``unknown experiment``. Resolution
        is bounded so a pathological input cannot turn one lookup into dozens of queries.
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
            # The resolved id must look like one too: a service that answers every lookup (a stub, a
            # stale cache) must not turn a commit sha into an "experiment".
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

        A bare name list gives a small local model nothing to compare against the goal it just
        failed to resolve (#218). Description, relevance and argument schema come from the
        registry's own ``OperationSpec`` so they cannot drift from what the operation does; a
        name with no spec behind it is dropped rather than described from guesswork.
        """
        specs = {spec.name: spec for spec in self.operation_specs()}
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

        Deliberately scoped to the current lookup, not the session history: an experiment id
        and the word "resume" left over from an earlier, unrelated request say nothing about
        what this task needs, and offering a capability on that basis points the caller at
        someone else's work.
        """
        context_text = " ".join(part for part in (task_text or "", goal or "") if part)
        experiment_ids = self.resolvable_experiment_ids(context_text)
        resume_relevant = bool(RESUME_INTENT_PATTERN.search(context_text))
        direct_capabilities = (
            [ACTION_RESUME_EXPERIMENT]
            if len(experiment_ids) == 1 and resume_relevant
            else []
        )
        # Read out of the goal alone: the task text describes the work, not this lookup ("fix github
        # issue #196" mentioning a command in its body says nothing about what find_tool asked for).
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
            # Native capabilities go ahead of related operations on purpose: when the goal asks to run
            # something, the owning action is the answer - recommending a git_* candidate for "run the
            # test suite" would be right-looking evidence about the wrong subject.
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
            # The single best alternative, named once: whoever acts on ``resolve_related_operation`` must
            # not re-rank the list or pick a name not on it; set only when resolving one really is the
            # recommendation.
            metadata["recommended_operation"] = descriptors[0]["operation"]
        if ACTION_RESUME_EXPERIMENT in direct_capabilities:
            # The id the capability was offered for, already resolved: the actor must not guess which
            # token was meant nor re-derive it from different text.
            metadata["resume_experiment_id"] = experiment_ids[0]
        return metadata


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
        # Case-folded: a model answering "Found" gave the same answer as "found"; discarding a
        # correct selection over spelling is not a safety property.
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
                # The values this operation may run with, or None when they could not be tied back to the
                # goal - a caller executing without asking must consult this, not the schema.
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
                # The one case the old wording described; kept exact because "named something that does not
                # exist" is a different failure from an unusable status field.
                final_reason = (
                    f"Resolver selected {operation!r}, which was not among the "
                    f"candidates it was offered."
                )
            elif operation:
                # What happened in #218's manual run: the resolver named an available operation whose
                # status field the contract did not accept; the old wording blamed the operation and sent
                # the diagnosis after the wrong bug.
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
            # Both unresolved outcomes carry the alternatives: a caller handed "unavailable" with only
            # a bare name list has exactly the #218 dead end. ``retryable`` separates them - not_found
            # is settled for this goal, unavailable may yet succeed on a fresh attempt.
            **(
                {
                    **self.find_tool_not_found_metadata(current_task, clean_goal, related_tools),
                    **({"retryable": True} if final_status == "unavailable" else {}),
                }
                if final_status in {"not_found", "unavailable"}
                else {}
            ),
            # Bounded and redacted: when an answer could not be used, what the model actually said is the
            # only thing that explains why - reconstructing it from a reason string is guesswork.
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


    def execute_operation(self, decision: dict[str, Any]) -> dict[str, Any]:
        operation = str(decision.get("operation", "")).strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            raise ToolRouterError("operation args must be an object")
        available = {spec.name for spec in self.operation_specs()}
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
            execute_kwargs: dict[str, Any] = {"caller": self.caller}
            invocation_id = decision.get("invocation_id")
            if invocation_id is not None:
                execute_kwargs["invocation_id"] = invocation_id
            result = self.operations_registry.execute(
                operation,
                args,
                **execute_kwargs,
            )
        except ValueError as exc:
            # The registry re-raises only argument validation (``OperationValidationError``, a
            # ValueError), caught by base class to keep the dependency one-way (registry injected,
            # never imported). "issue_number must be a positive integer" is a fact the caller can act
            # on - letting it escape ended a whole Nexus task with a traceback (#218).
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
