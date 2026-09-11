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
* **Local model first.** Uses :class:`~modules.llama_cpp_client.LlamaCppClient`
  against the configured local Nexus LLM by default; the provider is not
  hard-coded into the analysis shape, so a future external-model fallback
  can be added without changing callers.
* **No secrets in the prompt or the stored result.** Every piece of text
  that reaches the model or the database goes through
  ``modules.redaction.redact`` first.
* **Reusable, not finalization-only.** :meth:`analyze` takes a generic
  ``subject_type``/``subject_id`` plus a caller-supplied evidence bundle;
  :meth:`analyze_from_logs` is a convenience helper for the common case of
  diagnosing from an experiment's/mailbox task's structured JSONL logs, but
  callers that have their own evidence are not forced to know about
  ``central_logging`` at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import central_logging
from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.outbox_publisher import PUBLISH_PENDING_WRITE, PUBLISH_WRITTEN, RepoLock
from modules.redaction import redact
from modules.run_store import DEFAULT_DB_PATH, init_db

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

_DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_TOKENS = 900

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
    "confident rather than guessing with false certainty."
)

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
Cite evidence by its [E<n>] label in "evidence" lists. If the evidence does
not support a confident diagnosis, say so via a low confidence value and a
non-empty "missing_information" list rather than fabricating certainty."""


def _build_prompt(
    *,
    subject_type: str,
    subject_id: str,
    goal: str | None,
    failure_text: str,
    evidence_lines: list[str],
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
        analysis_timeout_seconds: float | None = None,
        log_dir: str | Path = central_logging.DEFAULT_LOG_DIR,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        mailbox_config: Any | None = None,
        outbox_dir: str | Path | None = None,
        repo_root: str | Path | None = None,
    ) -> None:
        self.db_path = db_path
        timeout = analysis_timeout_seconds
        if timeout is None:
            timeout = float(os.environ.get("NEXUS_FAILURE_ANALYSIS_TIMEOUT_SECONDS", _DEFAULT_ANALYSIS_TIMEOUT_SECONDS))
        self._client = llama_client or LlamaCppClient(timeout=max(5.0, timeout))
        self._max_tokens = max(200, max_tokens)
        self._log_dir = Path(log_dir)

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

    def close(self) -> None:
        self._conn.close()

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

        prompt = _build_prompt(
            subject_type=subject_type,
            subject_id=subject_id,
            goal=bounded_goal,
            failure_text=failure_text,
            evidence_lines=evidence_lines,
        )

        model_name = getattr(self._client, "model", None)
        try:
            raw = await self._client.complete(
                prompt=prompt,
                system=_SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except LlamaCppClientError as exc:
            return _unavailable_analysis(
                analysis_id=analysis_id,
                subject_type=subject_type,
                subject_id=subject_id,
                provider="local",
                model=model_name,
                reason=f"local model call failed: {exc}",
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
