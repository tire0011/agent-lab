"""Safe log-tailing utilities for the operations API.

Log files are read via ``journalctl`` (systemd) for service logs or from
explicitly allowed on-disk log files.  Raw output is redacted before being
returned to prevent accidental credential exposure.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

from modules.redaction import redact

_logger = logging.getLogger(__name__)

_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "nexus-api",
    "nexus-mailbox",
    "llama",
})

_MAX_LINES = 500  # hard upper bound regardless of what caller requests
_DEFAULT_LINES = 50

# Simple pattern to identify log lines that contain an error/warning level.
_ERROR_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|EXCEPTION|Traceback|FAILED)\b", re.IGNORECASE)


def _validate_service(service: str) -> None:
    if service not in _ALLOWED_SERVICES:
        raise ValueError(
            f"unknown service {service!r}; allowed: {sorted(_ALLOWED_SERVICES)}"
        )


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
        _validate_service(service)
        lines = min(max(1, lines), _MAX_LINES)
        _, out = await _run_journalctl(
            ["-u", service, f"-n{lines}", "--no-pager", "--output=short-iso"],
        )
        raw_lines = out.splitlines()
        return [redact(l) for l in raw_lines]

    async def get_error_logs(self, service: str, since_minutes: int = 60) -> list[dict]:
        """Return log lines at ERROR/CRITICAL level from the last *since_minutes* minutes."""
        _validate_service(service)
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
