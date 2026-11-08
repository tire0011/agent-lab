from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from models import (
    ExperimentLogEntry,
    ExperimentLogsResponse,
    LogTraceResponse,
    UnifiedLogsResponse,
)
from modules.experiment_service import ExperimentService
from modules.log_reader import LogReader

from api import LIMIT_MIN, app, get_experiment_service, get_log_reader
from fastapi import HTTPException
from modules import central_logging as central_logging_module
from modules import log_trace as log_trace_module
from modules.experiment_service import ExperimentNotFoundError
from modules.redaction import sanitized_reason as _sanitized_reason
import api

router = APIRouter(dependency_overrides_provider=app)
public_router = APIRouter(dependency_overrides_provider=app)


EXPERIMENT_LOGS_DEFAULT_LIMIT = 500
EXPERIMENT_LOGS_MAX_LIMIT = 5000
UNIFIED_LOGS_DEFAULT_LIMIT = 500
UNIFIED_LOGS_MAX_LIMIT = 5000


_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}

# Coarse grouping of a structured entry's ``phase`` into the timeline's ``source``;
# experiment/mailbox task/job ids remain available as detail/filter fields on every entry.
_MAILBOX_SOURCE_PHASES = {
    "mailbox",
    "queued",
    # Entries written before the mailbox phase fix carry the logger's module name as
    # their phase; recognizing those reclassifies the existing history on read, no migration.
    "mailbox_processor",
    "poll_runner",
    "processing",
    "retention",
}
_CONTROLLER_SOURCE_PHASES = {
    "research", "selection", "selection_reformat_retry", "selection_policy_replan",
}


def _derive_log_source(phase: str | None) -> str:
    if phase in _MAILBOX_SOURCE_PHASES:
        return "mailbox"
    if phase in _CONTROLLER_SOURCE_PHASES:
        return "controller"
    if phase == "chat":
        return "chat"
    if phase == "system":
        return "system"
    return "experiment"


def _apply_log_source(raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for raw in raw_entries:
        raw["source"] = _derive_log_source(raw.get("phase"))
    return raw_entries


@router.get("/experiments/{experiment_id}/logs", response_model=ExperimentLogsResponse)
def get_experiment_logs(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=EXPERIMENT_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=EXPERIMENT_LOGS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentLogsResponse:
    """Single, chronological view of one experiment's complete run - mailbox
    approval through research, implementation, verification, cleanup - from the
    durable JSONL log, merged with its mailbox task's pre-experiment entries.
    """
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")

    if level is not None and level.upper() not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail="invalid level filter")

    mailbox_task_id = api._mailbox_task_id(service, experiment.id)
    try:
        # Fetch one extra entry to detect whether an older page remains.
        raw_entries = central_logging_module.read_experiment_logs(
            service.experiment_log_dir, experiment.id,
            mailbox_task_id=mailbox_task_id, level=level, phase=phase,
            limit=limit + 1, before=cursor,
        )
    except OSError as exc:
        api._logger.error("experiment log read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries, next_cursor = _paginate_log_entries(raw_entries, limit)
    _apply_log_source(raw_entries)

    entries: list[ExperimentLogEntry] = []
    for raw in raw_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole response.
            continue
    return ExperimentLogsResponse(
        count=len(entries), experiment_id=experiment.id, entries=entries, next_cursor=next_cursor,
    )


# Per-service journalctl line count merged into every unfiltered-by-id page of
# GET /logs; bounded so one refresh cannot become an unbounded read across three
# services. LogReader normalizes each journal entry's timestamp and attaches the same
# sort/cursor keys, so both entry kinds order and paginate together.
_UNIFIED_LOGS_JOURNAL_LINES = 100


@router.get("/logs", response_model=UnifiedLogsResponse)
async def get_unified_logs(
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    include_journal: bool = Query(default=True),
    service: ExperimentService = Depends(get_experiment_service),
    log_reader: LogReader = Depends(get_log_reader),
) -> UnifiedLogsResponse:
    """One complete, chronological Nexus log timeline.

    Merges every ``<experiment_log_dir>/*.jsonl`` entry (including pre-experiment
    ``task-<mailbox_task_id>`` files) with the systemd journals on unfiltered pages;
    every entry carries a coarse ``source`` usable as a filter. Bounded reads and
    one cursor scheme paginate both kinds without re-showing entries across pages;
    an unreadable journal source is reported in ``source_errors``, not silently dropped.
    """
    if level is not None and level.upper() not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail="invalid level filter")

    resolved_experiment_id = experiment_id
    if experiment_id:
        try:
            resolved_experiment_id = service.resolve_experiment(experiment_id).id
        except ExperimentNotFoundError:
            return UnifiedLogsResponse(count=0, entries=[])

    resolved_mailbox_task_id = mailbox_task_id
    if resolved_experiment_id and not resolved_mailbox_task_id:
        # Correlate the experiment with its mailbox task log automatically, same as the
        # single-experiment endpoint.
        resolved_mailbox_task_id = api._mailbox_task_id(service, resolved_experiment_id)

    try:
        # Fetch one extra entry to detect an older page; still a correct upper bound after
        # merging journal entries - the final page never needs more than `limit + 1`
        # structured entries.
        scan = central_logging_module.scan_log_entries(
            service.experiment_log_dir,
            experiment_id=resolved_experiment_id,
            mailbox_task_id=resolved_mailbox_task_id,
            job_id=job_id,
            session_id=session_id,
            level=level,
            phase=phase,
            search=search,
            limit=limit + 1,
            before=cursor,
        )
    except OSError as exc:
        api._logger.error("unified log read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries = scan.entries
    _apply_log_source(raw_entries)

    # The journal is merged only into the unfiltered-by-id feed, on *every* such page -
    # a cursor must not opt it out, or later pages could never surface any journal entry.
    source_errors: dict[str, str] | None = None
    merged_entries = raw_entries
    is_default_page = (
        not resolved_experiment_id and not resolved_mailbox_task_id
        and not job_id and not session_id
    )
    if include_journal and is_default_page:
        decoded_cursor = central_logging_module.decode_log_cursor(cursor) if cursor else None
        journal_before: str | None = decoded_cursor[0] if decoded_cursor else None
        # Reuse the cursor's timestamp as the journal page boundary; an unrecognized/legacy
        # cursor omits ``--until`` (falls back to each service's newest window) rather than
        # risk an incorrectly-scoped, duplicate-producing query.
        journal_entries, errors = await log_reader.read_all_journal_entries(
            lines=_UNIFIED_LOGS_JOURNAL_LINES, before=journal_before, search=search,
        )
        if decoded_cursor is not None:
            # ``journalctl --until`` has only second precision and is inclusive, so it can return
            # an entry already on the previous page; re-apply the exact sub-second cursor
            # comparison the structured feed uses, so correctness never depends on journalctl's
            # coarser boundary semantics.
            journal_entries = [
                e for e in journal_entries
                if central_logging_module.entry_sort_key(e) < decoded_cursor
            ]
        if level:
            journal_entries = [e for e in journal_entries if e.get("level") == level.upper()]
        if phase:
            journal_entries = [e for e in journal_entries if e.get("phase") == phase]
        merged_entries = raw_entries + journal_entries
        if errors:
            source_errors = errors

    if source:
        wanted = source.strip().lower()
        merged_entries = [e for e in merged_entries if str(e.get("source", "")).lower() == wanted]
    # `search` is deliberately *not* re-applied here: it is pushed down into both readers
    # (JSONL scan and `journalctl --grep`), so it searches the whole retained history,
    # not just whatever one page contained.

    # One shared sort + cursor scheme spans structured and journal entries, so `next_cursor`
    # reflects the true oldest entry of the *merged* page - never just the structured feed's
    # boundary - and no entry is shown twice or skipped across pages.
    merged_entries.sort(key=central_logging_module.entry_sort_key)
    merged_entries, next_cursor = _paginate_log_entries(merged_entries, limit)

    entries: list[ExperimentLogEntry] = []
    for raw in merged_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole response.
            continue
    return UnifiedLogsResponse(
        count=len(entries), entries=entries, next_cursor=next_cursor,
        source_errors=source_errors, truncated=scan.truncated,
    )


@router.get("/logs/trace/{reference:path}", response_model=LogTraceResponse)
def get_log_trace(
    reference: str,
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> LogTraceResponse:
    """Everything Nexus did for one order, from any reference to it.

    *reference* may be an experiment/mailbox task/session/chat job id, an inbox
    message filename, or a distinctive fragment; everything sharing the resolved
    correlation ids comes back.

    An unresolvable reference returns ``found: false`` with an empty timeline rather
    than a 404: "I looked and found nothing" is a useful answer here.
    """
    try:
        trace = log_trace_module.resolve_trace(
            service.experiment_log_dir, reference, limit=limit,
        )
    except OSError as exc:
        api._logger.error("log trace read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries = _apply_log_source(trace.entries)
    entries: list[ExperimentLogEntry] = []
    for raw in raw_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole trace.
            continue

    return LogTraceResponse(
        reference=reference,
        found=trace.found,
        correlation_ids=trace.correlation_ids,
        summary=log_trace_module.summarize(raw_entries),
        count=len(entries),
        entries=entries,
        truncated=trace.truncated,
    )


def _paginate_log_entries(
    raw_entries: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Split a *raw_entries* list fetched with ``limit + 1`` into ``limit`` entries
    plus the ``next_cursor`` for the following (older) page, if any.

    The cursor anchors to the oldest entry actually returned - not the look-ahead
    entry - and is opaque, so entries sharing a timestamp at a page boundary are
    never skipped or duplicated across "load older" calls.
    """
    if len(raw_entries) <= limit:
        return raw_entries, None
    page = raw_entries[-limit:] if limit > 0 else []
    next_cursor = central_logging_module.encode_log_cursor(page[0]) if page else None
    return page, next_cursor


@public_router.get("/logs/public", response_model=UnifiedLogsResponse)
async def get_public_unified_logs(
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    include_journal: bool = Query(default=True),
    service: ExperimentService = Depends(get_experiment_service),
    log_reader: LogReader = Depends(get_log_reader),
) -> UnifiedLogsResponse:
    """Backwards-compatible alias of ``GET /logs`` - both return the identical,
    complete timeline, and reading logs needs no API key (DECISIONS.md D-012).

    Kept as a separate path only so existing clients keep working; it delegates
    rather than maintaining a second read implementation that could drift.
    """
    return await get_unified_logs(
        level=level,
        phase=phase,
        source=source,
        search=search,
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
        job_id=job_id,
        session_id=session_id,
        cursor=cursor,
        limit=limit,
        include_journal=include_journal,
        service=service,
        log_reader=log_reader,
    )
