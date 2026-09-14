"""Reusable LLM-powered failure-analysis service for Nexus (#145).

Anywhere in Nexus something fails, stalls, retries repeatedly, or lands in
``needs_review``, a caller can hand this service a normalized failure +
evidence bundle and get back a structured diagnosis: the current blocker,
likely root causes (each tied to the evidence that supports it), missing
information, recommended next checks/actions, and whether the problem looks
self-recoverable or should escalate to a human/external model.

Design points (see the issue for the full spec):

* **Never breaks the lifecycle it describes.** :meth:`FailureAnalysisService.analyze`
  never raises; any internal failure degrades to a persisted
  ``analysis_unavailable`` record instead (see ``_UNAVAILABLE_CATEGORY``).
* **Bounded, relevance-filtered context.** Callers never hand the model raw
  full logs; :func:`evidence_from_log_entries` picks a small, relevant slice
  and every field is length-capped.
* **One local-model execution path.** Local inference is submitted to the
  shared :class:`~modules.local_llm_jobs.LocalLlmJobService` under the
  ``failure_analysis`` role (#167), never as a second direct HTTP request to
  llama-server: queueing, serialization, liveness, cancellation, stuck-job
  handling and job observability all belong to that one component. The
  provider is not hard-coded into the analysis shape, so a future
  external-model fallback can be added without changing callers.
* **No analysis-side deadline.** A valid diagnosis on the deployed local
  model may legitimately need minutes; the shared job service's liveness
  handling decides when inference is genuinely stuck, not a fixed
  failure-analysis timeout.
* **No secrets in the prompt or the stored result.** Every piece of text
  that reaches the model or the database goes through
  ``modules.redaction.redact`` first.
* **Diagnosis may check, not only read (#172).** When the service is given
  the shared :class:`~modules.nexus_tool_router.NexusToolRouter`, a bounded
  evidence-gathering phase runs before the diagnosis itself: the same
  ``find_tool``/``call_tool`` mechanism the persistent controller uses, on
  the same read-only ``OperationsRegistry`` catalog, with the same schema
  validation, redaction and audit logging. This exists because a persisted
  failure reason is a claim about the past: "GITHUB_TOKEN is unavailable"
  can be false by the time it is diagnosed, and only current evidence can
  say so. The phase is read-only by construction, strictly bounded, and
  entirely optional - without a router the service behaves exactly as it
  did before.
* **Reusable, not finalization-only.** :meth:`analyze` takes a generic
  ``subject_type``/``subject_id`` plus a caller-supplied evidence bundle;
  :meth:`analyze_from_logs` is a convenience helper for the common case of
  diagnosing from an experiment's/mailbox task's structured JSONL logs, but
  callers that have their own evidence are not forced to know about
  ``central_logging`` at all.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import central_logging
from modules.llama_cpp_client import LlamaCppClientError, extract_json
from modules.local_llm_jobs import (
    ROLE_FAILURE_ANALYSIS,
    LocalLlmJobError,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    TOOL_FIND_TOOL,
    InMemoryToolGrantLedger,
    NexusToolRouter,
    ToolResolverUnavailableError,
    ToolRouterError,
)
from modules.outbox_publisher import PUBLISH_PENDING_WRITE, PUBLISH_WRITTEN, RepoLock
from modules.redaction import redact
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.tool_loop_guard import PersistentToolLoopGuard

logger = logging.getLogger(__name__)

CATEGORY_UNAVAILABLE = "analysis_unavailable"

# Bounds on the diagnostic context handed to the model. Generous enough to
# be diagnostically useful, small enough that a single analysis call cannot
# balloon into "dump the whole log file" (the issue explicitly calls this
# out: "Do not dump the complete log/history/context into the model").
_MAX_EVIDENCE_ITEMS = 40
_MAX_EVIDENCE_EXTRA_FIELDS = 8
_MAX_EVIDENCE_FIELD_CHARS = 300
_MAX_FAILURE_TEXT_CHARS = 1500
_MAX_GOAL_CHARS = 500

# Bounds on what is accepted back from the model / persisted.
_MAX_SUMMARY_CHARS = 1000
_MAX_TEXT_ITEM_CHARS = 300
_MAX_LIST_ITEMS = 8
_MAX_ROOT_CAUSES = 5
_MAX_EVIDENCE_REFS_PER_CAUSE = 6

# Below this confidence the analysis is treated as too uncertain to trust
# on its own, regardless of what the model itself claimed.
_LOW_CONFIDENCE_ESCALATION_THRESHOLD = 0.35

_DEFAULT_MAX_TOKENS = 900

# Bounds on the optional evidence-gathering phase (#172). Diagnosis is
# allowed to check a claim, not to go exploring: a couple of lookups, a
# couple of calls, small results.
_DEFAULT_MAX_TOOL_STEPS = 4
_MAX_TOOL_DECISION_TOKENS = 300
_MAX_TOOL_OUTPUT_LINES = 12
_MAX_TOOL_OUTPUT_LINE_CHARS = 200
_MAX_TOOL_REASON_CHARS = 200
_MAX_TOOL_GOAL_CHARS = 200

# Re-entrancy guard (#167). Diagnosing a failure must never become the next
# failure to diagnose: if anything on the analysis path ever asked for an
# analysis again, this stops the recursion at the first nested call instead
# of queueing a second local-model job and persisting a second record.
_ANALYSIS_IN_PROGRESS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "failure_analysis_in_progress", default=False
)

# Fields every structured log entry (see modules.central_logging) carries
# that are either redundant with the compact rendering below or too
# large/noisy to hand to the model (raw stdout/stderr, stack traces, the
# internal pagination keys).
_EVIDENCE_EXCLUDED_KEYS = frozenset({
    "logger", "message", "_source", "_rseq", "truncated",
    "stacktrace", "stdout", "stderr", "command", "cwd",
    "experiment_id", "mailbox_task_id", "job_id", "session_id",
})

# A log entry is "relevant" for diagnosis when it is a warning/error, or its
# event name looks like a decision/evidence/failure point. Everything else
# (routine INFO progress notes) is dropped before anything reaches the
# model, unless nothing at all matches - see evidence_from_log_entries.
_RELEVANT_EVENT_KEYWORDS = (
    "evidence", "assess", "block", "reject", "fail", "error",
    "review", "escalat", "loop", "timeout", "interrupt", "stale",
    "retry", "unavailable",
)


class FailureAnalysisError(RuntimeError):
    """Raised only for programmer errors (bad arguments), never for a model/IO failure."""


@dataclass(frozen=True, slots=True)
class RootCause:
    cause: str
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "confidence": self.confidence, "evidence": list(self.evidence)}


@dataclass(frozen=True, slots=True)
class RecommendedAction:
    type: str
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "target": self.target}


@dataclass(frozen=True, slots=True)
class FailureAnalysis:
    """A single structured diagnosis. Always machine-readable and persisted."""

    id: str
    subject_type: str
    subject_id: str
    created_at: str
    provider: str
    model: str | None
    summary: str
    category: str
    confidence: float
    likely_root_causes: tuple[RootCause, ...] = ()
    missing_information: tuple[str, ...] = ()
    recommended_checks: tuple[str, ...] = ()
    recommended_actions: tuple[RecommendedAction, ...] = ()
    can_self_recover: bool = False
    needs_external_model: bool = False
    needs_human: bool = False
    escalation_reason: str | None = None
    evidence_refs: tuple[str, ...] = ()
    experiment_id: str | None = None
    mailbox_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "created_at": self.created_at,
            "provider": self.provider,
            "model": self.model,
            "summary": self.summary,
            "category": self.category,
            "confidence": self.confidence,
            "likely_root_causes": [c.to_dict() for c in self.likely_root_causes],
            "missing_information": list(self.missing_information),
            "recommended_checks": list(self.recommended_checks),
            "recommended_actions": [a.to_dict() for a in self.recommended_actions],
            "can_self_recover": self.can_self_recover,
            "needs_external_model": self.needs_external_model,
            "needs_human": self.needs_human,
            "escalation_reason": self.escalation_reason,
            "evidence_refs": list(self.evidence_refs),
            "experiment_id": self.experiment_id,
            "mailbox_task_id": self.mailbox_task_id,
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bounded(text: Any, max_chars: int) -> str:
    return redact(str(text))[:max_chars]


def _bounded_list(values: Any, *, max_items: int = _MAX_LIST_ITEMS, max_chars: int = _MAX_TEXT_ITEM_CHARS) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = _bounded(value, max_chars)
        if text:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


# ---------------------------------------------------------------------------
# Evidence collection: bounded, relevance-filtered context from structured
# logs. Reusable independently of the analyzer itself - a caller that
# already has its own evidence never has to touch this.
# ---------------------------------------------------------------------------


def _entry_is_relevant(entry: dict[str, Any]) -> bool:
    level = str(entry.get("level") or "").upper()
    if level in ("WARNING", "ERROR"):
        return True
    event = str(entry.get("event") or "").lower()
    return any(keyword in event for keyword in _RELEVANT_EVENT_KEYWORDS)


def _compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A bounded, redacted, model-friendly rendering of one log entry."""
    compact: dict[str, Any] = {}
    for key in ("timestamp", "level", "phase", "event"):
        value = entry.get(key)
        if value is not None:
            compact[key] = _bounded(value, 100)
    extras = 0
    for key, value in entry.items():
        if extras >= _MAX_EVIDENCE_EXTRA_FIELDS:
            break
        if key in _EVIDENCE_EXCLUDED_KEYS or key in compact or value is None:
            continue
        if isinstance(value, bool):
            compact[key] = value
        elif isinstance(value, (int, float)):
            compact[key] = value
        else:
            compact[key] = _bounded(value, _MAX_EVIDENCE_FIELD_CHARS)
        extras += 1
    return compact


def evidence_from_log_entries(
    entries: list[dict[str, Any]], *, max_items: int = _MAX_EVIDENCE_ITEMS
) -> list[dict[str, Any]]:
    """Bounded, relevance-filtered, redacted evidence from raw log entries.

    Prefers warning/error and decision-shaped events (CI evidence, stale
    assessments, blocks/rejections, loops, escalations, ...) over routine
    INFO progress notes; falls back to the most recent entries verbatim
    when nothing matches, so a caller never gets an empty bundle just
    because a failure produced only INFO-level events. Chronological order
    is preserved (oldest of the kept entries first) and only the most
    recent *max_items* are kept.
    """
    relevant = [entry for entry in entries if _entry_is_relevant(entry)]
    source = relevant if relevant else list(entries)
    trimmed = source[-max_items:] if max_items > 0 else source
    return [_compact_entry(entry) for entry in trimmed]


def render_evidence_line(index: int, entry: dict[str, Any]) -> str:
    """One evidence bundle entry as a single ``[E<n>] event key=value ...`` line.

    Matches the compact event-line shape structured logging already writes
    (see the issue: ``ci_evidence_rejected rejection_reason=target_base_moved``),
    so the model sees evidence in the same vocabulary Nexus's own logs use.
    """
    event = str(entry.get("event") or "event")
    parts = [f"[E{index}]", event]
    for key, value in entry.items():
        if key == "event" or value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Nexus's failure-diagnosis assistant. You look at a failed, "
    "stalled, or repeatedly-retried task together with bounded structured "
    "evidence and produce a compact, honest, machine-readable diagnosis. "
    "You never invent evidence that was not supplied. You clearly "
    "distinguish observed symptoms from your hypotheses about their root "
    "cause, and you say so plainly when the evidence is insufficient to be "
    "confident rather than guessing with false certainty. "
    "A recorded failure reason is a claim about the moment it was written, "
    "not a fact about now: when current tool evidence contradicts it, the "
    "current evidence wins and the recorded reason is itself a finding - "
    "most often stale persisted state rather than the condition it names."
)

# The evidence-gathering role. Deliberately narrower than the diagnosis role:
# it decides what to look up, never what the failure means.
_TOOL_SYSTEM_PROMPT = (
    "You are Nexus's failure-diagnosis assistant in evidence-gathering mode. "
    "You do not diagnose yet. You decide whether a claim in the recorded "
    "failure can be checked against Nexus's current state, and if so you "
    "look it up using read-only Nexus capabilities. "
    "Runtime and configuration claims - a missing credential, an unreachable "
    "service, a stopped timer, an unresolved repository - are exactly the "
    "claims worth checking, because a persisted error string cannot tell you "
    "whether it is still true. "
    "You never change anything: no lifecycle transitions, no code edits, no "
    "merges, no restarts, no retries. Gathering evidence is all you do, and "
    "you stop as soon as you have it or no capability can supply it."
)

_TOOL_DECISION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [TOOL_FIND_TOOL, ACTION_CALL_TOOL, "done"]},
            "goal": {"type": "string"},
            "tool": {"type": "string"},
            "arguments": {"type": "object", "additionalProperties": True},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_RESPONSE_SCHEMA_HINT = """Respond with ONLY a single JSON object (no prose, no markdown fences) with this shape:
{
  "summary": "one or two sentences naming the current blocker",
  "category": "short machine-readable category, e.g. lifecycle_logic|transient|environment|missing_capability|test_verdict|unknown",
  "confidence": 0.0-1.0,
  "likely_root_causes": [
    {"cause": "...", "confidence": 0.0-1.0, "evidence": ["E2", "E4"]}
  ],
  "missing_information": ["what would make this more certain"],
  "recommended_checks": ["smallest next checks to run"],
  "recommended_actions": [{"type": "inspect_code|run_check|retry|escalate", "target": "..."}],
  "can_self_recover": true|false,
  "needs_external_model": true|false,
  "needs_human": true|false,
  "escalation_reason": "why, or null"
}
Cite evidence by its [E<n>] label (log evidence) or [T<n>] label (current
tool evidence) in "evidence" lists. If the evidence does not support a
confident diagnosis, say so via a low confidence value and a non-empty
"missing_information" list rather than fabricating certainty."""


def render_tool_line(index: int, observation: dict[str, Any]) -> str:
    """One gathered tool result as a single ``[T<n>] ...`` line.

    Deliberately the same shape as :func:`render_evidence_line`, so current
    evidence and logged evidence read as one vocabulary in the prompt and can
    be cited the same way.
    """
    parts = [f"[T{index}]", str(observation.get("action") or "tool")]
    for key in (
        "goal", "status", "tool", "operation", "success",
        "related_tools", "error", "reason", "output",
    ):
        value = observation.get(key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _build_tool_prompt(
    *,
    step: int,
    max_steps: int,
    subject_type: str,
    subject_id: str,
    goal: str | None,
    failure_text: str,
    evidence_lines: list[str],
    tool_lines: list[str],
) -> str:
    lines = [
        f"FAILURE ANALYSIS - EVIDENCE GATHERING STEP {step}/{max_steps}.",
        f"Subject: {subject_type} {subject_id}",
    ]
    if goal:
        lines.append(f"Goal Nexus was trying to achieve: {goal}")
    lines.append(f"Recorded failure/status:\n{failure_text}")
    lines.append("")
    if evidence_lines:
        lines.append("Logged evidence (most recent last):")
        lines.extend(evidence_lines)
    else:
        lines.append("No structured log evidence was supplied.")
    lines.append("")
    if tool_lines:
        lines.append("Current evidence gathered so far:")
        lines.extend(tool_lines)
    else:
        lines.append("No current evidence has been gathered yet.")
    lines.append("")
    lines.append("""Respond with ONLY one JSON object (no prose, no markdown fences):
{"action":"find_tool","goal":"<concrete read-only capability you need>","reason":"..."}
{"action":"call_tool","tool":"<operation name a find_tool result returned>","arguments":{},"reason":"..."}
{"action":"done","reason":"..."}

Rules:
- The recorded failure describes the past. If it asserts something about the
  runtime or the configuration, check whether it is still true before the
  diagnosis is written; a claim that current evidence contradicts points at
  stale persisted state, not at the condition it names.
- find_tool discovers a capability; its goal must name a concrete read-only
  capability, never a placeholder. Never repeat a goal that already returned
  not_found - nothing has changed since.
- A find_tool result whose arguments could be read straight out of your goal
  is executed for you; its result is the observation right after it. Never
  ask for that same call again - read the result instead.
- So name concrete values in the goal when the capability needs them ("the
  service_logs of llama-server"). A goal that names none, for a capability
  that takes some, comes back for you to call explicitly.
- call_tool may only name an operation a find_tool result in this analysis
  already returned, and only read-only operations exist here.
- Read-only means read-only: never transition lifecycle state, edit code,
  merge, restart, or retry anything.
- An observation with status=refused means that exact call was already made
  in this analysis. Repeating it returns nothing new: ask for different
  evidence or answer "done".
- Choose "done" as soon as you have the evidence you need, or once it is
  clear that no available capability can supply it.""")
    return "\n".join(lines)


def _build_prompt(
    *,
    subject_type: str,
    subject_id: str,
    goal: str | None,
    failure_text: str,
    evidence_lines: list[str],
    tool_lines: list[str] | None = None,
    tool_evidence_note: str | None = None,
) -> str:
    lines = [
        f"Subject: {subject_type} {subject_id}",
    ]
    if goal:
        lines.append(f"Goal Nexus was trying to achieve: {goal}")
    lines.append(f"Current failure/status:\n{failure_text}")
    lines.append("")
    if evidence_lines:
        lines.append("Evidence (most recent last):")
        lines.extend(evidence_lines)
    else:
        lines.append("No structured evidence was supplied.")
    lines.append("")
    if tool_lines:
        lines.append(
            "Current evidence, gathered just now with read-only Nexus tools. "
            "Where it contradicts the recorded failure above, it is the newer "
            "fact and the recorded failure is itself a symptom to explain:"
        )
        lines.extend(tool_lines)
        lines.append("")
    if tool_evidence_note:
        lines.append(tool_evidence_note)
        lines.append("")
    lines.append(
        "Task: identify the current blocker; distinguish symptoms from "
        "likely root causes; cite which evidence supports each hypothesis; "
        "identify missing information; propose the smallest useful next "
        "checks/actions; say whether this looks self-recoverable; avoid "
        "pretending certainty the evidence does not support."
    )
    lines.append("")
    lines.append(_RESPONSE_SCHEMA_HINT)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Result coercion: the model's JSON is untrusted input. Every field is
# defensively coerced/bounded; a malformed or missing field degrades to a
# sensible default rather than raising.
# ---------------------------------------------------------------------------


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _coerce_root_causes(raw: Any) -> tuple[RootCause, ...]:
    if not isinstance(raw, list):
        return ()
    causes: list[RootCause] = []
    for item in raw[:_MAX_ROOT_CAUSES]:
        if not isinstance(item, dict):
            continue
        cause_text = _bounded(item.get("cause", ""), _MAX_TEXT_ITEM_CHARS)
        if not cause_text:
            continue
        evidence = tuple(
            _bounded(ref, 20) for ref in (item.get("evidence") or [])[:_MAX_EVIDENCE_REFS_PER_CAUSE]
            if ref is not None
        )
        causes.append(
            RootCause(
                cause=cause_text,
                confidence=_coerce_confidence(item.get("confidence")),
                evidence=evidence,
            )
        )
    return tuple(causes)


def _coerce_actions(raw: Any) -> tuple[RecommendedAction, ...]:
    if not isinstance(raw, list):
        return ()
    actions: list[RecommendedAction] = []
    for item in raw[:_MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        action_type = _bounded(item.get("type", ""), 60)
        if not action_type:
            continue
        actions.append(RecommendedAction(type=action_type, target=_bounded(item.get("target", ""), _MAX_TEXT_ITEM_CHARS)))
    return tuple(actions)


def _coerce_analysis(
    data: dict[str, Any],
    *,
    analysis_id: str,
    subject_type: str,
    subject_id: str,
    provider: str,
    model: str | None,
    evidence_refs: list[str],
    experiment_id: str | None = None,
    mailbox_task_id: str | None = None,
) -> FailureAnalysis:
    confidence = _coerce_confidence(data.get("confidence"))
    needs_human = bool(data.get("needs_human", False))
    needs_external_model = bool(data.get("needs_external_model", False))
    escalation_reason = data.get("escalation_reason")
    escalation_reason = _bounded(escalation_reason, _MAX_TEXT_ITEM_CHARS) if escalation_reason else None

    if confidence < _LOW_CONFIDENCE_ESCALATION_THRESHOLD and not (needs_human or needs_external_model):
        needs_human = True
        escalation_reason = escalation_reason or (
            f"local model confidence {confidence:.2f} is below the "
            f"escalation threshold ({_LOW_CONFIDENCE_ESCALATION_THRESHOLD})"
        )

    return FailureAnalysis(
        id=analysis_id,
        subject_type=subject_type,
        subject_id=subject_id,
        created_at=_utcnow_iso(),
        provider=provider,
        model=model,
        summary=_bounded(data.get("summary") or "no summary provided", _MAX_SUMMARY_CHARS),
        category=_bounded(data.get("category") or "unknown", 80) or "unknown",
        confidence=confidence,
        likely_root_causes=_coerce_root_causes(data.get("likely_root_causes")),
        missing_information=tuple(_bounded_list(data.get("missing_information"))),
        recommended_checks=tuple(_bounded_list(data.get("recommended_checks"))),
        recommended_actions=_coerce_actions(data.get("recommended_actions")),
        can_self_recover=bool(data.get("can_self_recover", False)),
        needs_external_model=needs_external_model,
        needs_human=needs_human,
        escalation_reason=escalation_reason,
        evidence_refs=tuple(evidence_refs),
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
    )


def _unavailable_analysis(
    *,
    analysis_id: str,
    subject_type: str,
    subject_id: str,
    provider: str,
    model: str | None,
    reason: str,
    experiment_id: str | None = None,
    mailbox_task_id: str | None = None,
) -> FailureAnalysis:
    return FailureAnalysis(
        id=analysis_id,
        subject_type=subject_type,
        subject_id=subject_id,
        created_at=_utcnow_iso(),
        provider=provider,
        model=model,
        summary="Failure analysis was unavailable; the original failure is unaffected.",
        category=CATEGORY_UNAVAILABLE,
        confidence=0.0,
        needs_human=True,
        escalation_reason=_bounded(reason, _MAX_TEXT_ITEM_CHARS),
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
    )


def _yesno(value: bool) -> str:
    return "yes" if value else "no"


def render_outbox_content(analysis: FailureAnalysis) -> str:
    """Concise, user-visible mailbox outbox notification for one analysis (#148).

    Every field pulled in here already went through ``redact``/``_bounded``
    when the analysis was coerced from the model's output (or, for an
    unavailable analysis, when the escalation reason was captured) - so no
    additional redaction is needed at render time.
    """
    front_matter = (
        "---\n"
        "type: failure_analysis\n"
        f"analysis_id: {analysis.id}\n"
        f"subject_type: {analysis.subject_type}\n"
        f"subject_id: {analysis.subject_id}\n"
        f"experiment_id: {analysis.experiment_id or ''}\n"
        f"mailbox_task_id: {analysis.mailbox_task_id or ''}\n"
        f"category: {analysis.category}\n"
        f"confidence: {analysis.confidence:.2f}\n"
        f"created_at: {analysis.created_at}\n"
        "---\n"
    )

    if analysis.category == CATEGORY_UNAVAILABLE:
        body = (
            "# Failure Analysis Unavailable\n\n"
            "Nexus attempted to diagnose this failure automatically, but the "
            "diagnosis itself did not complete. The original failure/result "
            "is unaffected.\n\n"
            f"Reason: {analysis.escalation_reason or 'unknown'}\n\n"
            f"Analysis id: {analysis.id}\n"
            f"Experiment: {analysis.experiment_id or 'none'}\n"
            f"Task: {analysis.mailbox_task_id or 'none'}\n"
        )
        return front_matter + "\n" + body

    top_cause = analysis.likely_root_causes[0].cause if analysis.likely_root_causes else "unknown"
    next_checks = list(analysis.recommended_checks[:3])
    checks_block = "\n".join(f"- {check}" for check in next_checks) if next_checks else "- (none identified)"

    body = (
        "# Failure Analysis\n\n"
        f"Summary: {analysis.summary}\n\n"
        f"Category: {analysis.category}\n"
        f"Confidence: {analysis.confidence:.2f}\n"
        f"Likely cause: {top_cause}\n"
        f"Next checks:\n{checks_block}\n\n"
        f"Self recoverable: {_yesno(analysis.can_self_recover)}\n"
        f"External model recommended: {_yesno(analysis.needs_external_model)}\n"
        f"Human required: {_yesno(analysis.needs_human)}\n\n"
        f"Analysis id: {analysis.id}\n"
        f"Experiment: {analysis.experiment_id or 'none'}\n"
        f"Task: {analysis.mailbox_task_id or 'none'}\n"
    )
    return front_matter + "\n" + body


class FailureAnalysisService:
    """Reusable failure-analysis entry point, backed by the local Nexus LLM.

    Usable from anywhere a failure needs interpreting: task finalization
    landing in ``needs_review``, a failed/interrupted mailbox task, a
    repeated repair-loop, a tool-call failure, malformed LLM/tool output, a
    missing capability, or a manual/debug request to analyze a specific
    Nexus failure. Callers are never required to know about SQLite,
    ``central_logging``, or the LLM client - only :meth:`analyze` (generic
    evidence bundle) or :meth:`analyze_from_logs` (structured-log-backed
    convenience wrapper).
    """

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        llama_client: Any | None = None,
        job_service: LocalLlmJobService | None = None,
        analysis_timeout_seconds: float | None = None,
        log_dir: str | Path = central_logging.DEFAULT_LOG_DIR,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        mailbox_config: Any | None = None,
        outbox_dir: str | Path | None = None,
        repo_root: str | Path | None = None,
        tool_router: NexusToolRouter | None = None,
        operations_registry: Any | None = None,
        experiment_service: Any | None = None,
        max_tool_steps: int = _DEFAULT_MAX_TOOL_STEPS,
    ) -> None:
        """Build an analyzer bound to the shared local-LLM job service (#167).

        *job_service* injects an existing :class:`LocalLlmJobService`;
        *llama_client* injects the inference client a private job service
        should drive. With neither, the process-wide shared service for
        *db_path* is acquired, which is what puts failure-analysis jobs on the
        same queue and worker as controller jobs.

        *analysis_timeout_seconds* is accepted but ignored: a valid diagnosis
        on the deployed local model may legitimately take minutes, and the
        60s deadline this used to install is exactly what turned a healthy
        slow analysis into ``analysis_unavailable`` (#167). Liveness is the
        shared job service's job now.
        """
        self.db_path = db_path
        if analysis_timeout_seconds is not None:
            logger.info(
                "failure_analysis: analysis_timeout_seconds is ignored since #167; "
                "liveness is handled by the shared local LLM job service"
            )
        self._max_tokens = max(200, max_tokens)
        self._log_dir = Path(log_dir)
        self._max_tool_steps = max(0, min(int(max_tool_steps), 8))

        # Outbox publishing (#148) is optional: a caller that never supplies
        # mailbox_config (or outbox_dir + repo_root) still gets a fully
        # working analyzer - it just never emits an outbox notification for
        # its analyses. This keeps every existing construction site (tests,
        # the manual/API entry points) working unchanged.
        if mailbox_config is not None:
            self._outbox_dir: Path | None = Path(mailbox_config.outbox_dir)
            self._repo_root: Path | None = Path(mailbox_config.mailbox_repo_path)
        elif outbox_dir is not None and repo_root is not None:
            self._outbox_dir = Path(outbox_dir)
            self._repo_root = Path(repo_root)
        else:
            self._outbox_dir = None
            self._repo_root = None
        self._repo_lock = RepoLock(self._repo_root) if self._repo_root is not None else None

        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        # Schema ownership (#148): the failure_analyses table + its additive
        # outbox/correlation columns are created centrally by
        # modules.run_store.schema.init_db() (called just above), the same
        # place operation_chains/mailbox_task_approvals are created. That
        # guarantees the table exists before any caller touches it -
        # including FailureAnalysisOutboxPublisher, which never constructs
        # a FailureAnalysisService and would otherwise be able to run its
        # first query against a database where the table was never made.

        # Inference wiring last: an unusable database must fail construction
        # before a job worker is started for it.
        self._jobs = job_service or acquire_local_llm_job_service(
            db_path=db_path, llama_client=llama_client
        )
        self._owns_jobs = job_service is None
        # Kept for provider/model reporting only (the analysis record, logs and
        # observability all name the model). Production inference never calls
        # this client directly - it goes through self._jobs.
        self._client = getattr(self._jobs, "llama", None)

        # Optional tool access (#172). Supplied either as a ready router or as
        # the operations registry to build one from; without either, the
        # evidence-gathering phase is simply never entered and this service
        # behaves exactly as it did before. Building the router cannot be
        # allowed to fail construction: an analyzer that can still read
        # evidence is worth more than no analyzer at all.
        self._tool_router: NexusToolRouter | None = tool_router
        if self._tool_router is None and operations_registry is not None:
            try:
                self._tool_router = NexusToolRouter(
                    operations_registry=operations_registry,
                    experiment_service=experiment_service,
                    schema_completion=self._tool_schema_completion,
                    grant_ledger=InMemoryToolGrantLedger(),
                    caller="failure analysis",
                )
            except Exception:  # noqa: BLE001 - diagnosis must survive a missing tool path
                logger.warning(
                    "failure_analysis: could not build the shared tool router; "
                    "diagnosis will run without current-evidence gathering",
                    exc_info=True,
                )
                self._tool_router = None
        # The controller's duplicate/looping protections, reused rather than
        # re-invented: the analysis id is the session, so one analysis's
        # bounded budget can never be spent by another's.
        self._tool_guard: PersistentToolLoopGuard | None = None
        if self._tool_router is not None and self._max_tool_steps > 0:
            try:
                self._tool_guard = PersistentToolLoopGuard(
                    db_path=db_path,
                    max_total_steps=self._max_tool_steps,
                    # 2 would make the *first* repeat terminal, ending the
                    # whole gathering phase on one wasted call. A repeat costs
                    # that call; the phase ends when the model keeps repeating
                    # (#175).
                    max_identical_calls=3,
                    max_non_progressing_cycles=2,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "failure_analysis: could not open the tool loop guard; "
                    "diagnosis will run without current-evidence gathering",
                    exc_info=True,
                )
                self._tool_router = None

    def close(self) -> None:
        self._conn.close()
        if self._tool_guard is not None:
            self._tool_guard.close()
            self._tool_guard = None
        if self._owns_jobs:
            release_local_llm_job_service(self._jobs)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        subject_type: str,
        subject_id: str,
        failure: dict[str, Any] | str,
        evidence: list[dict[str, Any]] | None = None,
        goal: str | None = None,
        elog: Any = None,
        experiment_id: str | None = None,
        mailbox_task_id: str | None = None,
    ) -> FailureAnalysis:
        """Sync entry point for callers with no event loop of their own.

        Wraps :meth:`analyze_async` in ``asyncio.run`` - the same pattern
        :class:`~modules.task_orchestrator.NexusTaskOrchestrator` uses for
        its own sync ``run()``. Do **not** call this from inside a running
        event loop (``asyncio.run`` raises ``RuntimeError`` there); an
        async caller should await :meth:`analyze_async` directly instead,
        which is exactly what makes this service reusable from a future
        async orchestrator/tool-call path, not just today's sync mailbox
        workers and sync FastAPI endpoints.
        """
        return asyncio.run(
            self.analyze_async(
                subject_type=subject_type,
                subject_id=subject_id,
                failure=failure,
                evidence=evidence,
                goal=goal,
                elog=elog,
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )
        )

    async def analyze_async(
        self,
        *,
        subject_type: str,
        subject_id: str,
        failure: dict[str, Any] | str,
        evidence: list[dict[str, Any]] | None = None,
        goal: str | None = None,
        elog: Any = None,
        experiment_id: str | None = None,
        mailbox_task_id: str | None = None,
    ) -> FailureAnalysis:
        """Best-effort structured diagnosis of *failure*. Never raises.

        The core async entry point - awaitable directly from any async
        caller, so it works whether or not an event loop is already
        running. *evidence* should already be bounded/relevant (see
        :func:`evidence_from_log_entries`); this method bounds it again
        defensively regardless of what the caller passed.

        *experiment_id*/*mailbox_task_id* correlate the analysis with the
        mailbox task / experiment lifecycle that triggered it (#148, both
        optional - plenty of callers diagnose something with no mailbox
        context at all). When at least one is supplied, a concise outbox
        notification is published in addition to the full structured
        record persisted below.
        """
        analysis_id = uuid.uuid4().hex
        if _ANALYSIS_IN_PROGRESS.get():
            # Bounded and explicit (#167): one analysis per failure. A nested
            # request is refused before any job is queued and before anything
            # is persisted, so a failing analysis can never fan out into more
            # analyses or more durable records.
            logger.warning(
                "failure_analysis: refusing nested analysis for %s/%s",
                subject_type, subject_id,
            )
            return _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=getattr(self._client, "model", None),
                reason="nested failure analysis refused",
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )
        guard_token = _ANALYSIS_IN_PROGRESS.set(True)
        try:
            analysis = await self._analyze(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                failure=failure,
                evidence=evidence or [],
                goal=goal,
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )
        except Exception as exc:  # noqa: BLE001 - diagnosis must never break the caller
            logger.exception(
                "failure_analysis: analysis failed for %s/%s", subject_type, subject_id
            )
            analysis = _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=getattr(self._client, "model", None),
                reason=f"{type(exc).__name__}: {exc}",
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )
        finally:
            _ANALYSIS_IN_PROGRESS.reset(guard_token)
        persisted = self._persist(analysis)
        self._log(analysis, elog)
        # Only publish an outbox notification once the durable record it
        # points back to actually exists (#148 review): otherwise a DB
        # write failure could still leave an untracked outbox file with no
        # corresponding failure_analyses row to publish/track it from.
        if persisted:
            self._publish_outbox(analysis)
        return analysis

    def analyze_from_logs(
        self,
        *,
        subject_type: str,
        subject_id: str,
        experiment_id: str | None,
        mailbox_task_id: str | None = None,
        failure: dict[str, Any] | str,
        goal: str | None = None,
        max_items: int = _MAX_EVIDENCE_ITEMS,
        elog: Any = None,
    ) -> FailureAnalysis:
        """Sync wrapper around :meth:`analyze_from_logs_async` (see :meth:`analyze`
        for why an async caller must await the ``_async`` form instead)."""
        return asyncio.run(
            self.analyze_from_logs_async(
                subject_type=subject_type,
                subject_id=subject_id,
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
                failure=failure,
                goal=goal,
                max_items=max_items,
                elog=elog,
            )
        )

    async def analyze_from_logs_async(
        self,
        *,
        subject_type: str,
        subject_id: str,
        experiment_id: str | None,
        mailbox_task_id: str | None = None,
        failure: dict[str, Any] | str,
        goal: str | None = None,
        max_items: int = _MAX_EVIDENCE_ITEMS,
        elog: Any = None,
    ) -> FailureAnalysis:
        """Convenience wrapper: build the evidence bundle from structured logs.

        Merges the experiment's own JSONL log with its correlated
        pre-experiment mailbox task log (same merge the Logs API uses), so
        the bundle covers the whole task path - mailbox intake through
        adoption/finalization - not just whichever id the caller happens to
        have on hand. Reading the logs is itself best-effort: a missing or
        unreadable log directory yields an empty evidence bundle rather
        than failing the analysis.
        """
        entries: list[dict[str, Any]] = []
        try:
            if experiment_id:
                entries = central_logging.read_experiment_logs(
                    self._log_dir, experiment_id, mailbox_task_id=mailbox_task_id, limit=200
                )
            elif mailbox_task_id:
                path = self._log_dir / f"{central_logging.TASK_LOG_PREFIX}{mailbox_task_id}.jsonl"
                entries = central_logging.read_log_entries(path, limit=200)
        except Exception:  # noqa: BLE001 - evidence collection must never block diagnosis
            logger.warning(
                "failure_analysis: could not read logs for experiment=%s task=%s",
                experiment_id, mailbox_task_id, exc_info=True,
            )
        evidence = evidence_from_log_entries(entries, max_items=max_items)
        return await self.analyze_async(
            subject_type=subject_type,
            subject_id=subject_id,
            failure=failure,
            evidence=evidence,
            goal=goal,
            elog=elog,
            experiment_id=experiment_id,
            mailbox_task_id=mailbox_task_id,
        )

    # ------------------------------------------------------------------
    # Current-evidence gathering through the shared tool router (#172)
    # ------------------------------------------------------------------

    async def _tool_schema_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        role: str = "tool_resolver",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        """The router's inference path for this role: the shared job queue.

        Failure analysis has no memory budget plan and no external fallback of
        its own - it queues on the same local-LLM job service every other role
        uses (#167), under its own role label so the resolver calls a diagnosis
        made stay attributable.
        """
        del task_logger
        job_id = self._jobs.submit(
            prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            session_id=session_id,
            response_format=response_format,
            role=ROLE_FAILURE_ANALYSIS,
            system=_TOOL_SYSTEM_PROMPT,
        )
        logger.info(
            "failure_analysis: tool resolver job submitted job_id=%s role=%s "
            "analysis_id=%s resolver_role=%s",
            job_id, ROLE_FAILURE_ANALYSIS, session_id, role,
        )
        try:
            return await self._jobs.wait(job_id), "local"
        except LocalLlmJobError as exc:
            raise ToolResolverUnavailableError(str(exc)[:500], provider="local") from exc

    def _bounded_tool_observation(
        self, step: int, action: str, raw: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        """One tool result, bounded and redacted, ready for the model context.

        Nothing from a tool reaches a prompt, a log, the persisted analysis or
        the outbox except through here: every value is coerced to text,
        redacted and length-capped, and the raw registry payload is dropped
        rather than carried along.
        """
        observation: dict[str, Any] = {
            "step": step,
            "action": action,
            "reason": _bounded(reason, _MAX_TOOL_REASON_CHARS),
        }
        if action == TOOL_FIND_TOOL:
            observation["goal"] = _bounded(raw.get("goal", ""), _MAX_TOOL_GOAL_CHARS)
            observation["status"] = _bounded(raw.get("status", "unknown"), 40)
            resolved = raw.get("resolved_tool")
            if resolved:
                observation["tool"] = _bounded(resolved, 80)
            related = raw.get("related_tools")
            if isinstance(related, list) and related:
                observation["related_tools"] = [_bounded(item, 80) for item in related[:3]]
            if raw.get("status") != "found":
                observation["reason"] = _bounded(
                    raw.get("reason") or reason, _MAX_TOOL_REASON_CHARS
                )
            elif raw.get("resolved_arguments") is None:
                # Resolved, but its arguments could not be tied to the goal, so
                # it was not executed. Say why: without it the next step only
                # sees a lookup that led nowhere and no reason to call the tool
                # itself (#177 review).
                observation["reason"] = _bounded(
                    "not executed - "
                    + str(raw.get("arguments_binding_reason") or "arguments unresolved")
                    + "; call it yourself with explicit arguments",
                    _MAX_TOOL_REASON_CHARS,
                )
            return observation

        observation["tool"] = _bounded(raw.get("operation", ""), 80)
        if raw.get("error"):
            observation["status"] = "refused"
            observation["error"] = _bounded(raw.get("error"), 80)
            observation["reason"] = _bounded(
                raw.get("reason") or reason, _MAX_TOOL_REASON_CHARS
            )
            return observation
        result = raw.get("result") or {}
        success = bool(result.get("success"))
        observation["status"] = "ok" if success else "failed"
        observation["success"] = _yesno(success)
        output_lines = [
            _bounded(line, _MAX_TOOL_OUTPUT_LINE_CHARS)
            for line in list(result.get("output_lines") or [])[:_MAX_TOOL_OUTPUT_LINES]
        ]
        if output_lines:
            observation["output"] = " | ".join(output_lines)
        if not success and result.get("error_summary"):
            observation["error"] = _bounded(
                result.get("error_summary"), _MAX_TOOL_OUTPUT_LINE_CHARS
            )
        return observation

    def _tool_evidence_note(self, observations: list[dict[str, Any]]) -> str | None:
        """What the diagnosis should say about evidence it could not get.

        Silence here is what produced the failure mode in #172: an analysis
        that never checked anything read exactly like one that checked and
        found nothing. Naming the gap - no tool access at all, or no capability
        for what was asked - keeps "I could not verify this" a stated finding
        rather than an unstated assumption.
        """
        if self._tool_router is None or self._max_tool_steps <= 0:
            return (
                "No read-only diagnostic tools were available for this analysis, so "
                "nothing in the recorded failure could be verified against current "
                "state. Treat every runtime/configuration claim above as unverified, "
                "and list what would need checking under \"missing_information\"."
            )
        unmet = [
            str(item.get("goal") or "")
            for item in observations
            if item.get("action") == TOOL_FIND_TOOL
            and str(item.get("status")) in ("not_found", "invalid_goal", "unavailable")
        ]
        if not observations:
            return (
                "No current evidence was gathered for this analysis. Say so rather "
                "than treating the recorded failure as verified."
            )
        if unmet:
            missing = "; ".join(goal for goal in unmet if goal)[:400]
            return (
                "No Nexus capability could supply evidence for: "
                f"{missing or '(unnamed capability)'}. Report that evidence as "
                "missing under \"missing_information\" instead of assuming either "
                "answer."
            )
        return None

    async def _gather_tool_evidence(
        self,
        *,
        analysis_id: str,
        subject_type: str,
        subject_id: str,
        goal: str | None,
        failure_text: str,
        evidence_lines: list[str],
    ) -> list[dict[str, Any]]:
        """Bounded, read-only current evidence for this failure. Never raises.

        Returns the gathered observations in order. An empty list means the
        phase was skipped or produced nothing - which is a valid outcome, not
        an error: the diagnosis below then proceeds on logged evidence alone
        and says what it could not check.
        """
        router = self._tool_router
        guard = self._tool_guard
        if router is None or guard is None or self._max_tool_steps <= 0:
            return []
        observations: list[dict[str, Any]] = []
        try:
            await self._gather_tool_evidence_steps(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                goal=goal,
                failure_text=failure_text,
                evidence_lines=evidence_lines,
                observations=observations,
            )
        except Exception:  # noqa: BLE001 - gathering never breaks diagnosis
            logger.warning(
                "failure_analysis: evidence gathering failed analysis_id=%s; "
                "continuing with logged evidence only",
                analysis_id, exc_info=True,
            )
        if observations:
            logger.info(
                "failure_analysis: gathered current evidence analysis_id=%s steps=%s",
                analysis_id, len(observations),
            )
        return observations

    async def _gather_tool_evidence_steps(
        self,
        *,
        analysis_id: str,
        subject_type: str,
        subject_id: str,
        goal: str | None,
        failure_text: str,
        evidence_lines: list[str],
        observations: list[dict[str, Any]],
    ) -> None:
        """The bounded gathering loop itself, appending into *observations*.

        Split out so partial progress survives: whatever was already gathered
        when something unexpected goes wrong is still evidence, and the caller
        keeps it instead of discarding the whole phase.
        """
        router = self._tool_router
        guard = self._tool_guard
        assert router is not None and guard is not None
        # One state token per analysis: the failure being diagnosed does not
        # change while it is diagnosed, so every lookup in this run shares a
        # budget and a repeat really is a repeat.
        state_token = f"analysis:{analysis_id}"
        for step in range(1, self._max_tool_steps + 1):
            within_budget, _ = guard.claim_step(analysis_id, state_token)
            if not within_budget:
                logger.info(
                    "failure_analysis: tool evidence budget reached analysis_id=%s step=%s",
                    analysis_id, step,
                )
                break
            prompt = _build_tool_prompt(
                step=step,
                max_steps=self._max_tool_steps,
                subject_type=subject_type,
                subject_id=subject_id,
                goal=goal,
                failure_text=failure_text,
                evidence_lines=evidence_lines,
                tool_lines=[
                    render_tool_line(i, item) for i, item in enumerate(observations, start=1)
                ],
            )
            try:
                job_id = self._jobs.submit(
                    prompt,
                    temperature=0.0,
                    max_tokens=_MAX_TOOL_DECISION_TOKENS,
                    session_id=analysis_id,
                    response_format=_TOOL_DECISION_RESPONSE_FORMAT,
                    role=ROLE_FAILURE_ANALYSIS,
                    system=_TOOL_SYSTEM_PROMPT,
                )
                raw = await self._jobs.wait(job_id)
                decision = extract_json(raw)
            except Exception:  # noqa: BLE001 - gathering never breaks diagnosis
                logger.warning(
                    "failure_analysis: evidence-gathering step failed analysis_id=%s step=%s",
                    analysis_id, step, exc_info=True,
                )
                break
            if not isinstance(decision, dict):
                break
            action = str(decision.get("action", "")).strip().lower()
            reason = str(decision.get("reason", ""))
            if action not in (TOOL_FIND_TOOL, ACTION_CALL_TOOL):
                # "done", or anything else: the model has nothing more to look
                # up. Either way this phase is over - it is not the place to
                # argue with the model about its own vocabulary.
                break

            if action == TOOL_FIND_TOOL:
                arguments: dict[str, Any] = {"goal": str(decision.get("goal", "")).strip()[:1000]}
            else:
                raw_arguments = decision.get("arguments")
                arguments = {
                    "tool": str(decision.get("tool", "")).strip(),
                    "arguments": raw_arguments if isinstance(raw_arguments, dict) else {},
                }
            check = guard.check(
                analysis_id, action, arguments, state_token=state_token, reason=reason
            )
            if not check.allowed:
                event = str((check.observation or {}).get("event") or "duplicate_tool_call")
                logger.info(
                    "failure_analysis: repeated tool call refused analysis_id=%s "
                    "action=%s event=%s stop=%s",
                    analysis_id, action, event, check.stop,
                )
                if check.stop:
                    break
                # A silent `continue` left the model no way to learn that the
                # call was refused: the next prompt looked exactly like the
                # last one, so it repeated the same call and spent the budget
                # on nothing. The refusal is an observation about the
                # diagnosis attempt, the same way an invalid tool request is
                # (#175).
                observations.append(
                    self._bounded_tool_observation(
                        step,
                        action,
                        {
                            "operation": arguments.get("tool", "") if action != TOOL_FIND_TOOL else "",
                            "goal": arguments.get("goal", ""),
                            "status": "refused",
                            "error": event,
                            "reason": (
                                "This exact call was already made in this analysis and "
                                "returned nothing new. Ask for different evidence, or "
                                "answer \"done\"."
                            ),
                        },
                        reason,
                    )
                )
                continue

            try:
                if action == TOOL_FIND_TOOL:
                    result = await router.resolve_find_tool(
                        arguments["goal"],
                        session_id=analysis_id,
                        current_task=failure_text,
                    )
                else:
                    result = router.execute_operation(
                        {
                            "operation": arguments["tool"],
                            "args": arguments["arguments"],
                            "session_id": analysis_id,
                        }
                    )
            except ToolRouterError as exc:
                # A bad tool request is evidence about the diagnosis attempt,
                # not a failure of it: record it and let the model try
                # something else within the same budget.
                result = {
                    "operation": arguments.get("tool", ""),
                    "error": "invalid_tool_request",
                    "reason": str(exc),
                }
            except Exception:  # noqa: BLE001 - a tool failure never fails the analysis
                logger.warning(
                    "failure_analysis: tool execution failed analysis_id=%s action=%s",
                    analysis_id, action, exc_info=True,
                )
                break
            guard.record_result(check.call_id, {"action": action, "status": str(result.get("status") or "")})
            observations.append(
                self._bounded_tool_observation(step, action, result, reason)
            )

            if action != TOOL_FIND_TOOL:
                continue
            executable = self._directly_executable_operation(result)
            if executable is None:
                continue
            operation, operation_arguments = executable
            executed = self._execute_resolved_operation(
                analysis_id=analysis_id,
                state_token=state_token,
                step=step,
                operation=operation,
                arguments=operation_arguments,
                reason=reason,
            )
            if executed is not None:
                observations.append(executed)

    @staticmethod
    def _directly_executable_operation(
        result: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """The operation and arguments a resolved ``find_tool`` may run with.

        ``None`` means no direct execution: the ordinary bounded decision flow
        applies instead.

        Asking the local model to confirm "now call the tool you just asked
        for" is not a decision, it is a formality - and an expensive one: each
        controller-style decision costs many minutes on the 7B model, and in
        analysis ``878e8008`` the model answered the formality by repeating its
        ``find_tool`` instead of issuing ``call_tool``. The guard refused the
        duplicate, gathering ended with one observation and no diagnostic
        operation ever ran, and the diagnosis fell back on the stale token
        hypothesis (#177).

        Executing it instead is deterministic: the resolver returned exactly
        one operation, the grant for it is already recorded, and
        :meth:`NexusToolRouter.execute_operation` independently refuses
        anything outside the read-only set. This returns ``None`` whenever that
        determinism does not hold - a resolution that found nothing, or a spec
        that does not *state* it is read-only.

        Arguments are not this method's judgement call. The router binds them
        against the goal and refuses to bind whatever it cannot tie back to
        it, so ``resolved_arguments`` is either the values the goal actually
        named or ``None``. Treating ``None`` as "run it with the defaults" is
        the one reading that must never happen: it is how a goal naming
        ``llama-server`` would have been answered with ``agent-api`` (#177
        review).
        """
        if str(result.get("status") or "") != "found":
            return None
        operation = str(result.get("resolved_tool") or "").strip()
        if not operation:
            return None
        spec = result.get("operation")
        # Read-only must be stated. A missing field is not a "no", and
        # inferring one is how a mutating operation becomes auto-executable.
        if not isinstance(spec, dict) or spec.get("mutates_state") is not False:
            return None
        arguments = result.get("resolved_arguments")
        if not isinstance(arguments, dict):
            return None
        return operation, arguments

    def _execute_resolved_operation(
        self,
        *,
        analysis_id: str,
        state_token: str,
        step: int,
        operation: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        """Run an already-resolved read-only operation through the normal path.

        Deliberately the same ``guard.check`` -> ``execute_operation`` ->
        ``record_result`` sequence an explicit ``call_tool`` takes, so the loop
        guard, the grant rule, the registry's validation and its audit record
        all still apply. Only the LLM decision in front of it is skipped.
        """
        router = self._tool_router
        guard = self._tool_guard
        if router is None or guard is None:
            return None
        # The signature carries the arguments, so the same operation asked for
        # with different values is a different call, not a refused repeat.
        check = guard.check(
            analysis_id,
            ACTION_CALL_TOOL,
            {"tool": operation, "arguments": dict(arguments)},
            state_token=state_token,
            reason=f"executing the read-only operation find_tool resolved: {reason}"[:1000],
        )
        if not check.allowed:
            # Already run in this analysis; its result is in the evidence.
            logger.info(
                "failure_analysis: resolved tool already executed analysis_id=%s operation=%s",
                analysis_id, operation,
            )
            return None
        try:
            result = router.execute_operation(
                {"operation": operation, "args": dict(arguments), "session_id": analysis_id}
            )
        except ToolRouterError as exc:
            result = {
                "operation": operation,
                "error": "invalid_tool_request",
                "reason": str(exc),
            }
        except Exception:  # noqa: BLE001 - a tool failure never fails the analysis
            logger.warning(
                "failure_analysis: resolved tool execution failed analysis_id=%s operation=%s",
                analysis_id, operation, exc_info=True,
            )
            return None
        guard.record_result(
            check.call_id,
            {"action": ACTION_CALL_TOOL, "status": str(result.get("status") or "")},
        )
        logger.info(
            "failure_analysis: executed resolved tool analysis_id=%s operation=%s step=%s",
            analysis_id, operation, step,
        )
        return self._bounded_tool_observation(step, ACTION_CALL_TOOL, result, reason)

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    async def _analyze(
        self,
        *,
        analysis_id: str,
        subject_type: str,
        subject_id: str,
        failure: dict[str, Any] | str,
        evidence: list[dict[str, Any]],
        goal: str | None,
        experiment_id: str | None = None,
        mailbox_task_id: str | None = None,
    ) -> FailureAnalysis:
        if not subject_type or not subject_id:
            raise FailureAnalysisError("subject_type and subject_id are required")

        bounded_evidence = evidence_from_log_entries(evidence, max_items=_MAX_EVIDENCE_ITEMS)
        evidence_lines = [
            render_evidence_line(i, entry) for i, entry in enumerate(bounded_evidence, start=1)
        ]
        evidence_refs = [f"E{i}" for i in range(1, len(bounded_evidence) + 1)]

        if isinstance(failure, dict):
            failure_text = "\n".join(
                f"{key}={value}" for key, value in failure.items() if value is not None
            )
        else:
            failure_text = str(failure)
        failure_text = _bounded(failure_text, _MAX_FAILURE_TEXT_CHARS) or "(no failure detail supplied)"
        bounded_goal = _bounded(goal, _MAX_GOAL_CHARS) if goal else None

        # Check the claims before interpreting them (#172). Read-only,
        # bounded, and optional: without a tool router this returns nothing
        # and the diagnosis below is exactly the one it always was.
        tool_observations = await self._gather_tool_evidence(
            analysis_id=analysis_id,
            subject_type=subject_type,
            subject_id=subject_id,
            goal=bounded_goal,
            failure_text=failure_text,
            evidence_lines=evidence_lines,
        )
        tool_lines = [
            render_tool_line(i, item) for i, item in enumerate(tool_observations, start=1)
        ]
        evidence_refs.extend(f"T{i}" for i in range(1, len(tool_observations) + 1))

        prompt = _build_prompt(
            subject_type=subject_type,
            subject_id=subject_id,
            goal=bounded_goal,
            failure_text=failure_text,
            evidence_lines=evidence_lines,
            tool_lines=tool_lines,
            tool_evidence_note=self._tool_evidence_note(tool_observations),
        )

        model_name = getattr(self._client, "model", None)
        # Local inference goes through the shared job service (#167): one
        # queue, one worker, one set of job semantics for controller and
        # failure-analysis work alike. No deadline is imposed here - the job
        # service decides when a job is genuinely stuck.
        job_id = self._jobs.submit(
            prompt,
            temperature=0.0,
            max_tokens=self._max_tokens,
            session_id=analysis_id,
            role=ROLE_FAILURE_ANALYSIS,
            system=_SYSTEM_PROMPT,
        )
        logger.info(
            "failure_analysis: local LLM job submitted job_id=%s role=%s analysis_id=%s "
            "subject=%s/%s",
            job_id, ROLE_FAILURE_ANALYSIS, analysis_id, subject_type, subject_id,
        )
        try:
            raw = await self._jobs.wait(job_id)
        except LocalLlmJobError as exc:
            # Job failure, cancellation and stuck-job detection all land here
            # and all produce the same durable analysis_unavailable record.
            # Nothing re-triggers an analysis: the original lifecycle failure
            # keeps whatever result it already had.
            logger.warning(
                "failure_analysis: local LLM job did not produce a result job_id=%s "
                "status=%s error_kind=%s analysis_id=%s",
                job_id, getattr(exc, "status", None), getattr(exc, "kind", None), analysis_id,
            )
            return _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=model_name,
                reason=(
                    f"local model call failed: job {job_id} "
                    f"{getattr(exc, 'status', None) or 'failed'}"
                    f"/{getattr(exc, 'kind', None) or 'unknown'}: {exc}"
                ),
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )

        try:
            data = extract_json(raw)
        except LlamaCppClientError as exc:
            # Malformed model output must degrade gracefully, not cascade
            # into a TaskOrchestratorError-style failure of the caller.
            return _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=model_name,
                reason=f"local model returned unparseable output: {exc}",
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )
        if not isinstance(data, dict):
            return _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=model_name,
                reason="local model output was not a JSON object",
                experiment_id=experiment_id,
                mailbox_task_id=mailbox_task_id,
            )

        return _coerce_analysis(
            data,
            analysis_id=analysis_id,
            subject_type=subject_type,
            subject_id=subject_id,
            provider="local",
            model=model_name,
            evidence_refs=evidence_refs,
            experiment_id=experiment_id,
            mailbox_task_id=mailbox_task_id,
        )

    # ------------------------------------------------------------------
    # Persistence / visibility
    # ------------------------------------------------------------------

    def _persist(self, analysis: FailureAnalysis) -> bool:
        """Durably store *analysis*. Returns True on success.

        Never raises - a persistence failure must not break the caller -
        but the caller (analyze_async) uses the return value to decide
        whether it is safe to publish an outbox notification: an outbox
        message that outlives its own DB row would be an untracked orphan
        with no publish-lifecycle record behind it (#148 review).
        """
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO failure_analyses (
                        id, subject_type, subject_id, created_at, provider, model,
                        category, confidence, can_self_recover, needs_external_model,
                        needs_human, analysis_json, experiment_id, mailbox_task_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        analysis.id,
                        analysis.subject_type,
                        analysis.subject_id,
                        analysis.created_at,
                        analysis.provider,
                        analysis.model,
                        analysis.category,
                        analysis.confidence,
                        int(analysis.can_self_recover),
                        int(analysis.needs_external_model),
                        int(analysis.needs_human),
                        _json_dumps(analysis.to_dict()),
                        analysis.experiment_id,
                        analysis.mailbox_task_id,
                        analysis.created_at,
                    ),
                )
            return True
        except Exception:  # noqa: BLE001 - persistence must never break the caller
            logger.exception(
                "failure_analysis: could not persist analysis for %s/%s",
                analysis.subject_type, analysis.subject_id,
            )
            return False

    def _log(self, analysis: FailureAnalysis, elog: Any) -> None:
        if elog is None:
            return
        event = "failure_analysis_unavailable" if analysis.category == CATEGORY_UNAVAILABLE else "failure_analysis_completed"
        level = "warning" if (analysis.category == CATEGORY_UNAVAILABLE or analysis.needs_human) else "info"
        try:
            getattr(elog, level)(
                "failure_analysis",
                event,
                analysis.summary,
                analysis_id=analysis.id,
                category=analysis.category,
                confidence=analysis.confidence,
                can_self_recover=analysis.can_self_recover,
                needs_external_model=analysis.needs_external_model,
                needs_human=analysis.needs_human,
            )
        except Exception:  # noqa: BLE001 - logging must never break the caller
            logger.warning("failure_analysis: could not write elog entry", exc_info=True)

    # ------------------------------------------------------------------
    # Mailbox outbox publishing (#148)
    # ------------------------------------------------------------------

    def _publish_outbox(self, analysis: FailureAnalysis) -> None:
        """Best-effort: publish one outbox notification for *analysis*.

        Never raises and never touches the analysis record's own columns -
        a publish failure here must not alter the underlying diagnosis or
        the lifecycle state it describes. Skipped entirely when this
        service was constructed without outbox configuration, or when the
        analysis has no mailbox task / experiment correlation at all (a
        manual/API-only analysis has nothing to notify).
        """
        if self._outbox_dir is None or self._repo_root is None:
            return
        if not analysis.mailbox_task_id and not analysis.experiment_id:
            return
        try:
            self._write_outbox_file(analysis)
        except Exception:  # noqa: BLE001 - publishing must never break the caller
            logger.exception(
                "failure_analysis: could not write outbox notification for %s", analysis.id
            )

    def _write_outbox_file(self, analysis: FailureAnalysis) -> None:
        assert self._outbox_dir is not None and self._repo_root is not None and self._repo_lock is not None
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        filename = f"{ts}-failure-analysis-{analysis.id[:8]}.md"
        path = self._outbox_dir / filename
        content = render_outbox_content(analysis)
        rel_path = str(path.relative_to(self._repo_root))

        with self._repo_lock:
            # Persist publish intent + full content before the atomic write so
            # a crash between write and the second DB update is recoverable
            # via FailureAnalysisOutboxPublisher.content_fn (same recovery
            # contract as modules.outbox_publisher.OutboxPublisher).
            with self._conn:
                self._conn.execute(
                    """UPDATE failure_analyses
                       SET outbox_file=?, outbox_content=?, publish_status=?, updated_at=?
                       WHERE id=?""",
                    (rel_path, content, PUBLISH_PENDING_WRITE, _utcnow_iso(), analysis.id),
                )

            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)

            with self._conn:
                self._conn.execute(
                    "UPDATE failure_analyses SET publish_status=?, updated_at=? WHERE id=?",
                    (PUBLISH_WRITTEN, _utcnow_iso(), analysis.id),
                )

    def get(self, analysis_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT analysis_json FROM failure_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            return None
        return _json_loads(row["analysis_json"])

    def list_recent(
        self,
        *,
        limit: int = 20,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        query = "SELECT analysis_json FROM failure_analyses"
        clauses: list[str] = []
        params: list[Any] = []
        if subject_type:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [_json_loads(row["analysis_json"]) for row in rows]

    def latest_for_subject(self, subject_type: str, subject_id: str) -> dict[str, Any] | None:
        results = self.list_recent(limit=1, subject_type=subject_type, subject_id=subject_id)
        return results[0] if results else None


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: str) -> dict[str, Any]:
    return json.loads(raw)
