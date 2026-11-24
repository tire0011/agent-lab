"""Non-interactive Mistral Vibe worker: the ``mistral`` coding provider via the
``vibe`` CLI, so runs are billed to the owner's Vibe Code subscription instead
of the separate La Plateforme API workspace (which answered HTTP 402 on
2026-11-20).

Like :mod:`modules.claude_worker` it runs the CLI as a subprocess (never via
``shell=True``) and returns a :class:`~modules.coding_worker.WorkerResult`, so
it is interchangeable with the Codex and Claude workers.

The CLI is started in headless mode:

  --prompt-file <tmp>   the task, read from a temporary file (no argv size limit)
  --output streaming    one JSON journal entry per line (progress for the watchdog)
  --output-dir <tmp>    export.json with outcome, error, usage and cost
  --workdir <dir>       the experiment worktree
  --trust               trust that directory for this invocation only
  --time-limit <s>      the CLI stops itself at the timeout

Sandbox mapping: ``read-only`` enables only the ``read_file`` and ``grep``
tools; ``workspace-write`` and ``danger-full-access`` auto-approve tool calls
(the run happens in a throwaway worktree).

Authentication: Vibe reads ``MISTRAL_API_KEY`` from the environment first and
from ``~/.vibe/.env`` second. The service environment's ``MISTRAL_API_KEY`` is
the API-workspace key, so it is deliberately **not** passed on; the Vibe key
set up once with ``vibe --setup`` (as the service user) is used instead.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.worker_audit import audit_run
from modules.coding_worker import (
    WorkerConfigError,
    WorkerResult,
    filtered_env,
    progress_heartbeat,
    run_watched,
    truncate_output,
    validate_worker_config,
    watchdog_error,
)

#: Same provider name as the API worker: the rotation entry stays ``mistral``.
PROVIDER = "mistral"
#: Provider label used in the ``runs`` audit table (#810): Vibe runs are billed
#: to the Vibe Code subscription, which is a different bucket than the
#: pay-per-token Mistral API worker that shares the rotation name.
AUDIT_PROVIDER = "vibe"
ENV_VIBE_BIN = "AGENT_VIBE_BIN"
ENV_VIBE_MODEL = "AGENT_VIBE_MODEL"

_DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_SECONDS = 1800
_READ_ONLY_TOOLS = ("read_file", "grep")
#: Outcomes in export.json that mean the run finished normally.
_SUCCESS_OUTCOMES = frozenset({"finished", "success", "succeeded", "completed", "complete", "done", "ok"})


#: Where `uv tool install mistral-vibe` puts the CLI for the service user. The
#: services run with the default PATH, which does not include it (2026-11-21:
#: Mistral silently fell back to the API worker).
_SERVICE_USER_VIBE = "/home/agent/.local/bin/vibe"


def default_vibe_bin() -> str:
    """``AGENT_VIBE_BIN`` wins; then ``vibe`` on PATH; then the service user's
    local bin directory (like ``claude_worker._default_claude_bin``)."""
    configured = os.environ.get(ENV_VIBE_BIN)
    if configured:
        return configured
    return shutil.which("vibe") or _SERVICE_USER_VIBE


def vibe_available() -> bool:
    return shutil.which(default_vibe_bin()) is not None


def _final_message(stdout: str) -> str | None:
    """The answer text from ``--output json`` (shape-tolerant)."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        # --output streaming: one JSON entry per line.
        entries = []
        for line in text.splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        if not entries:
            return text
        data = entries
    if isinstance(data, dict):
        for key in ("result", "response", "content", "message", "output", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        messages = data.get("messages")
        if isinstance(messages, list):
            data = messages
    if isinstance(data, list):
        # vibe 3.x: a list of journal entries; the answer is the last assistant
        # message, whose content is a list of {"type": "text", "text": ...} parts.
        for item in reversed(data):
            if not isinstance(item, dict) or item.get("role") != "assistant":
                continue
            value = item.get("content")
            if isinstance(value, list):
                value = "".join(
                    part.get("text", "") for part in value
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            if isinstance(value, str) and value.strip():
                return value
    return text


def _read_export(output_dir: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(output_dir, "export.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _usage(export: dict[str, Any]) -> dict[str, int] | None:
    usage = export.get("usage")
    if not isinstance(usage, dict):
        return None
    result = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result or None


def _cost_usd(export: dict[str, Any]) -> float | None:
    """Return the reported USD amount when valid."""
    value = export.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


class VibeWorker:
    """Runs a coding task non-interactively via the ``vibe`` CLI."""

    provider = PROVIDER

    #: Vibe home/config only; MISTRAL_API_KEY is excluded on purpose (see module doc).
    _EXTRA_ENV_KEYS = frozenset({"VIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"})

    def __init__(
        self,
        vibe_bin: str | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        db_path: str | None = None,
        model: str | None = None,
        **_ignored: Any,
    ) -> None:
        self.vibe_bin = vibe_bin or default_vibe_bin()
        self.max_output_bytes = max_output_bytes
        self._db_path = db_path
        self.model = model or os.environ.get(ENV_VIBE_MODEL) or None

    def run(
        self,
        task: str,
        working_dir: str,
        sandbox: str = "read-only",
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        output_schema: dict | None = None,
        skip_git_repo_check: bool = False,
        experiment_logger: Any | None = None,
        phase: str = "vibe",
        stall_seconds: float | None = None,
        on_process: Any | None = None,
        extra_args: list[str] | None = None,
        on_live_usage: Any | None = None,
    ) -> WorkerResult:
        """Same signature as ``ClaudeWorker.run``; ``output_schema`` is added to
        the prompt as an instruction (the CLI has no schema flag)."""
        with audit_run(self._db_path, AUDIT_PROVIDER, phase, task) as audit:
            try:
                validate_worker_config(task, working_dir, sandbox)
            except WorkerConfigError as exc:
                result = self._result(task, working_dir, sandbox, error=str(exc))
                # A rejected call never ran, but it is still a failed run and is
                # recorded as one (#810: failed runs are audited too).
                audit.worker_result(result)
                return result

            prompt = task
            if output_schema is not None:
                prompt += (
                    "\n\nAnswer with exactly one JSON object matching this JSON Schema, "
                    "and nothing else:\n" + json.dumps(output_schema)
                )
            with tempfile.TemporaryDirectory(prefix="nexus-vibe-") as scratch:
                prompt_file = Path(scratch, "prompt.md")
                prompt_file.write_text(prompt, encoding="utf-8")
                output_dir = str(Path(scratch, "out"))
                cmd = self._build_command(str(prompt_file), output_dir, working_dir, sandbox, timeout_seconds)
                cmd += list(extra_args or [])
                if experiment_logger is not None:
                    experiment_logger.debug(
                        phase, "vibe_call_start",
                        f"Starting Vibe call (sandbox={sandbox}, timeout={timeout_seconds}s)",
                        command=cmd, cwd=working_dir, sandbox=sandbox, timeout_seconds=timeout_seconds,
                    )
                result = self._execute(
                    cmd, task, working_dir, sandbox, timeout_seconds, output_dir,
                    stall_seconds, on_process,
                    progress_heartbeat(experiment_logger, phase, "Vibe"),
                )
                audit.worker_result(result)
            if experiment_logger is not None:
                level_event = "vibe_call_completed" if result.success else "vibe_call_failed"
                experiment_logger.log_process(
                    phase, level_event,
                    f"Vibe call {'completed' if result.success else 'failed'}"
                    + (f": {result.error}" if result.error else ""),
                    command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                    exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                    timed_out=result.timed_out, level="INFO" if result.success else "ERROR",
                )
            return result

    def _build_command(
        self, prompt_file: str, output_dir: str, working_dir: str, sandbox: str, timeout_seconds: int
    ) -> list[str]:
        cmd = [
            self.vibe_bin,
            "--prompt-file", prompt_file,
            # One journal entry per line while the run works: the stall watchdog
            # counts lines as progress (with "json" Vibe printed only at the end,
            # and a long run was killed as stalled after 30 min, 2026-11-21).
            "--output", "streaming",
            "--output-dir", output_dir,
            "--workdir", working_dir,
            "--trust",
            "--time-limit", str(max(1, int(timeout_seconds))),
        ]
        if sandbox == "read-only":
            for tool in _READ_ONLY_TOOLS:
                cmd += ["--enabled-tools", tool]
        else:
            cmd += ["--auto-approve"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    @classmethod
    def _safe_env(cls) -> dict[str, str]:
        env = filtered_env(cls._EXTRA_ENV_KEYS)
        env.pop("MISTRAL_API_KEY", None)
        return env

    def _execute(
        self,
        cmd: list[str],
        task: str,
        working_dir: str,
        sandbox: str,
        timeout_seconds: int,
        output_dir: str,
        stall_seconds: float | None,
        on_process: Any | None,
        on_heartbeat: Any | None,
    ) -> WorkerResult:
        started = time.perf_counter()
        stdout_raw = stderr_raw = ""
        exit_code: int | None = None
        timed_out = False
        error: str | None = None
        try:
            if stall_seconds is not None:
                watched = run_watched(
                    cmd, cwd=working_dir, env=self._safe_env(), input_text="",
                    stall_seconds=stall_seconds, max_seconds=timeout_seconds + 60,
                    on_process=on_process, on_heartbeat=on_heartbeat,
                )
                stdout_raw, stderr_raw, exit_code = watched.stdout, watched.stderr, watched.exit_code
                if watched.stop_reason:
                    timed_out = True
                    error = watchdog_error("Vibe", watched, stall_seconds, timeout_seconds)
            else:
                proc = subprocess.run(
                    cmd, input="", capture_output=True, text=True,
                    timeout=timeout_seconds + 60, cwd=working_dir, env=self._safe_env(),
                )
                stdout_raw, stderr_raw, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"Vibe process timed out after {timeout_seconds}s"
        except FileNotFoundError:
            error = f"Vibe binary not found: {cmd[0]!r}"
        except PermissionError:
            error = f"Vibe binary not executable: {cmd[0]!r}"
        except OSError as exc:
            error = f"Failed to start Vibe: {exc}"
        runtime_ms = int(round((time.perf_counter() - started) * 1000))

        export = _read_export(output_dir)
        outcome = str(export.get("outcome") or "")
        if error is None:
            export_error = export.get("error")
            if isinstance(export_error, dict) and export_error.get("message"):
                error = f"Vibe {outcome or 'error'}: {export_error['message']}"
            elif exit_code == 3:
                timed_out = True
                error = f"Vibe stopped at its time limit ({timeout_seconds}s)"
            elif exit_code not in (0, None):
                error = f"Vibe exited with code {exit_code}" + (
                    f": {stderr_raw.strip().splitlines()[-1]}" if stderr_raw.strip() else ""
                )
            elif outcome and outcome.lower() not in _SUCCESS_OUTCOMES:
                error = f"Vibe ended with outcome {outcome!r}"

        cost = _cost_usd(export)
        steps = export.get("steps")
        made_calls = type(steps) is int and steps > 0
        override = os.environ.get("AGENT_VIBE_SUBSCRIPTION", "").strip().lower()
        if cost == 0 and error and not made_calls:
            cost = None

        return self._result(
            task, working_dir, sandbox, error=error, exit_code=exit_code,
            stdout=stdout_raw, stderr=stderr_raw, runtime_ms=runtime_ms, timed_out=timed_out,
            thread_id=str(export["session_id"]) if export.get("session_id") else None,
            final_message=_final_message(stdout_raw) if error is None else None,
            usage=_usage(export),
            cost_usd=cost,
            subscription=made_calls and override in {"1", "true", "yes", "on"},
        )

    def _result(
        self, task: str, working_dir: str, sandbox: str, *, error: str | None,
        exit_code: int | None = None, stdout: str = "", stderr: str = "",
        runtime_ms: int = 0, timed_out: bool = False, thread_id: str | None = None,
        final_message: str | None = None, usage: dict[str, int] | None = None,
        cost_usd: float | None = None, subscription: bool = False,
    ) -> WorkerResult:
        stdout_capped, stdout_truncated = truncate_output(stdout, self.max_output_bytes)
        stderr_capped, stderr_truncated = truncate_output(stderr, self.max_output_bytes)
        return WorkerResult(
            success=error is None and exit_code == 0,
            exit_code=exit_code,
            stdout=stdout_capped,
            stderr=stderr_capped,
            runtime_ms=runtime_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            thread_id=thread_id,
            final_message=final_message,
            usage=usage,
            task=task,
            working_dir=working_dir,
            sandbox=sandbox,
            error=error,
            provider=PROVIDER,
            cost_usd=cost_usd,
            subscription=subscription,
            billing_model=self.model,
        )
