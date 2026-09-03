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


def parse_journal_line(service: str, line: str) -> dict | None:
    """Normalize one already-redacted ``--output=short-iso`` journalctl
    line for *service* into the same shape as a structured JSONL log
    entry, so the unified Logs feed can merge and sort both kinds of
    entries together. Returns ``None`` for a blank line.
    """
    line = line.strip()
    if not line:
        return None
    # "--output=short-iso" lines look like
    # "2026-08-17T12:00:00+0000 host svc[1234]: message text".
    parts = line.split(" ", 3)
    timestamp = parts[0] if parts else ""
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

    async def tail_journal_entries(self, service: str, lines: int = _DEFAULT_LINES) -> list[dict]:
        """Return up to *lines* parsed, structured-shaped journal entries
        for *service*, raising if the read failed (journalctl missing,
        non-zero exit, or timeout) so a caller merging several sources can
        report a per-source error instead of silently returning nothing.
        """
        lines = min(max(1, lines), _MAX_LINES)
        returncode, out = await _run_journalctl(
            ["-u", service, f"-n{lines}", "--no-pager", "--output=short-iso"],
        )
        if returncode != 0:
            raise RuntimeError(f"journalctl failed for {service} (exit code {returncode})")
        entries = []
        for raw_line in out.splitlines():
            entry = parse_journal_line(service, redact(raw_line))
            if entry:
                entries.append(entry)
        return entries

    async def read_all_journal_entries(
        self, services: tuple[str, ...] = NEXUS_JOURNAL_SERVICES, lines: int = 100,
    ) -> tuple[list[dict], dict[str, str]]:
        """Fetch and merge journal entries for every service in *services*
        concurrently. One service failing (unavailable journalctl, unknown
        unit, timeout, ...) does not prevent the others from being
        returned; failures are reported per service in the second element
        of the returned tuple instead.
        """
        async def _one(service: str) -> tuple[str, list[dict], str | None]:
            try:
                return service, await self.tail_journal_entries(service, lines), None
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
