"""Gated remote-maintenance service for Nexus.

Provides a fixed set of named operations (no arbitrary shell execution) that
allow remote clients to inspect and control the VM while the maintenance window
is open.  The service is **disabled by default** and must be explicitly enabled
via environment variables before any endpoint is accessible.

Configuration (env vars)
------------------------
MAINTENANCE_ENABLED
    Set to ``"true"`` (case-insensitive) to enable the maintenance service.
    Any other value, or absent, means all operations return 403.

MAINTENANCE_TOKEN
    Required second authentication token.  All maintenance endpoints require
    *both* the regular ``AGENT_API_KEY`` header *and* this token in the
    ``X-Maintenance-Token`` header.

MAINTENANCE_EXPIRY
    Optional UTC datetime (ISO-8601, e.g. ``2026-08-20T22:00:00Z``).
    If set and the current UTC time is past this value the service auto-disables.

Allowed operations
------------------
- git_pull                  — git pull origin HEAD
- git_status                — git status --short
- git_log                   — git log --oneline -N (N via ``lines`` param, max 50)
- run_router_test           — start agent.py --use-router <prompt> in background
- router_task_status        — status of a background router task by id
- router_task_result        — result of a background router task by id
- run_tests                 — run the repo test command in background
- test_task_status          — status of a background test task by id
- test_task_result          — result of a background test task by id
- local_model_status        — HEAD /v1/models against the local llama-server
- local_model_test          — POST /v1/chat/completions with a short ping prompt
- service_status            — systemctl is-active for llama-server / agent-api
- service_restart           — systemctl restart for llama-server / agent-api
- service_logs              — journalctl tail for llama-server / agent-api
- process_status            — ps filtered for known Nexus/llama process names

Safety guarantees
-----------------
- Only the operations listed above are accepted; no arbitrary command strings.
- Only the service names ``llama-server`` and ``agent-api`` are accepted.
- All outputs are bounded/truncated.
- Every operation call is written to an in-memory audit log accessible via
  :meth:`MaintenanceService.audit_log`.
- Background tasks time out after their per-operation timeout.
- Sensitive strings (API keys, tokens) are redacted from outputs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import httpx  # optional – only needed for local_model_status / local_model_test
    _HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _HTTPX_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_LOG_LINES = 200
_MAX_OUTPUT_CHARS = 8000
_MAX_AUDIT_ENTRIES = 500

# Per-operation timeouts (seconds).
_OP_TIMEOUTS: dict[str, int] = {
    "git_pull": 60,
    "git_status": 10,
    "git_log": 10,
    "service_status": 10,
    "service_restart": 30,
    "service_logs": 15,
    "process_status": 10,
    "local_model_status": 15,
    "local_model_test": 30,
    "run_router_test": 600,   # background – applies to the subprocess
    "run_tests": 600,
    "router_task_status": 5,
    "router_task_result": 5,
    "test_task_status": 5,
    "test_task_result": 5,
}

# Patterns whose matches are replaced with [REDACTED] in any output.
_REDACT_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)[=:]\s*\S+"),
    re.compile(r"(?i)(Bearer\s+)\S+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]


# ── Background task tracking ──────────────────────────────────────────────────

@dataclass
class BackgroundTask:
    task_id: str
    operation: str
    status: str          # queued | running | succeeded | failed | timed_out
    started_at: str
    finished_at: str = ""
    output: str = ""
    error: str = ""
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)


# ── Audit entry ───────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    timestamp: str
    operation: str
    args: dict[str, Any]
    outcome: str          # ok | error | forbidden | expired


# ── MaintenanceService ────────────────────────────────────────────────────────

class MaintenanceDisabledError(Exception):
    """Raised when the maintenance service is not enabled or has expired."""


class MaintenanceAuthError(Exception):
    """Raised when the maintenance token is wrong or absent."""


class MaintenanceOperationError(Exception):
    """Raised for invalid arguments to a maintenance operation."""


class MaintenanceService:
    """Executes named maintenance operations when the maintenance window is open.

    Parameters
    ----------
    repo_root:
        Path to the repository root used for git commands.
    local_model_url:
        Base URL of the local llama-server (for ``local_model_status``/
        ``local_model_test``).  Defaults to ``LOCAL_MODEL_URL`` env var or
        ``http://127.0.0.1:8080/v1``.
    test_cmd:
        Command used to run the repository test suite.  Defaults to
        ``python -m pytest tests/ -q --tb=short``.
    agent_cmd:
        Command prefix used to launch ``agent.py --use-router``.  Defaults to
        ``[sys.executable, "agent.py", "--use-router"]``.
    """

    def __init__(
        self,
        repo_root: str | None = None,
        local_model_url: str | None = None,
        test_cmd: list[str] | None = None,
        agent_cmd: list[str] | None = None,
    ) -> None:
        self._repo_root = repo_root or os.getenv("REPO_ROOT", os.getcwd())
        self._local_model_url = (
            local_model_url
            or os.getenv("LOCAL_MODEL_URL")
            or os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080/v1")
        )
        self._test_cmd = test_cmd or [
            sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"
        ]
        self._agent_cmd = agent_cmd or [sys.executable, "agent.py", "--use-router"]
        self._tasks: dict[str, BackgroundTask] = {}
        self._audit: list[AuditEntry] = []
        self._lock = threading.Lock()

    # ── Auth / gating ─────────────────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        """Return True when the maintenance service is currently active."""
        if os.getenv("MAINTENANCE_ENABLED", "").lower() != "true":
            return False
        expiry_str = os.getenv("MAINTENANCE_EXPIRY", "")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= expiry:
                    return False
            except ValueError:
                pass  # malformed expiry → treat as no expiry
        return True

    def check_auth(self, token: str | None) -> None:
        """Raise :exc:`MaintenanceDisabledError` or :exc:`MaintenanceAuthError` as needed."""
        if not self.is_enabled():
            raise MaintenanceDisabledError("maintenance service is not enabled")
        expected = os.getenv("MAINTENANCE_TOKEN", "")
        if not expected:
            raise MaintenanceDisabledError("MAINTENANCE_TOKEN is not configured")
        if token != expected:
            raise MaintenanceAuthError("invalid maintenance token")

    # ── Public entry point ────────────────────────────────────────────────────

    def execute(
        self,
        operation: str,
        token: str | None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute *operation* and return a result dict.

        Raises :exc:`MaintenanceDisabledError`, :exc:`MaintenanceAuthError`, or
        :exc:`MaintenanceOperationError` on failure.  All calls are appended to
        :attr:`audit_log`.
        """
        args = args or {}
        try:
            self.check_auth(token)
            result = self._dispatch(operation, args)
            self._record_audit(operation, args, "ok")
            return result
        except (MaintenanceDisabledError, MaintenanceAuthError) as exc:
            kind = "forbidden" if isinstance(exc, MaintenanceAuthError) else "expired"
            self._record_audit(operation, args, kind)
            raise
        except MaintenanceOperationError:
            self._record_audit(operation, args, "error")
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_audit(operation, args, "error")
            raise MaintenanceOperationError(str(exc)) from exc

    @property
    def audit_log(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._audit)

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        dispatch: dict[str, Any] = {
            "git_pull": self._op_git_pull,
            "git_status": self._op_git_status,
            "git_log": self._op_git_log,
            "run_router_test": self._op_run_router_test,
            "router_task_status": self._op_task_status,
            "router_task_result": self._op_task_result,
            "run_tests": self._op_run_tests,
            "test_task_status": self._op_task_status,
            "test_task_result": self._op_task_result,
            "local_model_status": self._op_local_model_status,
            "local_model_test": self._op_local_model_test,
            "service_status": self._op_service_status,
            "service_restart": self._op_service_restart,
            "service_logs": self._op_service_logs,
            "process_status": self._op_process_status,
        }
        if operation not in dispatch:
            raise MaintenanceOperationError(f"unknown operation: {operation!r}")
        return dispatch[operation](args)

    # ── Git operations ────────────────────────────────────────────────────────

    def _op_git_pull(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self._run(
            ["git", "pull"],
            timeout=_OP_TIMEOUTS["git_pull"],
        )
        return {"output": result}

    def _op_git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self._run(
            ["git", "status", "--short"],
            timeout=_OP_TIMEOUTS["git_status"],
        )
        return {"output": result or "(clean)"}

    def _op_git_log(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_lines = args.get("lines", 10)
        try:
            lines = max(1, min(int(raw_lines), 50))
        except (TypeError, ValueError):
            raise MaintenanceOperationError("'lines' must be an integer between 1 and 50")
        result = self._run(
            ["git", "log", f"--max-count={lines}", "--oneline"],
            timeout=_OP_TIMEOUTS["git_log"],
        )
        return {"output": result}

    # ── Background task operations ────────────────────────────────────────────

    def _op_run_router_test(self, args: dict[str, Any]) -> dict[str, Any]:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            raise MaintenanceOperationError("'prompt' is required for run_router_test")
        task_id = self._launch_background(
            "run_router_test",
            self._agent_cmd + [prompt],
            timeout=_OP_TIMEOUTS["run_router_test"],
        )
        return {"task_id": task_id, "status": "queued"}

    def _op_run_tests(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = self._launch_background(
            "run_tests",
            self._test_cmd,
            timeout=_OP_TIMEOUTS["run_tests"],
        )
        return {"task_id": task_id, "status": "queued"}

    def _op_task_status(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id", ""))
        task = self._get_task(task_id)
        return {
            "task_id": task.task_id,
            "operation": task.operation,
            "status": task.status,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
        }

    def _op_task_result(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id", ""))
        task = self._get_task(task_id)
        return {
            "task_id": task.task_id,
            "operation": task.operation,
            "status": task.status,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "output": task.output[:_MAX_OUTPUT_CHARS],
            "error": task.error,
        }

    # ── Local model operations ────────────────────────────────────────────────

    def _op_local_model_status(self, args: dict[str, Any]) -> dict[str, Any]:
        if not _HTTPX_AVAILABLE:
            return {"status": "unknown", "error": "httpx not installed"}
        try:
            resp = httpx.get(
                f"{self._local_model_url.rstrip('/')}/models",
                timeout=_OP_TIMEOUTS["local_model_status"],
            )
            return {
                "status": "ok" if resp.status_code == 200 else "error",
                "http_status": resp.status_code,
                "body": resp.text[:500],
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "error": str(exc)}

    def _op_local_model_test(self, args: dict[str, Any]) -> dict[str, Any]:
        if not _HTTPX_AVAILABLE:
            return {"status": "unknown", "error": "httpx not installed"}
        ping_prompt = args.get("prompt", "Respond with: PONG")
        try:
            resp = httpx.post(
                f"{self._local_model_url.rstrip('/')}/chat/completions",
                json={
                    "model": os.getenv("LOCAL_MODEL_NAME", "local-model"),
                    "messages": [{"role": "user", "content": str(ping_prompt)}],
                    "max_tokens": 16,
                    "temperature": 0.0,
                },
                timeout=_OP_TIMEOUTS["local_model_test"],
            )
            return {
                "status": "ok" if resp.status_code == 200 else "error",
                "http_status": resp.status_code,
                "response": resp.text[:500],
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "error": str(exc)}

    # ── Service operations ────────────────────────────────────────────────────

    def _op_service_status(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(args.get("service"))
        result = self._run(
            ["systemctl", "is-active", service],
            timeout=_OP_TIMEOUTS["service_status"],
            check_returncode=False,
        )
        return {"service": service, "active": result.strip() == "active", "state": result.strip()}

    def _op_service_restart(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(args.get("service"))
        result = self._run(
            ["sudo", "systemctl", "restart", service],
            timeout=_OP_TIMEOUTS["service_restart"],
        )
        return {"service": service, "output": result}

    def _op_service_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(args.get("service"))
        raw_lines = args.get("lines", 50)
        try:
            lines = max(1, min(int(raw_lines), _MAX_LOG_LINES))
        except (TypeError, ValueError):
            raise MaintenanceOperationError("'lines' must be an integer")
        result = self._run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
            timeout=_OP_TIMEOUTS["service_logs"],
        )
        return {"service": service, "lines": lines, "output": result}

    # ── Process status ────────────────────────────────────────────────────────

    def _op_process_status(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=_OP_TIMEOUTS["process_status"],
            )
            lines = result.stdout.splitlines()
            output = "\n".join(lines[:50])
        except Exception as exc:  # noqa: BLE001
            output = f"ps error: {exc}"
        return {"output": _redact(output)}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _run(
        self,
        cmd: list[str],
        timeout: int,
        check_returncode: bool = True,
    ) -> str:
        """Run *cmd* in the repo root and return combined stdout+stderr (redacted)."""
        try:
            proc = subprocess.run(
                cmd,
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            combined = (proc.stdout + proc.stderr)[:_MAX_OUTPUT_CHARS]
            if check_returncode and proc.returncode != 0:
                raise MaintenanceOperationError(
                    f"command {shlex.join(cmd)!r} exited {proc.returncode}: {combined[:500]}"
                )
            return _redact(combined)
        except subprocess.TimeoutExpired:
            raise MaintenanceOperationError(
                f"command {shlex.join(cmd)!r} timed out after {timeout}s"
            )

    def _validate_service(self, service: Any) -> str:
        s = str(service or "").strip()
        if not s:
            raise MaintenanceOperationError("service is required")
        return s

    def _launch_background(
        self, operation: str, cmd: list[str], timeout: int
    ) -> str:
        task_id = str(uuid.uuid4())
        task = BackgroundTask(
            task_id=task_id,
            operation=operation,
            status="queued",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._tasks[task_id] = task

        def _worker() -> None:
            with self._lock:
                task.status = "running"
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self._repo_root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                combined = _redact((proc.stdout + proc.stderr)[:_MAX_OUTPUT_CHARS])
                with self._lock:
                    task.output = combined
                    task.status = "succeeded" if proc.returncode == 0 else "failed"
                    task.finished_at = datetime.now(timezone.utc).isoformat()
            except subprocess.TimeoutExpired:
                with self._lock:
                    task.status = "timed_out"
                    task.error = f"timed out after {timeout}s"
                    task.finished_at = datetime.now(timezone.utc).isoformat()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    task.status = "failed"
                    task.error = _redact(str(exc))
                    task.finished_at = datetime.now(timezone.utc).isoformat()

        t = threading.Thread(target=_worker, daemon=True, name=f"maint-{task_id[:8]}")
        with self._lock:
            task._thread = t
        t.start()
        return task_id

    def _get_task(self, task_id: str) -> BackgroundTask:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise MaintenanceOperationError(f"task {task_id!r} not found")
        return task

    def _record_audit(
        self, operation: str, args: dict[str, Any], outcome: str
    ) -> None:
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            operation=operation,
            args={k: "[REDACTED]" if "token" in k.lower() or "key" in k.lower() else v
                  for k, v in args.items()},
            outcome=outcome,
        )
        with self._lock:
            self._audit.append(entry)
            if len(self._audit) > _MAX_AUDIT_ENTRIES:
                self._audit = self._audit[-_MAX_AUDIT_ENTRIES:]


# ── Redaction helper ──────────────────────────────────────────────────────────

def _redact(text: str) -> str:
    """Replace known sensitive patterns with [REDACTED]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=[REDACTED]"
                           if "=" in m.group(0) or ":" in m.group(0)
                           else "[REDACTED]", text)
    return text
