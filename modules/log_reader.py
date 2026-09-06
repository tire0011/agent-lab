"""Safe log-tailing utilities for the operations API.

Log files are read via ``journalctl`` (systemd) for service logs or from
on-disk log files.  Raw output is redacted before being
returned to prevent accidental credential exposure.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

from modules.redaction import redact

_logger = logging.getLogger(__name__)


_MAX_LINES = 500  # hard upper bound regardless of what caller requests
_DEFAULT_LINES = 50

# Simple pattern to identify log lines that contain an error/warning level.
_ERROR_LEVEL_RE = re.compile(r"(ERROR|CRITICAL|EXCEPTION|Traceback|FAILED)", re.IGNORECASE)
_WARNING_LEVEL_RE = re.compile(r"\b(WARNING|WARN)\b", re.IGNORECASE)

# The systemd units the unified Logs timeline (GET /logs) merges in
# alongside the structured JSONL feed, so the operator sees agent-api,
# nexus-mailbox and llama-server journal messages in the same
# chronological list without picking a service first.
NEXUS_JOURNAL_SERVICES = ("agent-api", "nexus-mailbox", "llama-server")


def _canonical_journal_timestamp(raw: str) -> str:
    """Reformat a ``--utc --output=short-iso`` journalctl timestamp (e.g.
    ``2026-08-17T12:00:00+00:00``) into the exact same string shape
    structured JSONL entries use (``...123Z``, see
    ``modules.central_logging._iso_now``), so the unified Logs feed's
    generic, string-based chronological sort/cursor helpers
    (``central_logging._entry_sort_key``/``encode_log_cursor``) order
    journal and structured entries correctly against each other instead
    of comparing two different timestamp string shapes. Falls back to
    the raw value, unparsed, if it doesn't look like a timestamp at all
    - a malformed line must not crash the merge, only sort a little less
    precisely.
    """
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return raw
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_journal_line(service: str, line: str) -> dict | None:
    """Normalize one already-redacted ``--utc --output=short-iso``
    journalctl line for *service* into the same shape as a structured
    JSONL log entry, so the unified Logs feed can merge and sort both
    kinds of entries together. Returns ``None`` for a blank line or one
    of journalctl's own header/sentinel lines (e.g. ``-- No entries --``
    or ``-- Logs begin at ... --``), which start with ``--`` and are not
    an actual log message - most notably printed instead of any real
    output once a ``--until`` page boundary has walked back past a
    service's oldest retained entry.
    """
    line = line.strip()
    if not line or line.startswith("--"):
        return None
    # "--utc --output=short-iso" lines look like
    # "2026-08-17T12:00:00+00:00 host svc[1234]: message text".
    parts = line.split(" ", 3)
    timestamp = _canonical_journal_timestamp(parts[0]) if parts else ""
    message = parts[-1] if len(parts) >= 4 else line
    if _ERROR_LEVEL_RE.search(line):
        level = "ERROR"
    elif _WARNING_LEVEL_RE.search(line):
        level = "WARNING"
    else:
        level = "INFO"
    return {
        "timestamp": timestamp,
        "level": level,
        "logger": "journalctl",
        "phase": "system",
        "event": f"journal:{service}",
        "message": message,
        "source": service,
    }


def _journal_until_value(timestamp: str) -> str | None:
    """Convert one canonical (``...123Z``) merged-feed timestamp into a
    ``--until=`` value ``journalctl --utc`` accepts, so a *GET /logs*
    pagination cursor can be used to fetch the journal window at or
    before that exact point instead of always the newest tailed lines -
    the only way "load older" can ever reach further back into a
    service's journal history instead of endlessly re-showing (or
    permanently losing) the same fixed newest-N-lines window. Returns
    ``None`` for anything unparsable so the caller can safely skip
    ``--until`` instead of risking an incorrectly-scoped, possibly
    duplicate-producing query.
    """
    text = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _run_journalctl(args: list[str], timeout: int = 15) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "journalctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "journalctl timed out"
    return proc.returncode or 0, out_b.decode(errors="replace")


class LogReader:
    """Async helper that reads service logs via journalctl."""

    async def tail_service_logs(self, service: str, lines: int = _DEFAULT_LINES) -> list[str]:
        """Return up to *lines* most recent log lines for *service*."""
        lines = min(max(1, lines), _MAX_LINES)
        _, out = await _run_journalctl(
            ["-u", service, f"-n{lines}", "--no-pager", "--output=short-iso"],
        )
        raw_lines = out.splitlines()
        return [redact(l) for l in raw_lines]

    async def tail_journal_entries(
        self,
        service: str,
        lines: int = _DEFAULT_LINES,
        before: str | None = None,
        search: str | None = None,
    ) -> list[dict]:
        """Return up to *lines* parsed, structured-shaped journal entries
        for *service*, raising if the read failed (journalctl missing,
        non-zero exit, or timeout) so a caller merging several sources can
        report a per-source error instead of silently returning nothing.

        *before*, if given, is a canonical (``...123Z``) merged-feed
        timestamp; it is translated into ``--until=`` so the *lines*
        returned are the window at or before that point rather than
        always the newest tail, letting a *GET /logs* pagination cursor
        page further back into this service's journal instead of only
        ever re-reading (or never reaching past) the same fixed newest
        window. An unparsable *before* is ignored (falls back to the
        newest-tail behaviour) rather than risking an incorrectly-scoped
        query.

        Each returned entry also carries two internal-only keys,
        ``_source`` and ``_rseq`` (mirroring ``central_logging.read_log_entries``),
        so ``central_logging``'s generic, source-agnostic
        ``_entry_sort_key``/``encode_log_cursor``/``decode_log_cursor``
        can order and paginate journal entries together with structured
        ones with no journal-specific pagination logic required.
        """
        lines = min(max(1, lines), _MAX_LINES)
        args = ["-u", service, f"-n{lines}", "--no-pager", "--output=short-iso", "--utc"]
        if before:
            until_value = _journal_until_value(before)
            if until_value:
                args.append(f"--until={until_value}")
        if search and search.strip():
            # Push the search into journalctl itself, so the *whole*
            # retained journal is searched and `-n` then keeps the newest
            # matches - filtering the newest N lines here in Python
            # instead would only ever find a match inside that window.
            # The term is escaped: `--grep` is a regex, and an operator's
            # free-text search (a path, a bracketed prefix, ...) must be
            # matched literally rather than reinterpreted as a pattern.
            args.append(f"--grep={re.escape(search.strip())}")
            args.append("--case-sensitive=false")
        returncode, out = await _run_journalctl(args)
        if returncode != 0:
            raise RuntimeError(f"journalctl failed for {service} (exit code {returncode})")
        raw_lines = out.splitlines()
        total = len(raw_lines)
        entries = []
        for index, raw_line in enumerate(raw_lines):
            entry = parse_journal_line(service, redact(raw_line))
            if entry:
                entry["_source"] = f"journal:{service}"
                entry["_rseq"] = total - 1 - index
                entries.append(entry)
        return entries

    async def read_all_journal_entries(
        self,
        services: tuple[str, ...] = NEXUS_JOURNAL_SERVICES,
        lines: int = 100,
        before: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], dict[str, str]]:
        """Fetch and merge journal entries for every service in *services*
        concurrently. One service failing (unavailable journalctl, unknown
        unit, timeout, ...) does not prevent the others from being
        returned; failures are reported per service in the second element
        of the returned tuple instead. *before*, if given, is forwarded to
        ``tail_journal_entries`` on every service (see there).
        """
        async def _one(service: str) -> tuple[str, list[dict], str | None]:
            try:
                return service, await self.tail_journal_entries(
                    service, lines, before=before, search=search,
                ), None
            except Exception as exc:  # noqa: BLE001 - reported per source, never raised
                return service, [], str(exc)

        results = await asyncio.gather(*(_one(service) for service in services))
        entries: list[dict] = []
        errors: dict[str, str] = {}
        for service, service_entries, error in results:
            entries.extend(service_entries)
            if error:
                errors[service] = error
        return entries, errors

    async def get_error_logs(self, service: str, since_minutes: int = 60) -> list[dict]:
        """Return log lines at ERROR/CRITICAL level from the last *since_minutes* minutes."""
        since_minutes = min(max(1, since_minutes), 1440)  # cap at 24 h
        since_dt = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
        _, out = await _run_journalctl(
            ["-u", service, f"--since={since_str}", "--no-pager",
             "--output=short-iso", "-p", "err"],
        )
        result: list[dict] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # Try to parse timestamp from short-iso format: "2026-08-17T12:00:00+0000 host svc: msg"
            parts = line.split(" ", 3)
            timestamp = parts[0] if parts else ""
            message = redact(parts[-1] if len(parts) >= 4 else line)
            level = "ERROR" if _ERROR_LEVEL_RE.search(line) else "WARNING"
            result.append({"timestamp": timestamp, "level": level, "message": message[:500]})
        return result[:200]  # cap at 200 entries
