"""Central executor for self-healing operations.

All operations are:
* validated against an explicit allowlist,
* run with a timeout,
* logged to the ``operation_log`` SQLite table via :func:`modules.run_store.log_operation`.

Sensitive output is redacted before storage and before being returned to
API callers.
"""

from __future__ import annotations

import asyncio
import glob as _glob
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.git_operations import GitOperations
from modules.log_reader import LogReader
from modules.redaction import redact
from modules.service_operations import ServiceOperations

# A lock file is considered stale only when it is older than this threshold
# AND its owning process (if recorded inside the file) is no longer alive.
_LOCK_STALE_AGE_SECONDS: int = 3600  # 1 hour

# Keys whose values must never be stored in the audit log parameters/results.
_AUDIT_REDACT_KEYS_RE = re.compile(r"(?i)(key|secret|token|password|credential|value)")

# Keys whose string values must be run through the string redactor before
# storage (they may contain embedded secrets even though the key name itself
# does not match the sensitive-key pattern).
_AUDIT_REDACT_VALUE_KEYS: frozenset[str] = frozenset({"command"})


def _is_stale_lock(path: Path) -> bool:
    """Return True only if *path* is definitely a stale, ownerless lock file.

    A lock is considered safe to remove when **all** of the following hold:
    1. The file is older than ``_LOCK_STALE_AGE_SECONDS``.
    2. The file does NOT contain a PID of a running process (if it contains
       one at all — empty/non-numeric files satisfy this condition trivially).
    """
    try:
        age = time.time() - path.stat().st_mtime
        if age < _LOCK_STALE_AGE_SECONDS:
            return False

        # Try to read a PID from the file content (common convention).
        try:
            content = path.read_text(errors="replace").strip()
        except OSError:
            # Can't read → treat as potentially active.
            return False

        if content.isdigit():
            pid = int(content)
            try:
                # Signal 0 checks liveness without actually signalling.
                os.kill(pid, 0)
                # Process is alive → lock is not stale.
                return False
            except ProcessLookupError:
                # PID does not exist → process is gone.
                pass
            except PermissionError:
                # Process exists but we can't signal it → treat as active.
                return False

        return True
    except OSError:
        return False


def _redact_audit_value(v: object) -> object:
    """Recursively redact *v*, descending into dicts, lists, and tuples."""
    if isinstance(v, dict):
        return _redact_audit_dict(v)
    if isinstance(v, list):
        return [_redact_audit_value(item) for item in v]
    if isinstance(v, tuple):
        return tuple(_redact_audit_value(item) for item in v)
    return v


def _redact_audit_dict(d: dict | None) -> dict:
    """Return a deep copy of *d* with sensitive values replaced at any depth.

    Keys matching the sensitive-key pattern have their value replaced with
    ``"***"`` regardless of how deeply they are nested.  Keys in
    ``_AUDIT_REDACT_VALUE_KEYS`` have their string values run through the
    string redactor so that embedded secrets (e.g. tokens inside a shell
    command) are also sanitised.  Non-sensitive values are recursed into so
    that nested structures are also sanitized.

    If *d* is not a dict (e.g. ``None`` for failed operations), returns ``{}``.
    """
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        if _AUDIT_REDACT_KEYS_RE.search(str(k)):
            out[k] = "***"
        elif k in _AUDIT_REDACT_VALUE_KEYS and isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = _redact_audit_value(v)
    return out

_logger = logging.getLogger(__name__)

_ALLOWED_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")

# Shell tool limits (bootstrap-phase only, available when MCP_WRITE_ENABLED=true).
_SHELL_MAX_TIMEOUT: int = 120         # hard ceiling in seconds
_SHELL_DEFAULT_TIMEOUT: int = 30
_SHELL_MAX_OUTPUT_BYTES: int = 16_384  # 16 KiB before truncation

# Path to ``systemd-run`` if available on this host.  Used to confine each
# shell invocation in a transient cgroup scope so that children calling
# ``setsid()`` cannot escape the kill boundary.
_SYSTEMD_RUN: str | None = shutil.which("systemd-run")

# Secrets that must never appear in responses.
_SECRET_ENV_VARS: frozenset[str] = frozenset({
    "AGENT_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "SECRET_KEY",
})

# Directories where stale temp files are cleaned up.
_TEMP_DIRS: list[str] = [
    os.path.join(_ALLOWED_REPO_ROOT, "tmp"),
    "/tmp/nexus-work",
]

# Lock file patterns to clean.
_LOCK_PATTERNS: list[str] = [
    os.path.join(_ALLOWED_REPO_ROOT, "**", "*.lock"),
    os.path.join(_ALLOWED_REPO_ROOT, "state", "*.lock"),
]


@dataclass
class OperationResult:
    success: bool
    operation: str
    timestamp: str
    data: dict[str, Any]
    error: str | None = None
    duration_ms: int = 0
    status: str = "ok"


class OperationsExecutor:
    """High-level executor that wires together git, service, log and
    healing helpers, records every invocation in the audit log, and
    ensures no secrets are exposed in return values."""

    def __init__(
        self,
        db_path: str | None = None,
        git: GitOperations | None = None,
        services: ServiceOperations | None = None,
        logs: LogReader | None = None,
    ) -> None:
        from modules.run_store import DEFAULT_DB_PATH, init_db

        self._db_path = db_path or DEFAULT_DB_PATH
        self._git = git or GitOperations()
        self._services = services or ServiceOperations()
        self._logs = logs or LogReader()
        # lazy connection — avoid connecting at import time (tests mock this)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            from modules.run_store import init_db
            self._conn = init_db(self._db_path)
        return self._conn

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _log_operation(
        self,
        operation: str,
        params: dict,
        success: bool,
        result: dict,
        error: str | None,
        duration_ms: int,
        executor: str = "agent",
    ) -> None:
        try:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO operation_log
                    (id, timestamp, operation, parameters, success, result, error, duration_ms, executor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    datetime.now(timezone.utc).isoformat(),
                    operation,
                    json.dumps(_redact_audit_dict(params)),
                    1 if success else 0,
                    json.dumps(_redact_audit_dict(result)),
                    error,
                    duration_ms,
                    executor,
                ),
            )
            conn.commit()
        except Exception:
            _logger.exception("failed to write operation_log entry")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def git_pull(self) -> dict:
        return await self._git.pull()

    async def git_status(self) -> dict:
        return await self._git.status()

    async def git_commit_push(self, message: str, files: list[str]) -> dict:
        return await self._git.commit_and_push(message, files)

    async def restart_service(self, service: str) -> dict:
        return await self._services.restart(service)

    async def get_service_status(self) -> dict:
        return await self._services.status()

    async def reload_llama_model(self) -> dict:
        return await self._services.reload_llama_model()

    async def tail_logs(self, service: str, lines: int = 50) -> list[str]:
        return await self._logs.tail_service_logs(service, lines)

    async def get_error_logs(self, service: str, since_minutes: int = 60) -> list[dict]:
        return await self._logs.get_error_logs(service, since_minutes)

    async def fix_stale_locks(self) -> dict:
        removed: list[str] = []
        skipped: list[str] = []
        for pattern in _LOCK_PATTERNS:
            for match in _glob.glob(pattern, recursive=True):
                p = Path(match)
                try:
                    verdict = _is_stale_lock(p)
                    if verdict:
                        os.remove(match)
                        removed.append(p.name)
                    else:
                        skipped.append(p.name)
                except OSError:
                    skipped.append(p.name)
        return {
            "success": True,
            "locks_removed": len(removed),
            "locks_skipped": len(skipped),
            "details": removed[:50],
        }

    async def cleanup_temps(self) -> dict:
        cleaned_bytes = 0
        files_removed = 0
        for tmp_dir in _TEMP_DIRS:
            p = Path(tmp_dir)
            if not p.exists():
                continue
            for child in p.iterdir():
                try:
                    if child.is_file() or child.is_symlink():
                        size = child.stat().st_size
                        child.unlink()
                        cleaned_bytes += size
                        files_removed += 1
                    elif child.is_dir():
                        size = sum(
                            f.stat().st_size
                            for f in child.rglob("*")
                            if f.is_file()
                        )
                        shutil.rmtree(child, ignore_errors=True)
                        cleaned_bytes += size
                        files_removed += 1
                except OSError:
                    pass
        return {
            "success": True,
            "cleaned_bytes": cleaned_bytes,
            "files_removed": files_removed,
        }

    async def detect_problems(self) -> list[dict]:
        problems: list[dict] = []

        # Check for stale locks
        for pattern in _LOCK_PATTERNS:
            for match in _glob.glob(pattern, recursive=True):
                problems.append({
                    "type": "stale_lock",
                    "severity": "high",
                    "path": Path(match).name,  # no absolute path leaked
                    "auto_fixable": True,
                })

        # Check disk usage
        try:
            stat = shutil.disk_usage(_ALLOWED_REPO_ROOT)
            pct = stat.used / stat.total * 100
            if pct > 90:
                problems.append({
                    "type": "high_disk_usage",
                    "severity": "critical",
                    "value": f"{pct:.0f}%",
                    "auto_fixable": False,
                })
            elif pct > 80:
                problems.append({
                    "type": "high_disk_usage",
                    "severity": "warning",
                    "value": f"{pct:.0f}%",
                    "auto_fixable": False,
                })
        except OSError:
            pass

        # Check git status
        try:
            status = await self._git.status()
            if status.get("status") == "dirty":
                problems.append({
                    "type": "uncommitted_changes",
                    "severity": "warning",
                    "value": str(len(status.get("uncommitted", []))),
                    "auto_fixable": False,
                })
        except Exception:
            pass

        return problems

    async def get_safe_env(self) -> dict[str, str]:
        """Return non-secret environment variables relevant to the agent."""
        safe: dict[str, str] = {}
        for key, val in os.environ.items():
            if key in _SECRET_ENV_VARS:
                continue
            if re.search(r"(?i)(key|secret|token|password|credential)", key):
                continue
            safe[key] = val
        return safe

    async def set_config(self, key: str, value: str) -> dict:
        """Write a non-secret config key to the environment config file."""
        if re.search(r"(?i)(key|secret|token|password|credential)", key):
            raise ValueError(f"refusing to store secret-like key: {key!r}")
        config_path = Path(_ALLOWED_REPO_ROOT) / ".env.config"
        lines: list[str] = []
        if config_path.exists():
            lines = config_path.read_text().splitlines()
        # Replace or append
        new_line = f"{key}={value}"
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = new_line
                updated = True
                break
        if not updated:
            lines.append(new_line)
        config_path.write_text("\n".join(lines) + "\n")
        return {"success": True, "key": key}

    async def get_model_status(self) -> dict:
        """Return a best-effort status for known models."""
        status: dict[str, str] = {}

        # Check Azure OpenAI
        if os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_ENDPOINT"):
            status["azure_gpt4o"] = "available"
        else:
            status["azure_gpt4o"] = "not_configured"

        # Check llama via service status
        try:
            svc_status = await self._services.status()
            llama_svc_name = "llama"
            status["llama"] = svc_status.get(llama_svc_name, "unknown")
        except Exception:
            status["llama"] = "unknown"

        return status

    async def test_model(self, model: str, prompt: str) -> dict:
        """Send a minimal test prompt to the given model."""
        allowed_models = {"azure_gpt4o", "llama"}
        if model not in allowed_models:
            raise ValueError(f"model must be one of {sorted(allowed_models)}")
        if len(prompt) > 500:
            raise ValueError("prompt must be <= 500 characters")

        if model == "azure_gpt4o":
            return await self._test_azure_gpt4o(prompt)
        return await self._test_llama(prompt)

    async def _test_azure_gpt4o(self, prompt: str) -> dict:
        try:
            import httpx  # type: ignore
            endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
            deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
            api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
            if not endpoint or not api_key:
                return {"success": False, "model": "azure_gpt4o", "latency_ms": 0,
                        "response": None, "error": "not configured"}
            url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-05-01-preview"
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    headers={"api-key": api_key},
                    json={"messages": [{"role": "user", "content": prompt}], "max_tokens": 50},
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                return {"success": False, "model": "azure_gpt4o", "latency_ms": latency_ms,
                        "response": None, "error": f"HTTP {resp.status_code}"}
            text = resp.json()["choices"][0]["message"]["content"]
            return {"success": True, "model": "azure_gpt4o", "latency_ms": latency_ms,
                    "response": text[:300]}
        except Exception as exc:
            return {"success": False, "model": "azure_gpt4o", "latency_ms": 0,
                    "response": None, "error": type(exc).__name__}

    async def _test_llama(self, prompt: str) -> dict:
        try:
            import httpx  # type: ignore
            llama_url = os.environ.get("LLAMA_API_URL", "http://localhost:8080/completion")
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    llama_url,
                    json={"prompt": prompt, "n_predict": 50},
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                return {"success": False, "model": "llama", "latency_ms": latency_ms,
                        "response": None, "error": f"HTTP {resp.status_code}"}
            text = resp.json().get("content", "")
            return {"success": True, "model": "llama", "latency_ms": latency_ms,
                    "response": text[:300]}
        except Exception as exc:
            return {"success": False, "model": "llama", "latency_ms": 0,
                    "response": None, "error": type(exc).__name__}

    # ------------------------------------------------------------------
    # Bootstrap shell (bounded, redacted, audited)
    # ------------------------------------------------------------------

    async def shell(self, command: str, timeout_seconds: int = _SHELL_DEFAULT_TIMEOUT) -> dict:
        """Run *command* in a bounded, audited shell.

        Only available when ``MCP_WRITE_ENABLED=true``. Executes as the current
        service user with cwd ``AGENT_FS_ROOT`` (default ``/agent``). The
        process is started in its own process group so that the entire group
        (including any child processes) is reliably killed on timeout or output
        overflow — preventing background-daemon spawning.  Output is streamed
        with a hard byte cap; the process is killed if the cap is exceeded.
        Output is redacted before returning.
        """
        if not command or not command.strip():
            raise ValueError("command must not be empty")
        timeout_seconds = max(1, min(timeout_seconds, _SHELL_MAX_TIMEOUT))
        cwd: str | None = os.environ.get("AGENT_FS_ROOT", "/agent") or None
        return await asyncio.to_thread(self._shell_sync, command, timeout_seconds, cwd)

    def _shell_sync(self, command: str, timeout_seconds: int, cwd: str | None) -> dict:
        """Synchronous core of :meth:`shell`; runs in a thread pool worker.

        Confinement strategy
        --------------------
        When ``systemd-run`` is available the command is wrapped in a transient
        user-scoped cgroup (``systemd-run --scope --user``).  This means every
        process spawned by the shell — including those that call ``setsid()`` —
        remains inside the same cgroup and is unconditionally killed when the
        scope is stopped.  On hosts without ``systemd-run`` the shell is still
        isolated in its own session via ``start_new_session=True`` and killed
        via ``os.killpg``; ``setsid`` children may escape in that fallback case
        and the limitation is documented.

        Timeout enforcement
        -------------------
        A ``threading.Timer`` fires after exactly *timeout_seconds* and kills
        the scope/process-group regardless of whether the stdout pipe has been
        closed.  This prevents commands like ``exec >/dev/null 2>&1; sleep 300``
        from bypassing the deadline by closing the pipe early.

        Killing on normal exit
        ----------------------
        A ``killer_thread`` blocks on ``proc.wait()`` and kills the scope/group
        as soon as the shell exits with any code, freeing pipes that background
        children may be holding open.
        """
        import subprocess
        import threading

        chunks: list[bytes] = []
        total_bytes = 0
        truncated = False
        timed_out = False

        # Build the scope unit name before we start the process so it is
        # available inside the closures below.
        scope_unit: str | None = None
        if _SYSTEMD_RUN:
            scope_unit = f"nexus-shell-{uuid.uuid4().hex[:12]}.scope"
            argv = [
                _SYSTEMD_RUN, "--scope", "--user", "--collect", "--quiet",
                f"--unit={scope_unit}",
                "-p", "KillMode=control-group",
                "/bin/sh", "-c", command,
            ]
        else:
            argv = ["/bin/sh", "-c", command]

        def _kill_scope_and_group(pgid: int | None) -> None:
            """Kill every process in the scope cgroup (systemd) AND the pgid."""
            # systemd scope: kills all cgroup members regardless of setsid
            if scope_unit:
                try:
                    subprocess.run(
                        ["systemctl", "--user", "stop", scope_unit],
                        timeout=5,
                        capture_output=True,
                    )
                except Exception:
                    pass
            # pgid fallback: kills process group even without systemd
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

        with subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # safe in multithreaded/asyncio contexts
            cwd=cwd,
        ) as proc:
            # Capture PGID immediately while the process is alive.
            try:
                _pgid: int | None = os.getpgid(proc.pid)
            except (ProcessLookupError, OSError):
                _pgid = None

            def _kill() -> None:
                _kill_scope_and_group(_pgid)

            # ------------------------------------------------------------------
            # Absolute deadline timer — fires after timeout_seconds regardless
            # of stdout state.  Prevents pipe-close bypass.
            # ------------------------------------------------------------------
            def _on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                _kill()

            timer = threading.Timer(timeout_seconds, _on_timeout)
            timer.daemon = True
            timer.start()

            # ------------------------------------------------------------------
            # killer_thread: wait for shell exit then free the pipe; sets
            # _proc_done so the main thread knows the process is truly gone.
            # ------------------------------------------------------------------
            _proc_done = threading.Event()

            def _killer() -> None:
                try:
                    proc.wait()
                except (ChildProcessError, OSError):
                    pass
                _proc_done.set()
                _kill()

            killer_thread = threading.Thread(target=_killer, daemon=True)
            killer_thread.start()

            # ------------------------------------------------------------------
            # reader_thread: bounded stdout read
            # ------------------------------------------------------------------
            def _reader() -> None:
                nonlocal total_bytes, truncated
                assert proc.stdout is not None
                chunk_size = 4096
                while True:
                    chunk = proc.stdout.read(chunk_size)
                    if not chunk:
                        break
                    remaining = _SHELL_MAX_OUTPUT_BYTES - total_bytes
                    if remaining <= 0:
                        truncated = True
                        _kill()
                        while proc.stdout.read(chunk_size):
                            pass
                        break
                    if len(chunk) > remaining:
                        # Bytes beyond the cap were already consumed —
                        # trimming always means truncation.
                        chunks.append(chunk[:remaining])
                        total_bytes += remaining
                        truncated = True
                        _kill()
                        while proc.stdout.read(chunk_size):
                            pass
                        break
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= _SHELL_MAX_OUTPUT_BYTES:
                        # One-byte lookahead: if EOF the output was exactly at
                        # the cap (not truncated); if a byte is returned we are
                        # over the cap and the byte is correctly discarded.
                        lookahead = proc.stdout.read(1)
                        if lookahead:
                            truncated = True
                            _kill()
                            while proc.stdout.read(chunk_size):
                                pass
                        break

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            # ------------------------------------------------------------------
            # Wait for reader.  The Timer is the true deadline; cancelling it
            # is only safe when the process is CONFIRMED dead (via _proc_done).
            # If the command closed its stdout early (e.g. exec >/dev/null) the
            # reader exits immediately but the process keeps running — in that
            # case we must NOT cancel the timer; it will fire and kill the
            # process after timeout_seconds.
            # ------------------------------------------------------------------
            reader_thread.join(timeout=timeout_seconds + 10)

            if _proc_done.is_set():
                # Process exited naturally before the deadline.
                timer.cancel()
            # else: timer will fire (or already fired) to kill the process.

            if reader_thread.is_alive():
                # Reader still blocked — process must still be running.
                # The timer should fire soon; kill defensively now too.
                timed_out = True
                _kill()
                reader_thread.join(timeout=5)

            # Ensure the killer thread also finishes before reading returncode.
            killer_thread.join(timeout=5)
            proc.wait()  # idempotent; ensures returncode is set
            returncode = proc.returncode if proc.returncode is not None else -1

        if timed_out:
            return {
                "success": False,
                "exit_code": -1,
                "truncated": False,
                "output": f"[timed out after {timeout_seconds}s]",
            }

        raw = b"".join(chunks).decode(errors="replace")
        output = redact(raw)
        return {
            "success": returncode == 0,
            "exit_code": returncode,
            "truncated": truncated,
            "output": output,
        }

    # ------------------------------------------------------------------
    # Unified entry point with audit logging
    # ------------------------------------------------------------------

    async def execute(self, operation: str, params: dict) -> OperationResult:
        t0 = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        data: dict[str, Any] = {}
        error: str | None = None
        success = False
        try:
            data = await self._dispatch(operation, params)
            success = data.get("success", True) is not False
        except (ValueError, KeyError) as exc:
            error = str(exc)
            success = False
        except Exception as exc:
            _logger.exception("OperationsExecutor.execute failed: %s", operation)
            error = type(exc).__name__  # no traceback exposed
            success = False
        duration_ms = int((time.monotonic() - t0) * 1000)
        self._log_operation(operation, params, success, data, error, duration_ms)
        return OperationResult(
            success=success,
            operation=operation,
            timestamp=timestamp,
            data=data,
            error=error,
            duration_ms=duration_ms,
            status="ok" if success else "error",
        )

    async def _dispatch(self, operation: str, params: dict) -> dict:
        if operation == "git/pull":
            return await self.git_pull()
        if operation == "git/status":
            return await self.git_status()
        if operation == "git/commit-and-push":
            return await self.git_commit_push(
                params["message"], params.get("files", [])
            )
        if operation == "services/restart":
            return await self.restart_service(params["service"])
        if operation == "services/status":
            return await self.get_service_status()
        if operation == "services/llama/reload-model":
            return await self.reload_llama_model()
        if operation == "logs/tail":
            lines = await self.tail_logs(params["service"], params.get("lines", 50))
            return {"service": params["service"], "lines": lines,
                    "total_available": len(lines)}
        if operation == "logs/errors":
            errors = await self.get_error_logs(
                params["service"], params.get("since_minutes", 60)
            )
            return {"service": params["service"], "error_count": len(errors),
                    "errors": errors}
        if operation == "healing/fix-locks":
            return await self.fix_stale_locks()
        if operation == "healing/cleanup-temps":
            return await self.cleanup_temps()
        if operation == "healing/diagnose":
            problems = await self.detect_problems()
            return {"problems": problems}
        if operation == "models/status":
            return await self.get_model_status()
        if operation == "models/test":
            return await self.test_model(params["model"], params.get("prompt", "ping"))
        if operation == "context/env":
            return await self.get_safe_env()
        if operation == "context/set-config":
            return await self.set_config(params["key"], params["value"])
        if operation == "shell":
            return await self.shell(
                params["command"],
                params.get("timeout_seconds", _SHELL_DEFAULT_TIMEOUT),
            )
        raise ValueError(f"unknown operation: {operation!r}")
