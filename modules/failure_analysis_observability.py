"""Structured observability for the reusable failure-analysis lifecycle (#151).

The underlying FailureAnalysisService intentionally stays focused on diagnosis.
This module instruments its stable public/private seams plus the mailbox call
sites so operators can reconstruct why an analysis did (or did not) happen
from normal Nexus logs.  It never logs prompts, raw model output, credentials,
or full failure text.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import time
from pathlib import Path
from typing import Any, Callable

from modules.redaction import redact

logger = logging.getLogger(__name__)

_INSTALLED = False
_REQUEST_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "failure_analysis_request_context", default={}
)
_PERSIST_OUTCOME: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "failure_analysis_persist_outcome", default=None
)
_FINALIZATION_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "failure_analysis_finalization_source", default="unknown"
)


def _safe_text(value: Any, max_chars: int = 160) -> str:
    return redact(str(value))[:max_chars]


def _safe_model(value: Any) -> str | None:
    if value is None:
        return None
    text = _safe_text(value)
    # Local model identifiers are sometimes full filesystem paths.  The
    # filename is enough to identify the active model without exposing the
    # host's directory layout.
    return Path(text).name if ("/" in text or "\\" in text) else text


def _db_identity(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    return Path(text).name or "unknown"


def _log_directory(value: Any) -> str:
    text = str(value or "")
    return _safe_text(text, 240) if text else "unknown"


def _event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one safe structured event into the normal Nexus log pipeline."""
    payload = {key: value for key, value in fields.items() if value is not None}
    try:
        logger.log(
            level,
            event,
            extra={"event": event, "phase": "failure_analysis", **payload},
        )
    except Exception:  # noqa: BLE001 - diagnostics must never affect lifecycle
        return


def _context_fields(**overrides: Any) -> dict[str, Any]:
    fields = dict(_REQUEST_CONTEXT.get())
    fields.pop("_requested_logged", None)
    fields.update({key: value for key, value in overrides.items() if value is not None})
    return fields


def _analysis_fields(analysis: Any) -> dict[str, Any]:
    return _context_fields(
        experiment_id=getattr(analysis, "experiment_id", None),
        mailbox_task_id=getattr(analysis, "mailbox_task_id", None),
        analysis_id=getattr(analysis, "id", None),
        subject_type=getattr(analysis, "subject_type", None),
        subject_id=getattr(analysis, "subject_id", None),
        analyzer_provider=getattr(analysis, "provider", None),
        analyzer_model=_safe_model(getattr(analysis, "model", None)),
    )


def _request_fields(
    *,
    subject_type: str,
    subject_id: str,
    experiment_id: str | None,
    mailbox_task_id: str | None,
) -> dict[str, Any]:
    context = _REQUEST_CONTEXT.get()
    call_site = context.get("call_site")
    if not call_site:
        call_site = "manual_api" if subject_type == "manual" else "service_direct"
    return _context_fields(
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
        subject_type=subject_type,
        subject_id=subject_id,
        call_site=call_site,
    )


def _failure_reason(analysis: Any) -> str | None:
    reason = str(getattr(analysis, "escalation_reason", "") or "")
    if reason.startswith("local model call failed:"):
        return "model_call_failed"
    if reason.startswith("local model returned unparseable output:"):
        return "model_output_invalid"
    if reason == "local model output was not a JSON object":
        return "model_output_invalid"
    return None


def _install_service_instrumentation() -> None:
    import modules.failure_analysis_service as fa

    service_cls = fa.FailureAnalysisService
    if getattr(service_cls, "_failure_analysis_observability_installed", False):
        return

    original_init_db = fa.init_db

    @functools.wraps(original_init_db)
    def observed_init_db(db_path: str = fa.DEFAULT_DB_PATH, *args: Any, **kwargs: Any):
        fields = _context_fields(db_identity=_db_identity(db_path))
        _event("failure_analysis_schema_initializing", **fields)
        try:
            conn = original_init_db(db_path, *args, **kwargs)
        except Exception as exc:
            _event(
                "failure_analysis_schema_initialization_failed",
                level=logging.ERROR,
                reason=type(exc).__name__,
                **fields,
            )
            raise
        _event("failure_analysis_schema_ready", **fields)
        return conn

    fa.init_db = observed_init_db

    original_init = service_cls.__init__

    @functools.wraps(original_init)
    def observed_init(self: Any, *args: Any, **kwargs: Any) -> None:
        db_path = kwargs.get("db_path", fa.DEFAULT_DB_PATH)
        log_dir = kwargs.get("log_dir", fa.central_logging.DEFAULT_LOG_DIR)
        fields = _context_fields(
            db_identity=_db_identity(db_path),
            log_directory=_log_directory(log_dir),
            service_newly_created=True,
            service_reused=False,
        )
        _event("failure_analysis_service_initializing", **fields)
        try:
            original_init(self, *args, **kwargs)
        except Exception as exc:
            _event(
                "failure_analysis_service_initialization_failed",
                level=logging.ERROR,
                reason=type(exc).__name__,
                **fields,
            )
            raise
        _event(
            "failure_analysis_service_ready",
            analyzer_provider="local",
            analyzer_model=_safe_model(getattr(getattr(self, "_client", None), "model", None)),
            **fields,
        )

    service_cls.__init__ = observed_init

    original_analyze_async = service_cls.analyze_async

    @functools.wraps(original_analyze_async)
    async def observed_analyze_async(self: Any, *args: Any, **kwargs: Any):
        subject_type = str(kwargs.get("subject_type") or "")
        subject_id = str(kwargs.get("subject_id") or "")
        fields = _request_fields(
            subject_type=subject_type,
            subject_id=subject_id,
            experiment_id=kwargs.get("experiment_id"),
            mailbox_task_id=kwargs.get("mailbox_task_id"),
        )
        if not _REQUEST_CONTEXT.get().get("_requested_logged"):
            _event("failure_analysis_requested", **fields)
        _event("failure_analysis_started", **fields)
        started = time.monotonic()
        persist_token = _PERSIST_OUTCOME.set(None)
        try:
            analysis = await original_analyze_async(self, *args, **kwargs)
        except Exception as exc:
            # The underlying service is designed not to raise, but retain a
            # defensive event if that contract is ever accidentally broken.
            _event(
                "failure_analysis_unavailable",
                level=logging.ERROR,
                reason=type(exc).__name__,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                **fields,
            )
            raise
        else:
            result_fields = _analysis_fields(analysis)
            result_fields.update(
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                persistence_outcome=_PERSIST_OUTCOME.get() or "unknown",
            )
            if getattr(analysis, "category", None) == fa.CATEGORY_UNAVAILABLE:
                _event(
                    "failure_analysis_unavailable",
                    level=logging.WARNING,
                    reason=_failure_reason(analysis) or "analysis_unavailable",
                    **result_fields,
                )
            else:
                _event("failure_analysis_completed", **result_fields)
            return analysis
        finally:
            _PERSIST_OUTCOME.reset(persist_token)

    service_cls.analyze_async = observed_analyze_async

    original_analyze = service_cls._analyze

    @functools.wraps(original_analyze)
    async def observed_analyze(self: Any, *args: Any, **kwargs: Any):
        fields = _context_fields(
            analysis_id=kwargs.get("analysis_id"),
            subject_type=kwargs.get("subject_type"),
            subject_id=kwargs.get("subject_id"),
            experiment_id=kwargs.get("experiment_id"),
            mailbox_task_id=kwargs.get("mailbox_task_id"),
            analyzer_provider="local",
            analyzer_model=_safe_model(getattr(getattr(self, "_client", None), "model", None)),
        )
        _event("failure_analysis_model_call_started", **fields)
        started = time.monotonic()
        try:
            analysis = await original_analyze(self, *args, **kwargs)
        except Exception as exc:
            _event(
                "failure_analysis_model_call_failed",
                level=logging.ERROR,
                reason=type(exc).__name__,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                **fields,
            )
            raise

        duration_ms = round((time.monotonic() - started) * 1000, 2)
        failure_reason = _failure_reason(analysis)
        if failure_reason == "model_call_failed":
            _event(
                "failure_analysis_model_call_failed",
                level=logging.WARNING,
                reason=failure_reason,
                duration_ms=duration_ms,
                **fields,
            )
        elif failure_reason == "model_output_invalid":
            _event(
                "failure_analysis_model_output_invalid",
                level=logging.WARNING,
                reason=failure_reason,
                duration_ms=duration_ms,
                **fields,
            )
        else:
            _event("failure_analysis_model_call_completed", duration_ms=duration_ms, **fields)
        return analysis

    service_cls._analyze = observed_analyze

    original_persist = service_cls._persist

    @functools.wraps(original_persist)
    def observed_persist(self: Any, analysis: Any) -> bool:
        fields = _analysis_fields(analysis)
        _event("failure_analysis_persist_started", **fields)
        try:
            persisted = bool(original_persist(self, analysis))
        except Exception as exc:
            # _persist currently swallows its own errors; this protects the
            # event sequence if its implementation changes later.
            _PERSIST_OUTCOME.set("failed")
            _event(
                "failure_analysis_persist_failed",
                level=logging.ERROR,
                reason=type(exc).__name__,
                persistence_outcome="failed",
                **fields,
            )
            raise
        if persisted:
            _PERSIST_OUTCOME.set("persisted")
            _event(
                "failure_analysis_persisted",
                persistence_outcome="persisted",
                **fields,
            )
        else:
            _PERSIST_OUTCOME.set("failed")
            _event(
                "failure_analysis_persist_failed",
                level=logging.ERROR,
                reason="database_write_failed",
                persistence_outcome="failed",
                **fields,
            )
        return persisted

    service_cls._persist = observed_persist
    service_cls._failure_analysis_observability_installed = True


def _finalization_source(service: Any, approval_id: str) -> str:
    """Distinguish a first finalization pass from persisted-state recovery."""
    try:
        row = service._conn.execute(  # noqa: SLF001 - lifecycle instrumentation seam
            "SELECT phase FROM task_finalizations WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return "recovery" if row is not None else "initial"
    except Exception:  # noqa: BLE001 - older DB/schema or synthetic tests
        return "initial"


def _install_mailbox_instrumentation() -> None:
    from modules.mailbox import task_service as base
    from modules.mailbox.finalizing_task_service import FinalizingMailboxTaskService

    service_cls = base.MailboxTaskService
    if getattr(service_cls, "_failure_analysis_callsite_observability_installed", False):
        return

    original_lookup = service_cls._failure_analysis

    @functools.wraps(original_lookup)
    def observed_lookup(self: Any, *args: Any, **kwargs: Any):
        existing = getattr(self, "failure_analysis_service", None)
        fields = _context_fields(
            db_identity=_db_identity(getattr(self, "db_path", "")),
            log_directory=_log_directory(self._resolve_log_dir()),
            service_reused=existing is not None,
            service_newly_created=existing is None,
        )
        _event("failure_analysis_service_lookup", **fields)
        result = original_lookup(self, *args, **kwargs)
        if result is None:
            _event(
                "failure_analysis_service_initialization_failed",
                level=logging.ERROR,
                reason="service_lookup_failed",
                **fields,
            )
            if _REQUEST_CONTEXT.get():
                _event(
                    "failure_analysis_skipped",
                    level=logging.WARNING,
                    reason="service_unavailable",
                    **fields,
                )
            return None
        _event(
            "failure_analysis_service_ready",
            analyzer_provider="local",
            analyzer_model=_safe_model(getattr(getattr(result, "_client", None), "model", None)),
            **fields,
        )
        return result

    service_cls._failure_analysis = observed_lookup

    original_task_analysis = service_cls._analyze_task_failure

    @functools.wraps(original_task_analysis)
    def observed_task_analysis(self: Any, approval: Any, status: str, exp: Any) -> None:
        fields = {
            "experiment_id": str(getattr(exp, "id", "") or ""),
            "mailbox_task_id": str(getattr(approval, "id", "") or ""),
            "approval_id": str(getattr(approval, "id", "") or ""),
            "subject_type": "mailbox_task",
            "subject_id": str(getattr(exp, "id", "") or ""),
            "lifecycle_phase": "verification",
            "call_site": "mailbox_task_terminal",
            "_requested_logged": True,
        }
        _event("failure_analysis_requested", **{k: v for k, v in fields.items() if not k.startswith("_")})
        token = _REQUEST_CONTEXT.set(fields)
        try:
            return original_task_analysis(self, approval, status, exp)
        finally:
            _REQUEST_CONTEXT.reset(token)

    service_cls._analyze_task_failure = observed_task_analysis

    original_advance_verified = service_cls._advance_verified

    @functools.wraps(original_advance_verified)
    def observed_advance_verified(self: Any, approval: Any, exp: Any) -> None:
        source = _finalization_source(self, str(getattr(approval, "id", "") or ""))
        token = _FINALIZATION_SOURCE.set(source)
        try:
            return original_advance_verified(self, approval, exp)
        finally:
            _FINALIZATION_SOURCE.reset(token)

    service_cls._advance_verified = observed_advance_verified

    original_finalization_analysis = service_cls._analyze_finalization_failure

    @functools.wraps(original_finalization_analysis)
    def observed_finalization_analysis(
        self: Any, approval: Any, exp: Any, finalization_summary: str
    ) -> None:
        fields = {
            "experiment_id": str(getattr(exp, "id", "") or ""),
            "mailbox_task_id": str(getattr(approval, "id", "") or ""),
            "approval_id": str(getattr(approval, "id", "") or ""),
            "subject_type": "task_finalization",
            "subject_id": str(getattr(exp, "id", "") or ""),
            "lifecycle_phase": "finalization",
            "call_site": "finalization_needs_review",
            "source": _FINALIZATION_SOURCE.get(),
            "_requested_logged": True,
        }
        _event("failure_analysis_requested", **{k: v for k, v in fields.items() if not k.startswith("_")})
        token = _REQUEST_CONTEXT.set(fields)
        try:
            return original_finalization_analysis(self, approval, exp, finalization_summary)
        finally:
            _REQUEST_CONTEXT.reset(token)

    service_cls._analyze_finalization_failure = observed_finalization_analysis

    original_transition = service_cls._transition_finalization_terminal

    @functools.wraps(original_transition)
    def observed_transition(
        self: Any,
        approval: Any,
        exp: Any,
        status: str,
        finalization_summary: str,
    ) -> None:
        if status == base._STATUS_NEEDS_REVIEW:
            _event(
                "finalization_needs_review",
                level=logging.WARNING,
                experiment_id=str(getattr(exp, "id", "") or ""),
                mailbox_task_id=str(getattr(approval, "id", "") or ""),
                approval_id=str(getattr(approval, "id", "") or ""),
                lifecycle_phase="finalization",
                call_site="finalization_terminal_transition",
                source=_FINALIZATION_SOURCE.get(),
                failure_analysis_requested=True,
            )
        return original_transition(self, approval, exp, status, finalization_summary)

    service_cls._transition_finalization_terminal = observed_transition
    service_cls._failure_analysis_callsite_observability_installed = True
    FinalizingMailboxTaskService._failure_analysis_callsite_observability_installed = True


def install() -> None:
    """Install failure-analysis instrumentation exactly once per process."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_service_instrumentation()
    _install_mailbox_instrumentation()
    _INSTALLED = True
    _event(
        "failure_analysis_feature_status",
        importable=True,
        configured=True,
        implementation="failure_analysis_observability_v1",
        analyzer_provider="local",
        call_site="startup",
    )


__all__ = ["install"]
