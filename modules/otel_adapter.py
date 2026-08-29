"""Vendor-neutral OpenTelemetry adapter for Nexus runtime spans.

Design goals (see issue #76):
  * Nexus-side code never imports the ``opentelemetry`` packages directly —
    only this module does, and only lazily. If those packages are not
    installed, this module is a complete, silent no-op.
  * Configuration is entirely through the *standard* OpenTelemetry
    environment variables (``OTEL_EXPORTER_OTLP_ENDPOINT``,
    ``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_SERVICE_NAME``, ...). Nothing is
    hard-coded, and no Grafana-specific configuration or credentials ever
    live in this codebase.
  * A missing/invalid OTLP endpoint, an unreachable collector, exporter
    timeouts, or any other telemetry failure must *never* fail or slow down
    the actual Nexus task. Every call in this module swallows its own
    exceptions.
  * No local collector/Grafana/Tempo/Loki/Prometheus is required or
    started; spans are exported directly from this process via OTLP when
    configured, straight to whatever OTLP-compatible backend the operator
    points it at (Grafana Cloud is the backend used for the issue's manual
    end-to-end verification).

Usage:
    with otel_adapter.span("llm.generate", trace_id=..., span_id=...) as span:
        ...  # span is None when tracing is disabled/unavailable
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any, Iterator

logger = logging.getLogger("nexus.otel")

_lock = threading.Lock()
_configured = False
_enabled = False
_tracer: Any = None


def reset_for_tests() -> None:
    """Forget any previous configuration attempt (unit tests only)."""
    global _configured, _enabled, _tracer
    with _lock:
        _configured = False
        _enabled = False
        _tracer = None


def is_enabled() -> bool:
    """Return whether spans are actually being exported anywhere.

    Configuration is attempted at most once per process (subsequent calls
    return the cached result) so a broken/unreachable endpoint cannot cause
    repeated slow setup attempts on every span.
    """
    _ensure_configured()
    return _enabled


def _ensure_configured() -> None:
    global _configured, _enabled, _tracer
    if _configured:
        return
    with _lock:
        if _configured:
            return
        _configured = True
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            # Nothing configured: tracing stays fully disabled. This is the
            # default, expected state for local development and for the
            # small Nexus VM when no external backend is configured.
            return
        try:
            _enabled = _build_tracer(endpoint)
        except Exception:  # noqa: BLE001 - telemetry setup must never crash Nexus
            logger.warning(
                "otel_adapter: failed to configure OpenTelemetry exporter; "
                "tracing disabled for this process",
                exc_info=True,
            )
            _enabled = False
            _tracer = None


def _build_tracer(endpoint: str) -> bool:
    global _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.info(
            "otel_adapter: opentelemetry packages not installed; "
            "runtime tracing export disabled (endpoint=%s)",
            endpoint,
        )
        return False

    # OTEL_SERVICE_NAME / OTEL_EXPORTER_OTLP_HEADERS are read natively by
    # the SDK/exporter from the environment; a sane default service name is
    # only supplied if the operator did not set one explicitly.
    os.environ.setdefault("OTEL_SERVICE_NAME", "nexus")

    resource = Resource.create()
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("nexus.runtime")
    logger.info("otel_adapter: OTLP tracing enabled (endpoint=%s)", endpoint)
    return True


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Context manager wrapping one OpenTelemetry span, or a no-op.

    Any ``attributes`` value that is not ``None`` is recorded as
    ``nexus.<key>`` on the span (e.g. ``nexus.trace_id``,
    ``nexus.operation``), purely for cross-referencing with the existing
    JSONL logs from within the trace backend UI.
    """
    if not is_enabled():
        yield None
        return

    try:
        from opentelemetry.trace import Status, StatusCode
    except ImportError:
        yield None
        return

    try:
        cm = _tracer.start_as_current_span(name)
        otel_span = cm.__enter__()
        for key, value in attributes.items():
            if value is not None:
                otel_span.set_attribute(f"nexus.{key}", value)
    except Exception:  # noqa: BLE001 - never fail the caller's operation
        logger.debug("otel_adapter: failed to start span %r", name, exc_info=True)
        yield None
        return

    error: BaseException | None = None
    try:
        yield otel_span
    except BaseException as exc:  # noqa: BLE001 - re-raised below, recorded first
        error = exc
        raise
    finally:
        try:
            if error is not None:
                otel_span.record_exception(error)
                otel_span.set_status(Status(StatusCode.ERROR, str(error)))
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - exporter/shutdown issues are not fatal
            logger.debug("otel_adapter: failed to end span %r", name, exc_info=True)


def shutdown() -> None:
    """Best-effort flush/shutdown of the tracer provider (e.g. at exit)."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            force_flush(timeout_millis=2000)
    except Exception:  # noqa: BLE001
        logger.debug("otel_adapter: shutdown/flush failed", exc_info=True)
