"""Shared constants, schemas, decision types and helpers of the Nexus task orchestrator."""

from __future__ import annotations

import json
import os
import re
from dataclasses import (
    dataclass,
    field,
)
from typing import Any
from modules.nexus_task_store import NexusTaskStore
from modules.llama_cpp_client import json_schema_response_format
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
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
    TOOL_PYTHON_RUN,
    TOOL_RESOLVER_RESPONSE_FORMAT,
    TOOL_SEARCH_STOP_WORDS,
)
from modules.tool_result_reader import TOOL_READ_TOOL_RESULT


ACTION_NEW_EXPERIMENT = "new_experiment"


ACTION_ANSWER = "answer"


ACTION_CLARIFICATION = "clarification"


ACTION_KNOWLEDGE_COLLECTOR = "knowledge_collector"


# Fallback delegates after a local inference failure: free registry models
# first (#552), then paid. ``local_worker`` is excluded - same llama-server.
_EXTERNAL_FALLBACK_ORDER: tuple[str, ...] = ("free_tier", "azure_openai", "codex_readonly")


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


# Code changes belong in an isolated experiment (#263 decision 1). The guard is
# deliberately narrow: it refuses only Git commands that *author* code history
# in the live checkout (commit, branch creation, patches, merges). File
# commands, package installs, `git pull`/`fetch`/`status`/`log`/`diff` and Git
# in any other directory stay available for machine work and reading.
_GIT_AUTHORING_COMMAND = re.compile(
    r"(?:^|[;&|(]\s*|\bsudo\s+)git"
    r"(?P<options>(?:\s+-C\s+\S+|\s+-c\s+\S+|\s+--git-dir(?:=|\s+)\S+|\s+--work-tree(?:=|\s+)\S+)*)"
    r"\s+(?:commit|am|apply|cherry-pick|merge|rebase|revert"
    r"|checkout\s+(?:\S+\s+)*-[bB]\b|switch\s+(?:\S+\s+)*(?:-[cC]\b|--create\b))",
    re.IGNORECASE,
)
_CD_TARGET = re.compile(r"(?:^|[;&|(]\s*)cd\s+(\S+)")


def _live_checkout_git_write(command: str, repo_root: str) -> bool:
    """Whether *command* would author code history in the live checkout *repo_root*.

    A command that first changes into another directory, or points Git at one
    with ``-C``/``--git-dir``/``--work-tree``, works elsewhere and is allowed.
    """
    root = os.path.normpath(str(repo_root))

    def _elsewhere(path: str) -> bool:
        path = path.strip("'\"")
        return os.path.normpath(path) != root and not os.path.normpath(
            os.path.join(root, path)
        ).startswith(root + os.sep) and path not in (".", "")

    for match in _GIT_AUTHORING_COMMAND.finditer(command):
        options = match.group("options") or ""
        targets = re.findall(r"(?:-C\s+|--git-dir(?:=|\s+)|--work-tree(?:=|\s+))(\S+)", options)
        if targets and all(_elsewhere(target) for target in targets):
            continue
        cds = _CD_TARGET.findall(command[: match.start()])
        if cds and _elsewhere(cds[-1]):
            continue
        return True
    return False


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
    TOOL_READ_TOOL_RESULT,
}


# Decision fields worth journalling; `message` can be a page of prose and
# adds nothing to "why did it choose this".
_DECISION_FIELDS = (
    "tool", "goal", "query", "request", "operation", "provider", "command",
    "script", "module", "experiment_id", "capability", "event_id", "offset",
)


_PROVIDER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "free_tier": ("free_tier", "azure_openai", "local_worker", "codex_readonly"),
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
                    TOOL_READ_TOOL_RESULT,
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
            "event_id": {"type": "integer"},
            "offset": {"type": "integer"},
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
    complexity: str | None = None
    # What Nexus' own new_experiment tool did (#263 step 4): an
    # ``ExperimentLaunch`` with status, experiment id and - if it did not
    # start - the reason. ``None`` when no experiment tool is configured.
    experiment_launch: Any | None = None


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

