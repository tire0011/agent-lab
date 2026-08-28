"""Systemd service management wrapper for the operations API.

Service names are passed through to systemd without a static application allowlist.
All subprocess output is sanitized via :mod:`modules.redaction` before being
returned to callers.
"""

from __future__ import annotations

import asyncio
import logging

from modules.redaction import redact

_logger = logging.getLogger(__name__)

_DEFAULT_NEXUS_SERVICES: tuple[str, ...] = ("agent-api", "nexus-mailbox", "llama-server")

_GRACEFUL_STOP_TIMEOUT = 5  # seconds before SIGKILL


async def _systemctl(args: list[str], timeout: int = 15) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "systemctl timed out"
    return proc.returncode or 0, out_b.decode(errors="replace")


class ServiceOperations:
    """Async wrapper around ``systemctl`` without a static service allowlist."""

    async def restart(self, service: str) -> dict:
        """Restart one service. ``all`` remains a convenience for Nexus services."""
        targets = list(_DEFAULT_NEXUS_SERVICES) if service == "all" else [service]
        results: dict[str, str] = {}
        for svc in targets:
            rc, out = await _systemctl(
                ["restart", svc], timeout=_GRACEFUL_STOP_TIMEOUT + 10
            )
            results[svc] = "restarted" if rc == 0 else f"error: {redact(out)[:100]}"
        return {
            "status": "restarting",
            "service": service,
            "results": results,
            "message": "graceful restart requested",
        }

    async def status(self) -> dict:
        """Return status of the default Nexus services."""
        result: dict[str, str] = {}
        for svc in _DEFAULT_NEXUS_SERVICES:
            rc, out = await _systemctl(["is-active", svc], timeout=5)
            # ``is-active`` returns 0 for active, non-zero otherwise.
            state_line = out.strip().lower()
            if rc == 0 and state_line == "active":
                result[svc] = "running"
            elif "inactive" in state_line:
                result[svc] = "stopped"
            elif "failed" in state_line:
                result[svc] = "error"
            else:
                result[svc] = "unknown"
        return result

    async def reload_llama_model(self) -> dict:
        """Send SIGHUP to the llama-server service to trigger a model reload."""
        rc, out = await _systemctl(["kill", "-s", "HUP", "llama-server"], timeout=10)
        if rc != 0:
            return {
                "status": "error",
                "model": "llama-server",
                "message": redact(out)[:200],
            }
        return {
            "status": "reloading",
            "model": "llama-server",
            "eta_seconds": 30,
            "message": "SIGHUP sent to llama-server service",
        }
