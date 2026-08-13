"""Controlled, single-run self-development experiment service.

``ExperimentService`` orchestrates exactly one bounded self-improvement
attempt end to end:

  1. Selection phase — runs the existing ``CodexWorker`` in ``read-only``
     mode against ``/agent`` to have the agent pick and structurally
     describe exactly one small, testable improvement.
  2. Plan review — the structured plan is parsed and checked against a
     small hard denylist (secrets, package installs, destructive DB ops,
     commit/push/merge, out-of-repo changes, ...) plus an action-based
     infrastructure/deployment check: bare mentions of terms like "VM",
     "Azure", "deploy", or "infrastructure" are allowed on their own (the
     agent must be able to reason about and document such topics), and a
     plan is only rejected when an actual infra-changing *action* (create,
     provision, delete, destroy, resize, scale, deploy, ...) appears close
     to such a target noun, i.e. the plan describes real external effect
     rather than a passing mention. Anything that does not parse reliably
     or trips one of these checks ends the experiment as ``rejected``
     before any write access is granted.
  3. Isolated worktree — a new ``git worktree`` is created at a unique path
     with a unique local branch, starting from the currently checked-out
     commit. The main working directory (``/agent``) is never touched:
     no commit, push, merge, or checkout happens there.
  4. Implementation phase — the existing ``CodexWorker`` is run exactly
     once more, this time with ``sandbox=workspace-write`` but scoped to
     the worktree directory only (a worker instance whose ``allowed_roots``
     is limited to that single path). The prompt is built entirely from the
     reviewed plan; no free-form client prompt can reach this phase.
  5. Verification — ``git status --short`` and a combined diff against
     ``HEAD`` are captured for the worktree. The diff uses an isolated
     temporary index so tracked and untracked changes are included without
     modifying the real index (all Git calls use fixed argument lists).
     A test command is chosen only from a small server-side allowlist; if
     none applies, the experiment becomes ``needs_review`` instead of being
     scored as successful.

No commit, push, merge, or deployment is ever performed by this service.
Reuses ``modules.codex_worker.CodexWorker`` for both phases (no duplicated
subprocess/worker logic) and the existing SQLite store from
``modules.run_store`` (additive ``experiments`` table).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.codex_worker import CodexResult, CodexWorker
from modules.run_store import DEFAULT_DB_PATH, init_db, insert_activity, list_activity

# ---------------------------------------------------------------------------
# Status constants — small deterministic state machine.
# ---------------------------------------------------------------------------

_NEXUS_IDENTITY_PROMPT = """You are operating as a reasoning and execution worker for Nexus, the persistent agent system. This task originates from Nexus unless explicitly marked as a human instruction. Refer to the acting system as "Nexus", not as "the user". You are not independently Nexus; you are temporarily working on Nexus's behalf."""

def _with_nexus_identity(prompt: str) -> str:
    return f"{_NEXUS_IDENTITY_PROMPT}\n\n{prompt}"


STATUS_QUEUED = "queued"
STATUS_SELECTING = "selecting"
STATUS_REJECTED = "rejected"
STATUS_IMPLEMENTING = "implementing"
STATUS_VERIFYING = "verifying"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_NEEDS_REVIEW = "needs_review"

EXPERIMENT_STATUSES = (
    STATUS_QUEUED,
    STATUS_SELECTING,
    STATUS_REJECTED,
    STATUS_IMPLEMENTING,
    STATUS_VERIFYING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_NEEDS_REVIEW,
)

_TERMINAL_STATUSES = (
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_NEEDS_REVIEW,
)

# Allowed forward transitions. ``run_experiment`` walks this graph and
# refuses to run twice: only queued -> selecting is a valid entry point.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_QUEUED: (STATUS_SELECTING,),
    STATUS_SELECTING: (STATUS_REJECTED, STATUS_IMPLEMENTING, STATUS_FAILED, STATUS_TIMED_OUT),
    STATUS_IMPLEMENTING: (STATUS_VERIFYING, STATUS_FAILED, STATUS_TIMED_OUT),
    STATUS_VERIFYING: (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_NEEDS_REVIEW),
}

_DEFAULT_MAX_TEST_OUTPUT_BYTES = 32 * 1024
_DEFAULT_MAX_DIFF_BYTES = 64 * 1024
_DEFAULT_MAX_RAW_MESSAGE_BYTES = 16 * 1024
_DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES = 64 * 1024
_DEFAULT_RESEARCH_TIMEOUT_SECONDS = 120
_DEFAULT_PLAN_TIMEOUT_SECONDS = 90
_REFORMAT_RETRY_TIMEOUT_SECONDS = 60
_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 1200  # 20 minutes

# Test-command allowlist, keyed by a fixed identifier. The plan/LLM output
# never supplies a raw shell string; the service only ever chooses one of
# these fixed argv templates. "{python}" is substituted with the
# configured test interpreter (see ExperimentService.test_python_executable)
# so that worktrees without their own virtualenv can still run tests using
# the main repository's already-installed interpreter/site-packages,
# without copying a .venv or installing any package into the worktree.
_ALLOWED_TEST_COMMANDS: dict[str, list[str]] = {
    "unittest-discover": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
}
_DEFAULT_TEST_COMMAND_KEY = "unittest-discover"

# Default interpreter used to run tests inside an experiment worktree.
# Using the main repository's own virtualenv interpreter lets a fresh git
# worktree (which has no virtualenv of its own) run the existing test
# suite without copying any files or installing any packages — the
# worktree only needs to be used as the *working directory* (``cwd``) for
# ``python -m unittest``, since Python resolves ``tests/`` relative to cwd
# while the interpreter and its installed packages come from the venv path.
_DEFAULT_TEST_PYTHON_EXECUTABLE = "/agent/.venv/bin/python"

# Hard keyword denylist applied to the raw selection plan text. These are
# unambiguous by themselves — the bare word already implies the disallowed
# action (there is no legitimate "harmless mention" of e.g. "pip install"
# or "drop table" inside a plan) — so any hit rejects the experiment
# immediately, regardless of surrounding context. Matching is a simple
# case-insensitive substring check against the lowercased haystack.
# Secret/credential-related terms are intentionally NOT here: they are
# handled by the action-based _SECRET_ACTION_REGEXES check below, since a
# bare mention (e.g. "without secrets", "tests the redaction logic for
# tokens") is a common, safe statement that must not reject a plan.
_REJECT_KEYWORDS: tuple[str, ...] = (
    "commit", "push", "merge",
    "pip install", "npm install", "apt install", "apt-get install",
    "package install", "install package",
    "drop table", "delete from", "truncate table", "rm -rf",
    "shell=true",
    # Paid/costly actions: only the action words themselves (never bare
    # nouns like "budget" or "cost"), so mentioning cost considerations in
    # a plan's risk assessment does not accidentally reject it.
    "purchas",  # stem: matches purchase/purchasing/purchased/purchases
    "enable billing", "paid tier", "spend money",
    "increase budget", "increase the budget", "credit card",
)

# Action-based infrastructure/deployment check. Bare mentions of terms
# such as "VM", "Azure", "deploy", or "infrastructure" must NOT alone
# reject a plan (the agent needs to be able to read, discuss, and document
# such topics — e.g. fixing a VM version number in README.md). A plan is
# only rejected when one of these *action verbs* appears close to one of
# these *target nouns*, i.e. the plan actually describes creating,
# changing, deleting, or deploying real external infrastructure rather
# than just mentioning it. Proximity is word-count based (not a fixed
# character window) so an incidental mention of a noun in one sentence and
# an unrelated verb several sentences later does not trigger a false
# rejection.
_INFRA_ACTION_VERBS: tuple[str, ...] = (
    "create", "creates", "creating", "created",
    "provision", "provisions", "provisioning", "provisioned",
    "delete", "deletes", "deleting", "deleted",
    "destroy", "destroys", "destroying", "destroyed",
    "terminate", "terminates", "terminating", "terminated",
    "resize", "resizes", "resizing", "resized",
    "scale", "scales", "scaling", "scaled",
    "restart", "restarts", "restarting", "restarted",
    "stop", "stops", "stopping", "stopped",
    "start", "starts", "starting", "started",
    "modify", "modifies", "modifying", "modified",
    "configure", "configures", "configuring", "configured",
    "reconfigure", "reconfigures", "reconfiguring", "reconfigured",
    "change", "changes", "changing", "changed",
    "update", "updates", "updating", "updated",
    "install", "installs", "installing", "installed",
    "deploy", "deploys", "deploying", "deployed",
)

_INFRA_TARGET_NOUNS: tuple[str, ...] = (
    "vm", "virtual machine", "azure", "infrastructure", "nginx", "dns",
    "cloud resource", "resource group", "server", "subscription",
    "database instance", "storage account", "load balancer",
    "production", "prod", "environment", "website", "site",
)

# Maximum number of filler words allowed between the action verb and the
# target noun for the pair to still count as describing the same action
# (e.g. "delete the VM" -> 1 filler word; "resize the VM to Standard_B2s"
# -> 1 filler word). A plan that merely mentions "VM" in one sentence and
# an unrelated verb many words later is not considered a match.
_INFRA_ACTION_MAX_FILLER_WORDS = 6

_INFRA_VERB_ALT = "|".join(re.escape(v) for v in _INFRA_ACTION_VERBS)
_INFRA_NOUN_ALT = "|".join(re.escape(n) for n in _INFRA_TARGET_NOUNS)
_INFRA_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\b(?:{_INFRA_VERB_ALT})\b(?:\s+\S+){{0,{_INFRA_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_INFRA_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:{_INFRA_NOUN_ALT})\b(?:\s+\S+){{0,{_INFRA_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_INFRA_VERB_ALT})\b",
        re.IGNORECASE,
    ),
)

# Words whose presence between a matched verb and noun indicate the plan is
# actually *disclaiming* the action ("does not touch infrastructure",
# "will not deploy anything") rather than describing it. A raw proximity
# match alone cannot tell "create a VM" apart from "will not create a VM",
# so any negation word found inside the matched span turns off the
# rejection for that specific match (the search continues past it).
_INFRA_ACTION_NEGATION_WORDS = frozenset(
    {
        "not", "no", "never", "without", "cannot", "avoid", "don't",
        "doesn't", "won't", "wouldn't", "shouldn't", "didn't", "isn't",
        "aren't", "n't",
    }
)


def _infra_action_match_is_false_positive(matched_text: str, preceding_text: str = "") -> bool:
    """True if *matched_text* (a verb/noun proximity match) should NOT be
    treated as describing a real action — either because it crosses a
    sentence boundary (a later, unrelated sentence merely repeats the
    noun) or because it contains an explicit negation. ``preceding_text``
    (a small window of text immediately before the match) is also checked
    for a trailing negation word, so "without changing production" is
    recognized as a disclaimer even though "without" falls just outside
    the verb/noun match span itself."""
    if re.search(r"[.!?]\s", matched_text):
        return True
    words = re.findall(r"[a-z']+", matched_text.lower())
    if any(word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't") for word in words):
        return True
    if preceding_text:
        # Look for a negation anywhere since the start of the current
        # sentence/clause, not just the immediately preceding word: prose
        # like "without network access, secrets, or production changes"
        # lists several nouns after a single leading negation, so a
        # negation several comma-separated items earlier still applies to
        # a later item in the same list.
        sentence_start = 0
        for boundary in re.finditer(r"[.!?]\s|\b(?:but|however|although|though)\b", preceding_text):
            sentence_start = boundary.end()
        clause = preceding_text[sentence_start:]
        clause_words = re.findall(r"[a-z']+", clause.lower())
        if any(
            word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't")
            for word in clause_words
        ):
            return True
    return False


# Action-based secret/credential check. Bare mentions of terms such as
# "secret", "credential", "token", "api key", or "ssh key" must NOT alone
# reject a plan — legitimate plans routinely discuss redaction logic,
# assert they need no secrets, or test secret-scrubbing code (e.g. this
# very module's own _redact_secrets tests). A plan is only rejected when
# one of these *action verbs* appears close to one of these *target
# nouns*, i.e. the plan actually describes reading, obtaining, using,
# copying, exposing/logging, storing, transmitting, creating, changing, or
# deleting a real secret/credential rather than just mentioning the
# concept. Reuses the same word-count proximity, sentence-boundary, and
# negation-guard logic as the infrastructure/deployment check above (see
# ``_infra_action_match_is_false_positive``), so "without secrets",
# "does not access credentials", and "no API key required" are accepted.
_SECRET_ACTION_VERBS: tuple[str, ...] = (
    "read", "reads", "reading",
    "obtain", "obtains", "obtaining", "obtained",
    "fetch", "fetches", "fetching", "fetched",
    "retrieve", "retrieves", "retrieving", "retrieved",
    "use", "uses", "using", "used",
    "load", "loads", "loading", "loaded",
    "copy", "copies", "copying", "copied",
    "expose", "exposes", "exposing", "exposed",
    "leak", "leaks", "leaking", "leaked",
    "print", "prints", "printing", "printed",
    "log", "logs", "logging", "logged",
    "output", "outputs", "outputting",
    "store", "stores", "storing", "stored",
    "save", "saves", "saving", "saved",
    "transmit", "transmits", "transmitting", "transmitted",
    "send", "sends", "sending", "sent",
    "upload", "uploads", "uploading", "uploaded",
    "create", "creates", "creating", "created",
    "generate", "generates", "generating", "generated",
    "change", "changes", "changing", "changed",
    "rotate", "rotates", "rotating", "rotated",
    "delete", "deletes", "deleting", "deleted",
    "remove", "removes", "removing", "removed",
)

_SECRET_ACTION_NOUNS: tuple[str, ...] = (
    "secret", "secrets", "credential", "credentials",
    "api key", "api keys", "api_key", "apikey",
    "ssh key", "ssh keys", "private key", "private keys",
    "access token", "access tokens", "auth token", "auth tokens",
    "token", "tokens", "password", "passwords",
)

_SECRET_ACTION_MAX_FILLER_WORDS = 6

_SECRET_VERB_ALT = "|".join(re.escape(v) for v in _SECRET_ACTION_VERBS)
_SECRET_NOUN_ALT = "|".join(re.escape(n) for n in _SECRET_ACTION_NOUNS)
_SECRET_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\b(?:{_SECRET_VERB_ALT})\b(?:\s+\S+){{0,{_SECRET_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_SECRET_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:{_SECRET_NOUN_ALT})\b(?:\s+\S+){{0,{_SECRET_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_SECRET_VERB_ALT})\b",
        re.IGNORECASE,
    ),
)


# Title phrases that indicate a placeholder/intention-only response rather
# than a concrete, already-selected feature (e.g. the model announcing it
# is about to start investigating instead of reporting what it decided).
_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "inspection started",
    "analysis only",
    "investigation started",
    "investigating",
    "will inspect",
    "will investigate",
    "will analyze",
    "read-only analysis",
    "to be determined",
    "tbd",
    "placeholder",
    "repository inspection",
    "repo inspection",
    "code review only",
    "documentation only",
)

# File extensions/names considered "concrete code or test files" for the
# purpose of rejecting investigation-only or documentation-only plans. Any
# plan whose affected_files consists solely of non-code documents (e.g.
# only IDEAS.md/README.md) does not describe an implementable feature. This
# is intentionally an allowlist of source-like suffixes, not a denylist of
# documentation formats, so it never blocks a legitimate feature that
# happens to also touch documentation alongside real code/tests.
_CODE_OR_TEST_FILE_SUFFIXES: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".rb", ".sh", ".bash", ".ps1", ".sql", ".yml", ".yaml", ".toml",
    ".ini", ".cfg", ".json",
)


def _is_code_or_test_file(path: str) -> bool:
    """True if *path* looks like a concrete source, test, or config file
    rather than a plain documentation/notes file (e.g. IDEAS.md, README.md,
    or a bare directory name)."""
    name = Path(path).name.lower()
    if not name:
        return False
    return any(name.endswith(suffix) for suffix in _CODE_OR_TEST_FILE_SUFFIXES)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_text(text: str | None, max_bytes: int, label: str = "output") -> str | None:
    if text is None:
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return (
        encoded[:max_bytes].decode("utf-8", errors="replace")
        + f"\n[... {label} truncated]"
    )


# Event ``item.type`` values considered safe and useful for diagnosing why a
# selection/reformat response could not be parsed. Reasoning content and
# unbounded tool output are intentionally excluded: they can be large, can
# reflect internal chain-of-thought, and are not needed to debug a JSON
# extraction failure (the "agent_message" text is what parse_plan() actually
# consumes). "command_execution" items are included only in a redacted,
# metadata-only form (command + exit_code + status), never their full
# ``aggregated_output``, since that could contain arbitrary file contents.
_DIAGNOSTIC_EVENT_TYPES: frozenset[str] = frozenset(
    {"thread.started", "turn.started", "turn.completed"}
)
_DIAGNOSTIC_ITEM_TYPES: frozenset[str] = frozenset({"agent_message"})
_DIAGNOSTIC_TOOL_ITEM_TYPES: frozenset[str] = frozenset({"command_execution"})

# Simple, conservative secret-scrubbing patterns applied to any diagnostic
# text before it is persisted. Matches common credential/token shapes
# regardless of which environment variable they came from.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _extract_diagnostic_log(raw_stdout: str | None, max_bytes: int) -> str | None:
    """Build a small, redacted, size-limited diagnostic log from a raw
    Codex JSONL stdout stream.

    Only a safe subset of event/item types is kept (see
    ``_DIAGNOSTIC_EVENT_TYPES``/``_DIAGNOSTIC_ITEM_TYPES``/
    ``_DIAGNOSTIC_TOOL_ITEM_TYPES``); reasoning content and unbounded tool
    output are dropped entirely, and any residual secret-shaped text is
    scrubbed. The result is truncated to ``max_bytes`` with a clear marker
    (including the original byte count) if it does not fit.
    """
    if not raw_stdout or not raw_stdout.strip():
        return None

    kept_lines: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype in _DIAGNOSTIC_EVENT_TYPES:
            kept_lines.append(json.dumps(event, sort_keys=True))
            continue

        if etype == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in _DIAGNOSTIC_ITEM_TYPES:
                kept_lines.append(json.dumps(event, sort_keys=True))
            elif item_type in _DIAGNOSTIC_TOOL_ITEM_TYPES:
                # Metadata-only: command + exit_code + status. Never the
                # full aggregated_output (may contain file contents/secrets).
                safe_item = {
                    "id": item.get("id"),
                    "type": item_type,
                    "command": item.get("command"),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                }
                kept_lines.append(
                    json.dumps({"type": "item.completed", "item": safe_item}, sort_keys=True)
                )
            # All other item types (e.g. "reasoning") are dropped entirely.

    if not kept_lines:
        return None

    log_text = _redact_secrets("\n".join(kept_lines))
    encoded = log_text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return log_text
    original_bytes = len(encoded)
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return (
        truncated
        + f"\n[... diagnostic log truncated; original size {original_bytes} bytes]"
    )


class ExperimentNotFoundError(LookupError):
    """Raised when an experiment id does not exist in the store."""


class ExperimentNotRunnableError(RuntimeError):
    """Raised when ``run_experiment`` is called on an experiment already started."""


@dataclass
class ExperimentPlan:
    """Structured feature plan as selected by the read-only selection phase."""

    title: str
    motivation: str
    affected_files: list[str]
    expected_changes: str
    success_criteria: str
    planned_tests: str
    risks: str
    estimated_scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "motivation": self.motivation,
            "affected_files": self.affected_files,
            "expected_changes": self.expected_changes,
            "success_criteria": self.success_criteria,
            "planned_tests": self.planned_tests,
            "risks": self.risks,
            "estimated_scope": self.estimated_scope,
        }


# JSON Schema for the structured plan, passed to Codex via
# ``CodexWorker.run(output_schema=...)`` for both the selection phase and
# the reformat retry. Codex's own ``--output-schema`` support constrains
# the *shape* of the model's final response; the result is still fully
# re-validated with parse_plan()/plan_violates_policy() below (no
# semantic values are trusted or guessed just because the shape matched).
PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "motivation": {"type": "string"},
        "affected_files": {"type": "array", "items": {"type": "string"}},
        "expected_changes": {"type": "string"},
        "success_criteria": {"type": "string"},
        "planned_tests": {"type": "string"},
        "risks": {"type": "string"},
        "estimated_scope": {"type": "string"},
    },
    "required": [
        "title",
        "motivation",
        "affected_files",
        "expected_changes",
        "success_criteria",
        "planned_tests",
        "risks",
        "estimated_scope",
    ],
    "additionalProperties": False,
}


@dataclass
class Experiment:
    id: str
    goal: str
    status: str
    created_at: str
    selection_started_at: str | None = None
    selection_ended_at: str | None = None
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
    plan: dict[str, Any] | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    research_task_id: str | None = None
    research_started_at: str | None = None
    research_ended_at: str | None = None
    research_runtime_ms: int | None = None
    research_usage: dict[str, Any] | None = None
    research_raw_summary: str | None = None
    research_diagnostic_log: str | None = None
    selection_task_id: str | None = None
    reformat_retry_task_id: str | None = None
    policy_replan_task_id: str | None = None
    implementation_task_id: str | None = None
    selection_runtime_ms: int | None = None
    reformat_retry_runtime_ms: int | None = None
    policy_replan_runtime_ms: int | None = None
    implementation_runtime_ms: int | None = None
    selection_usage: dict[str, Any] | None = None
    reformat_retry_usage: dict[str, Any] | None = None
    policy_replan_usage: dict[str, Any] | None = None
    implementation_usage: dict[str, Any] | None = None
    selection_raw_message: str | None = None
    reformat_raw_message: str | None = None
    policy_replan_raw_message: str | None = None
    selection_diagnostic_log: str | None = None
    reformat_diagnostic_log: str | None = None
    policy_replan_diagnostic_log: str | None = None
    test_command: list[str] | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    git_status: str | None = None
    diff_stat: str | None = None
    diff_text: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "selection_started_at": self.selection_started_at,
            "selection_ended_at": self.selection_ended_at,
            "implementation_started_at": self.implementation_started_at,
            "implementation_ended_at": self.implementation_ended_at,
            "ended_at": self.ended_at,
            "plan": self.plan,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "research_task_id": self.research_task_id,
            "research_started_at": self.research_started_at,
            "research_ended_at": self.research_ended_at,
            "research_runtime_ms": self.research_runtime_ms,
            "research_usage": self.research_usage,
            "research_raw_summary": self.research_raw_summary,
            "research_diagnostic_log": self.research_diagnostic_log,
            "selection_task_id": self.selection_task_id,
            "reformat_retry_task_id": self.reformat_retry_task_id,
            "policy_replan_task_id": self.policy_replan_task_id,
            "implementation_task_id": self.implementation_task_id,
            "selection_runtime_ms": self.selection_runtime_ms,
            "reformat_retry_runtime_ms": self.reformat_retry_runtime_ms,
            "policy_replan_runtime_ms": self.policy_replan_runtime_ms,
            "implementation_runtime_ms": self.implementation_runtime_ms,
            "selection_usage": self.selection_usage,
            "reformat_retry_usage": self.reformat_retry_usage,
            "policy_replan_usage": self.policy_replan_usage,
            "implementation_usage": self.implementation_usage,
            "selection_raw_message": self.selection_raw_message,
            "reformat_raw_message": self.reformat_raw_message,
            "policy_replan_raw_message": self.policy_replan_raw_message,
            "selection_diagnostic_log": self.selection_diagnostic_log,
            "reformat_diagnostic_log": self.reformat_diagnostic_log,
            "policy_replan_diagnostic_log": self.policy_replan_diagnostic_log,
            "test_command": self.test_command,
            "test_exit_code": self.test_exit_code,
            "test_output": self.test_output,
            "git_status": self.git_status,
            "diff_stat": self.diff_stat,
            "diff_text": self.diff_text,
            "error": self.error,
        }


def _row_to_experiment(row: sqlite3.Row) -> Experiment:
    def _json_or_none(value: str | None) -> Any:
        return json.loads(value) if value else None

    return Experiment(
        id=row["id"],
        goal=row["goal"],
        status=row["status"],
        created_at=row["created_at"],
        selection_started_at=row["selection_started_at"],
        selection_ended_at=row["selection_ended_at"],
        implementation_started_at=row["implementation_started_at"],
        implementation_ended_at=row["implementation_ended_at"],
        ended_at=row["ended_at"],
        plan=_json_or_none(row["plan_json"]),
        worktree_path=row["worktree_path"],
        branch_name=row["branch_name"],
        research_task_id=row["research_task_id"],
        research_started_at=row["research_started_at"],
        research_ended_at=row["research_ended_at"],
        research_runtime_ms=row["research_runtime_ms"],
        research_usage=_json_or_none(row["research_usage_json"]),
        research_raw_summary=row["research_raw_summary"],
        research_diagnostic_log=row["research_diagnostic_log"],
        selection_task_id=row["selection_task_id"],
        reformat_retry_task_id=row["reformat_retry_task_id"],
        policy_replan_task_id=row["policy_replan_task_id"],
        implementation_task_id=row["implementation_task_id"],
        selection_runtime_ms=row["selection_runtime_ms"],
        reformat_retry_runtime_ms=row["reformat_retry_runtime_ms"],
        policy_replan_runtime_ms=row["policy_replan_runtime_ms"],
        implementation_runtime_ms=row["implementation_runtime_ms"],
        selection_usage=_json_or_none(row["selection_usage_json"]),
        reformat_retry_usage=_json_or_none(row["reformat_retry_usage_json"]),
        policy_replan_usage=_json_or_none(row["policy_replan_usage_json"]),
        implementation_usage=_json_or_none(row["implementation_usage_json"]),
        selection_raw_message=row["selection_raw_message"],
        reformat_raw_message=row["reformat_raw_message"],
        policy_replan_raw_message=row["policy_replan_raw_message"],
        selection_diagnostic_log=row["selection_diagnostic_log"],
        reformat_diagnostic_log=row["reformat_diagnostic_log"],
        policy_replan_diagnostic_log=row["policy_replan_diagnostic_log"],
        test_command=_json_or_none(row["test_command_json"]),
        test_exit_code=row["test_exit_code"],
        test_output=row["test_output"],
        git_status=row["git_status"],
        diff_stat=row["diff_stat"],
        diff_text=row["diff_text"],
        error=row["error"],
    )


# ---------------------------------------------------------------------------
# Plan parsing helpers
# ---------------------------------------------------------------------------

_PLAN_FIELD_PATTERNS: dict[str, str] = {
    "title": r"title",
    "motivation": r"motivation",
    "affected_files": r"affected[_ ]files",
    "expected_changes": r"expected[_ ]changes",
    "success_criteria": r"success[_ ]criteria",
    "planned_tests": r"(planned[_ ]tests|tests)",
    "risks": r"risks",
    "estimated_scope": r"estimated[_ ]scope",
}


def _find_balanced_json_objects(text: str) -> list[str]:
    """Scan *text* and return the exact substrings of every top-level
    balanced ``{...}`` object, tracking string/escape state so that braces
    inside JSON string values (e.g. in a ``"risks"`` free-text field) never
    throw off the matching. Only top-level objects are returned (nested
    objects are part of their parent's span, not reported separately).
    """
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : i + 1])
                    start = None

    return objects


def extract_single_json_object(raw_text: str | None) -> dict[str, Any] | None:
    """Tolerantly extract exactly one JSON object from *raw_text*.

    Accepts, in order of preference:
      1. The whole trimmed text being pure JSON.
      2. Exactly one JSON object inside a ```json fenced code block.
      3. Exactly one top-level balanced ``{...}`` object anywhere in the
         text (braces inside JSON string values are correctly ignored via
         proper string/escape tracking, unlike a naive first/last-brace
         slice).

    Returns ``None`` — never guesses or merges — if zero or more than one
    top-level JSON object is found, or if the found object does not parse.
    No values are invented; this only locates and decodes exactly one
    object if unambiguously present.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    # 1) Whole text is pure JSON.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2) Exactly one fenced ```json ... ``` block containing an object.
    fenced_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if len(fenced_blocks) == 1:
        try:
            parsed = json.loads(fenced_blocks[0])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return None
    if len(fenced_blocks) > 1:
        return None  # ambiguous: more than one candidate, do not guess

    # 3) Exactly one top-level balanced {...} object anywhere in the text.
    candidates = _find_balanced_json_objects(text)
    if len(candidates) != 1:
        return None  # zero or ambiguous multiple objects: reject, don't guess

    try:
        parsed = json.loads(candidates[0])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_plan(raw_text: str | None) -> ExperimentPlan | None:
    """Best-effort parse of the selection phase's structured output.

    Prefers a single, unambiguous JSON object (see
    ``extract_single_json_object``). Falls back to a simple
    ``Field: value`` line format only when no JSON object is present at
    all. Returns ``None`` if the fields cannot be reliably extracted —
    callers must treat that as "reject, do not guess".
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()
    data = extract_single_json_object(text)

    if data is None:
        # Fallback: "Field: value" lines. Only used when the text contains
        # no JSON object candidates at all (extract_single_json_object
        # returns None both for "no object" and "ambiguous multiple
        # objects" — in the ambiguous case this fallback will also very
        # likely fail to find clean field lines, which is the desired
        # conservative behavior).
        data = {}
        for line in text.splitlines():
            match = re.match(r"^\s*[-*]?\s*([A-Za-z_ ]+):\s*(.+)$", line)
            if not match:
                continue
            key_raw, value = match.groups()
            for field, pattern in _PLAN_FIELD_PATTERNS.items():
                if re.fullmatch(pattern, key_raw.strip().lower().replace(" ", "_")):
                    data[field] = value.strip()
                    break

    if not data:
        return None

    def _get_str(key: str) -> str | None:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return None

    affected_files_raw = data.get("affected_files")
    if isinstance(affected_files_raw, list):
        affected_files = [str(v).strip() for v in affected_files_raw if str(v).strip()]
    elif isinstance(affected_files_raw, str) and affected_files_raw.strip():
        affected_files = [p.strip() for p in re.split(r"[,\n]", affected_files_raw) if p.strip()]
    else:
        affected_files = []

    required = {
        "title": _get_str("title"),
        "motivation": _get_str("motivation"),
        "expected_changes": _get_str("expected_changes"),
        "success_criteria": _get_str("success_criteria"),
        "planned_tests": _get_str("planned_tests"),
        "risks": _get_str("risks"),
        "estimated_scope": _get_str("estimated_scope"),
    }

    if not affected_files or any(v is None for v in required.values()):
        return None

    return ExperimentPlan(
        title=required["title"],
        motivation=required["motivation"],
        affected_files=affected_files,
        expected_changes=required["expected_changes"],
        success_criteria=required["success_criteria"],
        planned_tests=required["planned_tests"],
        risks=required["risks"],
        estimated_scope=required["estimated_scope"],
    )


def plan_violates_policy(plan: ExperimentPlan, raw_text: str) -> str | None:
    """Returns a human-readable rejection reason, or ``None`` if acceptable."""
    haystack = (raw_text + "\n" + json.dumps(plan.to_dict())).lower()
    for keyword in _REJECT_KEYWORDS:
        if keyword in haystack:
            return f"plan mentions disallowed keyword: {keyword!r}"

    for regex in _INFRA_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            if _infra_action_match_is_false_positive(match.group(0), preceding):
                continue
            return (
                "plan describes an actual infrastructure/deployment action "
                f"with external effect: {match.group(0)!r}"
            )

    for regex in _SECRET_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            if _infra_action_match_is_false_positive(match.group(0), preceding):
                continue
            return (
                "plan describes an actual secret/credential action: "
                f"{match.group(0)!r}"
            )

    # Safety net against placeholder/intention-only plans that technically
    # satisfy the JSON schema (non-empty affected_files, all fields
    # present) but describe only the *intent* to investigate rather than a
    # concrete, implementation-ready feature. This is an additional,
    # conservative check — it never loosens any existing rule.
    title_lower = plan.title.strip().lower()
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in title_lower:
            return f"plan title looks like a placeholder/intention-only statement: {plan.title!r}"

    # A plan whose affected_files are all non-code/non-test documents (e.g.
    # only IDEAS.md) describes an investigation or documentation note, not
    # an implementable feature. This does not forbid legitimate
    # documentation *features* in general — it only requires that at least
    # one affected file be a concrete code or test file; a plan may still
    # additionally touch README.md/IDEAS.md/etc. alongside a real code file.
    if not any(_is_code_or_test_file(path) for path in plan.affected_files):
        return (
            "plan does not touch any concrete code or test file; "
            "investigation-only or documentation-only changes "
            f"(affected_files={plan.affected_files!r}) are not an "
            "implementable feature"
        )

    if len(plan.affected_files) > 1:
        # Not an automatic hard rejection by count alone, but more than a
        # small handful strongly suggests more than "one small connected
        # feature" — conservative reject.
        if len(plan.affected_files) > 4:
            return (
                f"plan touches too many files ({len(plan.affected_files)}); "
                "must be one small, connected feature"
            )

    for path in plan.affected_files:
        if path.startswith("/") and not path.startswith("/agent"):
            return f"plan references a path outside the repository: {path!r}"
        if ".." in Path(path).parts:
            return f"plan references a path outside the repository: {path!r}"

    return None


# ---------------------------------------------------------------------------
# Research prompt template (read-only investigation, free-form output)
# ---------------------------------------------------------------------------

_RESEARCH_PROMPT_TEMPLATE = """You are researching this repository to find candidate improvements. This is
a READ-ONLY investigation phase. Do not modify any files.

Goal: {goal}

Steps:
1. Inspect IDEAS.md and the repository structure using read-only tools
   (read files, search, list directories) to understand what small,
   low-risk, testable improvements are plausible.
2. Consider one or more candidate features or fixes that:
   - can be fully implemented and tested within 20 minutes,
   - touch at most a small handful of closely related files,
   - need no new dependency, no package installation,
   - need no infrastructure, cloud, or deployment action,
   - need no secrets or credentials,
   - are not an authentication or security relaxation,
   - do not commit, push, or merge anything,
   - prefer existing small IDEAS.md entries, tests, observability, or
     internal developer-experience improvements.
3. Write a concise, factual research summary (plain text, NOT JSON) that
   states which candidate(s) you considered, the concrete evidence you
   found for each (e.g. exact IDEAS.md line, existing file/test names), and
   which single candidate you recommend as the best next feature and why.

Do NOT emit a final structured plan or any JSON object in this phase — a
separate phase will do that using your research summary as input. Your
entire response should be the research summary itself: prose, not JSON,
and no code fence.
"""


def _build_research_prompt(goal: str) -> str:
    return _with_nexus_identity(_RESEARCH_PROMPT_TEMPLATE.format(goal=goal))


# ---------------------------------------------------------------------------
# Plan prompt template (no tools, schema-constrained output only)
# ---------------------------------------------------------------------------

_PLAN_PROMPT_TEMPLATE = """You are selecting exactly ONE small, low-risk, testable improvement for this repository, based ONLY on the research summary below.

Goal: {goal}

You have NO tools available in this phase and cannot run any command or
investigate the repository further. Base your entire decision solely on
the research summary provided below; do not assume anything beyond it.

Research summary:
---
{research_summary}
---

The final answer must describe one concrete, implementation-ready feature
and "affected_files" must contain at least one existing or planned
repository file. Do not return progress, placeholder, or intention-only
content (for example: "inspection started", "analysis only",
"investigating", or any other statement that merely describes what you are
about to do rather than what you have already decided).

Select exactly one small feature or fix from the research summary that:
- can be fully implemented and tested within 20 minutes,
- touches at most a small handful of closely related files,
- needs no new dependency, no package installation,
- needs no infrastructure, Azure, or deployment change,
- needs no secrets or credentials,
- is not an authentication or security relaxation,
- does not commit, push, or merge anything.

Produce your final response now. Your ENTIRE response MUST be exactly one
JSON object and NOTHING else: no prose before or after it, no markdown
code fence, no explanation outside the JSON. The object must have exactly
these keys: "title", "motivation", "affected_files" (list of repo-relative
paths), "expected_changes", "success_criteria", "planned_tests", "risks",
"estimated_scope". All values must be strings, except "affected_files"
which must be a non-empty list of strings naming at least one existing or
planned repository file — never an empty list.

If no suitable small improvement exists in the research summary, respond
with a JSON object where "title" is exactly "NONE" and briefly explain why
in "motivation" (still exactly one JSON object and nothing else).
"""


def _build_plan_prompt(goal: str, research_summary: str) -> str:
    return _with_nexus_identity(
        _PLAN_PROMPT_TEMPLATE.format(goal=goal, research_summary=research_summary)
    )


_REFORMAT_RETRY_PROMPT_TEMPLATE = """Reformat the previous plan as exactly one JSON object matching this schema. Do not select or modify the feature. Output JSON only.

Schema keys (all string values, except "affected_files" which is a list of strings):
"title", "motivation", "affected_files", "expected_changes",
"success_criteria", "planned_tests", "risks", "estimated_scope".

Previous plan (reformat this content only, do not change its meaning or
select a different feature):
---
{previous_output}
---

Your entire response must be exactly one JSON object and nothing else: no
prose, no markdown code fence, no explanation outside the JSON.
"""


def _build_reformat_retry_prompt(previous_output: str) -> str:
    return _with_nexus_identity(
        _REFORMAT_RETRY_PROMPT_TEMPLATE.format(previous_output=previous_output)
    )


# ---------------------------------------------------------------------------
# Policy-replan prompt template (separate from the parser reformat retry):
# used exactly once when a syntactically valid, schema-conformant plan was
# rejected by plan_violates_policy(). No tools, no further research; the
# research summary is re-supplied as-is, plus the rejected plan and the
# exact rejection reason, and the model is instructed to select a
# different, concrete code/test/config change instead.
# ---------------------------------------------------------------------------

_POLICY_REPLAN_PROMPT_TEMPLATE = """Your previous plan was rejected by an automated policy check. You must now select a DIFFERENT small, low-risk, testable improvement, based ONLY on the research summary below.

Goal: {goal}

You have NO tools available in this phase and cannot run any command or
investigate the repository further. Base your entire decision solely on
the research summary provided below; do not assume anything beyond it.

Research summary:
---
{research_summary}
---

Your previous (rejected) plan:
---
{rejected_plan_json}
---

Rejection reason (must not recur in your new plan):
---
{rejection_reason}
---

Select a genuinely different feature or fix that concretely changes at
least one existing or planned code, test, or configuration file (not only
documentation), and that avoids the exact problem described in the
rejection reason above. It must still:
- be fully implementable and testable within 20 minutes,
- touch at most a small handful of closely related files,
- need no new dependency, no package installation,
- need no infrastructure, Azure, or deployment change,
- need no secrets or credentials,
- not be an authentication or security relaxation,
- not commit, push, or merge anything.

Do not return progress, placeholder, or intention-only content (for
example: "inspection started", "analysis only", "investigating", or any
other statement that merely describes what you are about to do rather
than what you have already decided).

Produce your final response now. Your ENTIRE response MUST be exactly one
JSON object and NOTHING else: no prose before or after it, no markdown
code fence, no explanation outside the JSON. The object must have exactly
these keys: "title", "motivation", "affected_files" (list of repo-relative
paths), "expected_changes", "success_criteria", "planned_tests", "risks",
"estimated_scope". All values must be strings, except "affected_files"
which must be a non-empty list of strings naming at least one existing or
planned repository file — never an empty list.

If no suitable alternative improvement exists, respond with a JSON object
where "title" is exactly "NONE" and briefly explain why in "motivation"
(still exactly one JSON object and nothing else).
"""


def _build_policy_replan_prompt(
    goal: str, research_summary: str, rejected_plan_json: str, rejection_reason: str
) -> str:
    return _with_nexus_identity(
        _POLICY_REPLAN_PROMPT_TEMPLATE.format(
            goal=goal,
            research_summary=research_summary,
            rejected_plan_json=rejected_plan_json,
            rejection_reason=rejection_reason,
        )
    )


_IMPLEMENTATION_PROMPT_TEMPLATE = """Implement exactly the following pre-approved, small feature plan. Do not
implement anything beyond this plan.

Title: {title}
Motivation: {motivation}
Affected files: {affected_files}
Expected changes: {expected_changes}
Success criteria: {success_criteria}
Planned tests: {planned_tests}

Allowed boundaries (must not be violated):
- Only modify files inside this working directory.
- Do not install any package or dependency.
- Do not change infrastructure, deployment, or Azure configuration.
- Do not read or output secrets or credentials.
- Do NOT run `git commit`, `git push`, or any merge.

Instructions:
1. Implement the change described above.
2. Add or update tests so the success criteria are verifiable.
3. Do not commit or push. Leave the working tree with your changes unstaged.
"""


def _build_implementation_prompt(plan: ExperimentPlan) -> str:
    return _with_nexus_identity(
        _IMPLEMENTATION_PROMPT_TEMPLATE.format(
            title=plan.title,
            motivation=plan.motivation,
            affected_files=", ".join(plan.affected_files),
            expected_changes=plan.expected_changes,
            success_criteria=plan.success_criteria,
            planned_tests=plan.planned_tests,
        )
    )


# ---------------------------------------------------------------------------
# Git helpers (fixed argv lists only, never shell=True)
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: str,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        env=env,
    )


class ExperimentService:
    """Orchestrates one bounded self-development experiment at a time.

    Reuses ``CodexWorker`` for both the read-only selection phase and the
    isolated workspace-write implementation phase; no worker/subprocess
    logic is duplicated here beyond git worktree management and the fixed
    verification commands.
    """

    def __init__(
        self,
        selection_worker: CodexWorker | None = None,
        worker_factory: Callable[[str], CodexWorker] | None = None,
        plan_worker_factory: Callable[[str], CodexWorker] | None = None,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        worktrees_root: str | None = None,
        max_test_output_bytes: int = _DEFAULT_MAX_TEST_OUTPUT_BYTES,
        max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
        max_raw_message_bytes: int = _DEFAULT_MAX_RAW_MESSAGE_BYTES,
        max_diagnostic_log_bytes: int = _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
        test_python_executable: str = _DEFAULT_TEST_PYTHON_EXECUTABLE,
    ) -> None:
        """
        Parameters
        ----------
        selection_worker:
            ``CodexWorker`` used for the read-only *research* phase (the
            only phase that ever investigates the live repository).
            Defaults to a real worker allowed only inside ``repo_root``.
        worker_factory:
            Callable ``(worktree_path) -> CodexWorker`` used to build the
            implementation-phase worker, scoped (``allowed_roots``) to that
            single worktree path only. Defaults to constructing a real
            ``CodexWorker(allowed_roots=(worktree_path,))``.
        plan_worker_factory:
            Callable ``(scratch_dir) -> CodexWorker`` used to build the
            *plan*-phase worker (and its one reformat retry), scoped
            (``allowed_roots``) to a fresh, empty scratch directory created
            fresh for each experiment and removed afterwards. This worker
            never sees ``repo_root`` and is given only the goal plus the
            research summary as input — it cannot perform any further
            repository research even if it tried to run a shell command,
            since its working directory is empty. Defaults to constructing
            a real ``CodexWorker(allowed_roots=(scratch_dir,))``.
        repo_root:
            Root of the main repository (default ``/agent``). Never
            modified by this service.
        worktrees_root:
            Parent directory under which per-experiment worktrees (and
            plan-phase scratch directories) are created. Defaults to
            ``~/agent-worktrees`` since ``/`` itself is typically not
            writable by the agent user.
        test_python_executable:
            Path to the Python interpreter used to run the verification
            test command inside an experiment worktree. Defaults to the
            main repository's own virtualenv interpreter
            (``/agent/.venv/bin/python``) so a fresh git worktree — which
            has no virtualenv of its own — can still run the existing test
            suite: the worktree is used only as the test command's
            *working directory*, never as the source of the interpreter
            or its installed packages. No package is installed and no
            ``.venv`` is copied into the worktree.
        """
        self.selection_worker = selection_worker if selection_worker is not None else CodexWorker(
            allowed_roots=(repo_root,)
        )
        self.worker_factory = worker_factory or (
            lambda worktree_path: CodexWorker(allowed_roots=(worktree_path,))
        )
        self.plan_worker_factory = plan_worker_factory or (
            lambda scratch_dir: CodexWorker(allowed_roots=(scratch_dir,))
        )
        self.db_path = db_path
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root or str(Path.home() / "agent-worktrees")
        self.max_test_output_bytes = max_test_output_bytes
        self.max_diff_bytes = max_diff_bytes
        self.max_raw_message_bytes = max_raw_message_bytes
        self.max_diagnostic_log_bytes = max_diagnostic_log_bytes
        self.test_python_executable = test_python_executable

        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # create_experiment
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        goal: str,
        timeout_seconds: int = _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    ) -> Experiment:
        if not goal or not goal.strip():
            raise ValueError("goal must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        experiment_id = uuid.uuid4().hex
        created_at = _utcnow_iso()

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO experiments (id, goal, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (experiment_id, goal.strip(), STATUS_QUEUED, created_at),
            )
            insert_activity(
                self._conn,
                timestamp=created_at,
                entity_type="experiment",
                entity_id=experiment_id,
                event="created",
                to_status=STATUS_QUEUED,
                message=goal.strip(),
            )

        self._implementation_timeout_seconds = timeout_seconds
        return self.get_experiment(experiment_id)

    # ------------------------------------------------------------------
    # get_experiment
    # ------------------------------------------------------------------

    def get_experiment(self, experiment_id: str) -> Experiment:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise ExperimentNotFoundError(f"no experiment with id {experiment_id!r}")
        return _row_to_experiment(row)

    def list_experiments(self, limit: int = 20) -> list[Experiment]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_experiment(row) for row in rows]

    # ------------------------------------------------------------------
    # list_activity — persistent activity log (experiment lifecycle events)
    # ------------------------------------------------------------------

    def list_activity(
        self, limit: int = 50, experiment_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent persisted activity log entries, newest first.

        Reuses ``modules.run_store.list_activity`` against the same
        connection/lock used for all other experiment reads/writes.
        """
        with self._lock:
            return list_activity(
                self._conn,
                limit=limit,
                entity_type="experiment",
                entity_id=experiment_id,
            )

    def mailbox_task_id_for_experiment(self, experiment_id: str) -> str | None:
        """Return the approval id linked to an experiment, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM mailbox_task_approvals WHERE experiment_id = ? LIMIT 1",
                (experiment_id,),
            ).fetchone()
        return str(row[0]) if row else None

    # ------------------------------------------------------------------
    # Internal: status transition helper
    # ------------------------------------------------------------------

    def _transition(self, experiment_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        """Atomically move ``from_status`` -> ``to_status`` iff still ``from_status``.

        Returns True if the transition happened. Extra ``fields`` are set in
        the same UPDATE. Unknown/disallowed transitions are refused by the
        caller (checked via ``_ALLOWED_TRANSITIONS`` before calling this).
        """
        set_clauses = ["status = ?"]
        values: list[Any] = [to_status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([experiment_id, from_status])

        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} "
                "WHERE id = ? AND status = ?",
                values,
            )
            moved = cursor.rowcount == 1
            if moved:
                insert_activity(
                    self._conn,
                    timestamp=_utcnow_iso(),
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="status_changed",
                    from_status=from_status,
                    to_status=to_status,
                    message=str(fields.get("error")) if fields.get("error") else None,
                )
        return moved

    def _force_status(self, experiment_id: str, status: str, **fields: Any) -> None:
        """Set status unconditionally (used for terminal/failure bookkeeping)."""
        set_clauses = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(experiment_id)

        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            insert_activity(
                self._conn,
                timestamp=_utcnow_iso(),
                entity_type="experiment",
                entity_id=experiment_id,
                event="status_forced",
                to_status=status,
                message=str(fields.get("error")) if fields.get("error") else None,
            )

    # ------------------------------------------------------------------
    # run_experiment — the whole pipeline, single entry point
    # ------------------------------------------------------------------

    def run_experiment(self, experiment_id: str) -> Experiment:
        experiment = self.get_experiment(experiment_id)  # raises if missing

        if experiment.status != STATUS_QUEUED:
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{experiment.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        moved = self._transition(
            experiment_id, STATUS_QUEUED, STATUS_SELECTING,
            selection_started_at=_utcnow_iso(),
        )
        if not moved:
            # Raced with another start attempt; refuse rather than double-run.
            current = self.get_experiment(experiment_id)
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{current.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        try:
            return self._run_pipeline(experiment_id)
        except Exception as exc:  # noqa: BLE001 - must never leave an active status
            self._force_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                error=f"unhandled exception in experiment pipeline: {exc}",
            )
            return self.get_experiment(experiment_id)

    def _run_pipeline(self, experiment_id: str) -> Experiment:
        experiment = self.get_experiment(experiment_id)

        # ---- Phase 1: research (read-only, free-form, tools allowed) -----
        research_started_at = _utcnow_iso()
        research_prompt = _build_research_prompt(experiment.goal)
        research_task_id = uuid.uuid4().hex
        research_result: CodexResult = self.selection_worker.run(
            task=research_prompt,
            working_dir=self.repo_root,
            sandbox="read-only",
            timeout_seconds=_DEFAULT_RESEARCH_TIMEOUT_SECONDS,
        )

        research_fields = dict(
            research_started_at=research_started_at,
            research_ended_at=_utcnow_iso(),
            research_task_id=research_task_id,
            research_runtime_ms=research_result.runtime_ms,
            research_usage_json=json.dumps(research_result.usage) if research_result.usage else None,
            research_raw_summary=_truncate_text(
                _redact_secrets(research_result.final_message or ""),
                self.max_raw_message_bytes,
                label="research summary",
            ),
            research_diagnostic_log=_extract_diagnostic_log(
                research_result.stdout, self.max_diagnostic_log_bytes
            ),
        )

        if research_result.timed_out:
            self._force_status(
                experiment_id, STATUS_TIMED_OUT,
                ended_at=_utcnow_iso(),
                error="research phase timed out",
                **research_fields,
            )
            return self.get_experiment(experiment_id)

        if research_result.error or not research_result.success:
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                error=research_result.error or "research phase failed (non-zero exit)",
                **research_fields,
            )
            return self.get_experiment(experiment_id)

        research_summary = research_result.final_message or ""

        # ---- Phase 2: plan (no tools, schema-constrained, isolated) -------
        # The plan-phase worker is scoped to a fresh, empty scratch
        # directory (never repo_root): even if the model attempted to run
        # a shell command, there is no repository content there to
        # research further. The directory is removed again once the plan
        # phase (including its one reformat retry) has finished.
        Path(self.worktrees_root).mkdir(parents=True, exist_ok=True)
        scratch_dir = tempfile.mkdtemp(prefix=f"plan-{experiment_id}-", dir=self.worktrees_root)
        try:
            plan_worker = self.plan_worker_factory(scratch_dir)

            plan_prompt = _build_plan_prompt(experiment.goal, research_summary)
            selection_task_id = uuid.uuid4().hex
            selection_result: CodexResult = plan_worker.run(
                task=plan_prompt,
                working_dir=scratch_dir,
                sandbox="read-only",
                timeout_seconds=_DEFAULT_PLAN_TIMEOUT_SECONDS,
                output_schema=PLAN_JSON_SCHEMA,
                skip_git_repo_check=True,
            )

            if selection_result.timed_out:
                self._force_status(
                    experiment_id, STATUS_TIMED_OUT,
                    ended_at=_utcnow_iso(),
                    selection_ended_at=_utcnow_iso(),
                    selection_task_id=selection_task_id,
                    selection_runtime_ms=selection_result.runtime_ms,
                    selection_usage_json=json.dumps(selection_result.usage) if selection_result.usage else None,
                    selection_raw_message=_truncate_text(
                        selection_result.final_message or "", self.max_raw_message_bytes, label="raw message"
                    ),
                    selection_diagnostic_log=_extract_diagnostic_log(
                        selection_result.stdout, self.max_diagnostic_log_bytes
                    ),
                    error="plan phase timed out",
                    **research_fields,
                )
                return self.get_experiment(experiment_id)

            if selection_result.error or not selection_result.success:
                self._force_status(
                    experiment_id, STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    selection_ended_at=_utcnow_iso(),
                    selection_task_id=selection_task_id,
                    selection_runtime_ms=selection_result.runtime_ms,
                    selection_usage_json=json.dumps(selection_result.usage) if selection_result.usage else None,
                    selection_raw_message=_truncate_text(
                        selection_result.final_message or "", self.max_raw_message_bytes, label="raw message"
                    ),
                    selection_diagnostic_log=_extract_diagnostic_log(
                        selection_result.stdout, self.max_diagnostic_log_bytes
                    ),
                    error=selection_result.error or "plan phase failed (non-zero exit)",
                    **research_fields,
                )
                return self.get_experiment(experiment_id)

            plan = parse_plan(selection_result.final_message)
            reformat_task_id: str | None = None
            reformat_runtime_ms: int | None = None
            reformat_usage: dict[str, Any] | None = None
            reformat_raw_message: str | None = None
            reformat_diagnostic_log: str | None = None

            # Exactly one read-only formatting retry: only triggered when no
            # JSON object could be extracted at all (a genuine formatting
            # problem), never for a policy violation or "NONE" plan, and
            # never more than once. The retry uses the same tool-less,
            # isolated plan_worker/scratch_dir — it never regains repo
            # access either.
            if plan is None:
                reformat_task_id = uuid.uuid4().hex
                reformat_prompt = _build_reformat_retry_prompt(selection_result.final_message or "")
                reformat_result: CodexResult = plan_worker.run(
                    task=reformat_prompt,
                    working_dir=scratch_dir,
                    sandbox="read-only",
                    timeout_seconds=_REFORMAT_RETRY_TIMEOUT_SECONDS,
                    output_schema=PLAN_JSON_SCHEMA,
                    skip_git_repo_check=True,
                )
                reformat_runtime_ms = reformat_result.runtime_ms
                reformat_usage = reformat_result.usage
                reformat_raw_message = _truncate_text(
                    reformat_result.final_message or "", self.max_raw_message_bytes, label="raw message"
                )
                reformat_diagnostic_log = _extract_diagnostic_log(
                    reformat_result.stdout, self.max_diagnostic_log_bytes
                )

                if not reformat_result.timed_out and not reformat_result.error and reformat_result.success:
                    plan = parse_plan(reformat_result.final_message)
                    # Guard against the retry silently picking a new feature:
                    # if a plan was recovered, the title must not differ from
                    # what a naive re-parse of the *original* text would
                    # otherwise suggest is a wholesale new selection. We cannot
                    # know the original intended title if it was unparseable,
                    # so we only guard the one case we CAN check: NONE markers
                    # and policy are still evaluated exactly as for the first
                    # attempt below (a retry that fabricates a new, non-"NONE"
                    # feature will still be subject to the same policy checks;
                    # it cannot bypass validation).

            # ---- Policy check (+ exactly one policy replan) ---------------
            # A syntactically valid, schema-conformant plan may still be
            # rejected by plan_violates_policy(). Distinct from the parser
            # reformat retry above (which only ever fires on unparseable
            # output): here the plan parsed fine but its *content* violates
            # policy, so we give the plan phase exactly one more chance to
            # pick a different, policy-compliant feature — never a second
            # attempt at reformatting the same rejected content, and never
            # triggered by a parse failure or a "NONE" plan.
            policy_replan_task_id: str | None = None
            policy_replan_runtime_ms: int | None = None
            policy_replan_usage: dict[str, Any] | None = None
            policy_replan_raw_message: str | None = None
            policy_replan_diagnostic_log: str | None = None

            rejection_reason: str | None = None
            if plan is None:
                rejection_reason = "plan output could not be reliably parsed into a structured plan"
            elif plan.title.strip().upper() == "NONE":
                rejection_reason = f"agent found no suitable improvement: {plan.motivation}"
            else:
                rejection_reason = plan_violates_policy(plan, selection_result.final_message or "")

            if rejection_reason is not None and plan is not None and plan.title.strip().upper() != "NONE":
                # This is specifically a policy violation on an otherwise
                # valid plan (never a parse failure, never a "NONE" plan).
                policy_replan_task_id = uuid.uuid4().hex
                policy_replan_prompt = _build_policy_replan_prompt(
                    goal=experiment.goal,
                    research_summary=research_summary,
                    rejected_plan_json=json.dumps(plan.to_dict()),
                    rejection_reason=rejection_reason,
                )
                policy_replan_result: CodexResult = plan_worker.run(
                    task=policy_replan_prompt,
                    working_dir=scratch_dir,
                    sandbox="read-only",
                    timeout_seconds=_DEFAULT_PLAN_TIMEOUT_SECONDS,
                    output_schema=PLAN_JSON_SCHEMA,
                    skip_git_repo_check=True,
                )
                policy_replan_runtime_ms = policy_replan_result.runtime_ms
                policy_replan_usage = policy_replan_result.usage
                policy_replan_raw_message = _truncate_text(
                    _redact_secrets(policy_replan_result.final_message or ""),
                    self.max_raw_message_bytes,
                    label="raw message",
                )
                policy_replan_diagnostic_log = _extract_diagnostic_log(
                    policy_replan_result.stdout, self.max_diagnostic_log_bytes
                )

                if (
                    not policy_replan_result.timed_out
                    and not policy_replan_result.error
                    and policy_replan_result.success
                ):
                    new_plan = parse_plan(policy_replan_result.final_message)
                    if new_plan is None:
                        rejection_reason = (
                            "policy replan output could not be reliably parsed into a "
                            "structured plan"
                        )
                    elif new_plan.title.strip().upper() == "NONE":
                        rejection_reason = (
                            f"agent found no suitable alternative improvement: {new_plan.motivation}"
                        )
                    else:
                        new_rejection_reason = plan_violates_policy(
                            new_plan, policy_replan_result.final_message or ""
                        )
                        plan = new_plan
                        rejection_reason = new_rejection_reason
                else:
                    rejection_reason = (
                        policy_replan_result.error or "policy replan failed (non-zero exit)"
                    )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

        common_selection_fields = dict(
            **research_fields,
            selection_ended_at=_utcnow_iso(),
            selection_task_id=selection_task_id,
            selection_runtime_ms=selection_result.runtime_ms,
            selection_usage_json=json.dumps(selection_result.usage) if selection_result.usage else None,
            reformat_retry_task_id=reformat_task_id,
            reformat_retry_runtime_ms=reformat_runtime_ms,
            reformat_retry_usage_json=json.dumps(reformat_usage) if reformat_usage else None,
            policy_replan_task_id=policy_replan_task_id,
            policy_replan_runtime_ms=policy_replan_runtime_ms,
            policy_replan_usage_json=json.dumps(policy_replan_usage) if policy_replan_usage else None,
            selection_raw_message=_truncate_text(
                selection_result.final_message or "", self.max_raw_message_bytes, label="raw message"
            ),
            reformat_raw_message=reformat_raw_message,
            policy_replan_raw_message=policy_replan_raw_message,
            selection_diagnostic_log=_extract_diagnostic_log(
                selection_result.stdout, self.max_diagnostic_log_bytes
            ),
            reformat_diagnostic_log=reformat_diagnostic_log,
            policy_replan_diagnostic_log=policy_replan_diagnostic_log,
            plan_json=json.dumps(plan.to_dict()) if plan is not None else None,
        )

        if rejection_reason is not None:
            self._transition(
                experiment_id, STATUS_SELECTING, STATUS_REJECTED,
                ended_at=_utcnow_iso(),
                error=rejection_reason,
                **common_selection_fields,
            )
            return self.get_experiment(experiment_id)

        assert plan is not None  # for type-checkers; rejection_reason is None here

        moved = self._transition(
            experiment_id, STATUS_SELECTING, STATUS_IMPLEMENTING,
            implementation_started_at=_utcnow_iso(),
            **common_selection_fields,
        )
        if not moved:
            return self.get_experiment(experiment_id)

        # ---- Phase 3: isolated worktree -----------------------------------
        branch_name = f"experiment/{experiment_id}"
        worktree_path = str(Path(self.worktrees_root) / experiment_id)

        try:
            Path(self.worktrees_root).mkdir(parents=True, exist_ok=True)
            create_proc = _run_git(
                ["worktree", "add", "-b", branch_name, worktree_path, "HEAD"],
                cwd=self.repo_root,
                timeout=60,
            )
            if create_proc.returncode != 0:
                self._force_status(
                    experiment_id, STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    implementation_ended_at=_utcnow_iso(),
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    error=f"git worktree add failed: {create_proc.stderr.strip()[:2000]}",
                )
                return self.get_experiment(experiment_id)
        except (OSError, subprocess.SubprocessError) as exc:
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                error=f"failed to create worktree: {exc}",
            )
            return self.get_experiment(experiment_id)

        self._force_status(
            experiment_id, STATUS_IMPLEMENTING,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        # ---- Phase 3: implementation (workspace-write, worktree only) -----
        implementation_prompt = _build_implementation_prompt(plan)
        implementation_task_id = uuid.uuid4().hex
        worker = self.worker_factory(worktree_path)

        implementation_timeout = getattr(
            self, "_implementation_timeout_seconds", _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS
        )

        implementation_result: CodexResult = worker.run(
            task=implementation_prompt,
            working_dir=worktree_path,
            sandbox="workspace-write",
            timeout_seconds=implementation_timeout,
        )

        if implementation_result.timed_out:
            self._force_status(
                experiment_id, STATUS_TIMED_OUT,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=json.dumps(implementation_result.usage)
                if implementation_result.usage else None,
                error="implementation phase timed out",
            )
            return self.get_experiment(experiment_id)

        if implementation_result.error:
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=json.dumps(implementation_result.usage)
                if implementation_result.usage else None,
                error=implementation_result.error,
            )
            return self.get_experiment(experiment_id)

        self._transition(
            experiment_id, STATUS_IMPLEMENTING, STATUS_VERIFYING,
            implementation_ended_at=_utcnow_iso(),
            implementation_task_id=implementation_task_id,
            implementation_runtime_ms=implementation_result.runtime_ms,
            implementation_usage_json=json.dumps(implementation_result.usage)
            if implementation_result.usage else None,
        )

        # ---- Phase 4: verification -----------------------------------------
        return self._verify(experiment_id, worktree_path)

    def _verify(self, experiment_id: str, worktree_path: str) -> Experiment:
        try:
            status_proc = _run_git(["status", "--short"], cwd=worktree_path)
            with tempfile.TemporaryDirectory(prefix="experiment-git-index-") as index_dir:
                index_env = dict(os.environ)
                index_env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
                read_tree_proc = _run_git(
                    ["read-tree", "HEAD"], cwd=worktree_path, env=index_env
                )
                add_proc = _run_git(["add", "-A"], cwd=worktree_path, env=index_env)
                if read_tree_proc.returncode != 0 or add_proc.returncode != 0:
                    failed_proc = read_tree_proc if read_tree_proc.returncode != 0 else add_proc
                    raise subprocess.CalledProcessError(
                        failed_proc.returncode,
                        failed_proc.args,
                        output=failed_proc.stdout,
                        stderr=failed_proc.stderr,
                    )
                stat_proc = _run_git(
                    ["diff", "--cached", "--stat", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                )
                diff_proc = _run_git(
                    ["diff", "--cached", "HEAD"], cwd=worktree_path, env=index_env
                )
                check_proc = _run_git(
                    ["diff", "--cached", "--check", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                error=f"verification git commands failed: {exc}",
            )
            return self.get_experiment(experiment_id)

        git_status = status_proc.stdout
        diff_stat = stat_proc.stdout
        diff_text = _truncate_text(diff_proc.stdout, self.max_diff_bytes, label="diff")
        diff_check_issue = check_proc.stdout.strip() or (check_proc.returncode != 0 and check_proc.stderr.strip())

        test_command_key = _DEFAULT_TEST_COMMAND_KEY
        test_command_template = _ALLOWED_TEST_COMMANDS.get(test_command_key)
        test_command = (
            [part.format(python=self.test_python_executable) for part in test_command_template]
            if test_command_template is not None
            else None
        )

        if test_command is None or not (Path(worktree_path) / "tests").is_dir():
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                git_status=git_status,
                diff_stat=diff_stat,
                diff_text=diff_text,
                error="no safe allowlisted test command could be determined for this worktree",
            )
            return self.get_experiment(experiment_id)

        try:
            test_proc = subprocess.run(
                test_command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=300,
                shell=False,
            )
            test_exit_code = test_proc.returncode
            test_output = (test_proc.stdout or "") + (test_proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            test_exit_code = None
            test_output = (
                (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            ) + "\n[test command timed out]"
        except (OSError, subprocess.SubprocessError) as exc:
            test_exit_code = None
            test_output = f"failed to run test command: {exc}"

        test_output_capped = _truncate_text(test_output, self.max_test_output_bytes, label="test output")

        final_status = STATUS_SUCCEEDED
        error: str | None = None
        if diff_check_issue:
            final_status = STATUS_NEEDS_REVIEW
            error = f"git diff --check reported issues: {str(diff_check_issue)[:500]}"
        elif test_exit_code != 0:
            final_status = STATUS_FAILED if test_exit_code is not None else STATUS_NEEDS_REVIEW
            error = f"tests failed (exit code {test_exit_code})"

        self._force_status(
            experiment_id, final_status,
            ended_at=_utcnow_iso(),
            git_status=git_status,
            diff_stat=diff_stat,
            diff_text=diff_text,
            test_command_json=json.dumps(test_command),
            test_exit_code=test_exit_code,
            test_output=test_output_capped,
            error=error,
        )
        return self.get_experiment(experiment_id)

    # ------------------------------------------------------------------
    # Cleanup — explicit only, never automatic
    # ------------------------------------------------------------------

    def cleanup_worktree(self, experiment_id: str) -> None:
        """Explicitly remove an experiment's git worktree and branch.

        Never called automatically by ``run_experiment``; the worktree is
        kept after an experiment finishes so a human can inspect it. Safe to
        call multiple times.
        """
        experiment = self.get_experiment(experiment_id)
        if experiment.worktree_path:
            _run_git(
                ["worktree", "remove", "--force", experiment.worktree_path],
                cwd=self.repo_root,
            )
            shutil.rmtree(experiment.worktree_path, ignore_errors=True)
        if experiment.branch_name:
            _run_git(["branch", "-D", experiment.branch_name], cwd=self.repo_root)
