"""Non-interactive Claude Code worker for controlled coding tasks.

Executes the ``claude`` CLI in print mode as a subprocess (never via
``shell=True``) and returns the same :class:`~modules.coding_worker.WorkerResult`
as ``modules.codex_worker.CodexWorker``, so both agents are interchangeable for
``CodexTaskService`` and for the implementation phase of ``ExperimentService``.

The process is always started with:

  --print                    non-interactive; print the result and exit
  --output-format json       one structured JSON result object on stdout
  --no-session-persistence   no session files left behind (Codex: --ephemeral)
  --permission-prompts none  nothing may wait for a human: anything that would
                             prompt is denied automatically

The task itself is written to the child's **stdin**, not passed as an argv
element: prompts here routinely exceed a comfortable argument size, and stdin
cannot be mistaken for a flag.

Capability per sandbox mode is enforced by the *tool set*, not by prompts,
because a non-interactive run has nobody to answer a prompt:

  read-only           only ``Read``/``Glob``/``Grep`` exist, so there is no
                      tool that could modify the working directory
  workspace-write     read, edit, write and shell tools are pre-approved; the
                      file tools stay inside the working directory (the
                      experiment worktree), and anything reaching outside it
                      would prompt and is therefore denied
  danger-full-access  only with the explicit bypass flag; never a default

Security constraints enforced here:
  - Working directory must be absolute and exist.
  - No shell=True, no shell-composed command strings.
  - Outputs are capped at max_output_bytes; truncation is flagged in result.
  - Only the variables Claude needs are passed on; AGENT_API_KEY and other
    application secrets are never exposed to the subprocess.

Requires a ``claude`` CLI that understands ``--permission-prompts`` and
``--tools`` (Claude Code 2.x). ``python3 -m modules.claude_worker --help``
runs a one-off task and prints the parsed result, which is the quickest way
to verify the installed CLI on the VM.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

from modules.coding_worker import (
    SANDBOX_MODES,
    WorkerConfigError,
    WorkerResult,
    filtered_env,
    progress_heartbeat,
    run_watched,
    truncate_output,
    validate_worker_config,
    watchdog_error,
)

#: This worker's provider name as recorded on every :class:`WorkerResult`.
PROVIDER = "claude"


def _default_claude_bin() -> str:
    """Path to the ``claude`` executable.

    ``AGENT_CLAUDE_BIN`` wins; otherwise the CLI is looked up on ``PATH``
    (it is installed per-user on the VM, so there is no single fixed
    location), falling back to the agent user's local bin directory.
    """
    configured = os.environ.get("AGENT_CLAUDE_BIN")
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    return "/home/agent/.local/bin/claude"


_DEFAULT_MAX_OUTPUT_BYTES: int = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT_SECONDS: int = 120
_DEFAULT_SANDBOX: str = "read-only"

# Tool sets per sandbox mode. These are Claude Code's built-in tool names;
# a tool that is not listed does not exist for that run at all.
_READ_ONLY_TOOLS = "Read,Glob,Grep"
_WORKSPACE_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash,TodoWrite"


def _parse_result(stdout: str) -> dict[str, Any] | None:
    """Return the CLI's final ``result`` object, or ``None`` if absent.

    Accepts both output shapes: ``--output-format json`` (a single object)
    and ``--output-format stream-json`` (one JSON object per line, of which
    the last ``type == "result"`` event is the final one). Unparseable lines
    are ignored, exactly like the Codex event-stream parser.
    """
    text = stdout.strip()
    if not text:
        return None

    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict) and whole.get("type") == "result":
        return whole

    final: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            final = event
    return final


def _usage_from_result(result: dict[str, Any]) -> dict[str, int] | None:
    """Flatten Claude's usage block to the ``{name: int}`` shape.

    Only scalar token counters are kept (``input_tokens``,
    ``output_tokens``, the cache counters); nested breakdowns and
    non-numeric entries are dropped, matching the Codex worker's usage
    shape so cost/usage reporting stays provider-independent.
    """
    raw = result.get("usage")
    if not isinstance(raw, dict):
        return None
    usage = {
        key: int(value)
        for key, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return usage or None


def _model_from_result(result: dict[str, Any]) -> str:
    """Best-effort model name for the audit entry."""
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        return str(next(iter(model_usage)))
    return "claude"


class ClaudeWorker:
    """Runs a coding task non-interactively via the ``claude`` CLI.

    Parameters
    ----------
    claude_bin:
        Path to the ``claude`` executable. Defaults to ``AGENT_CLAUDE_BIN``,
        then to whatever ``claude`` resolves to on ``PATH``.
    max_output_bytes:
        Maximum bytes kept from stdout/stderr individually.
        Output beyond this is truncated and flagged in the result.
    db_path:
        Optional path to the SQLite run store for audit entries.
        Pass ``None`` to skip audit logging.
    model:
        Optional model override (``AGENT_CLAUDE_MODEL`` by default). When
        unset, the CLI's own configured model is used.
    """

    #: Provider name recorded on every result this worker produces.
    provider = PROVIDER

    #: Claude-specific variables added to the shared base passthrough set.
    _EXTRA_ENV_KEYS = frozenset(
        {
            # Claude Code auth / configuration
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
        }
    )

    def __init__(
        self,
        claude_bin: str | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        db_path: str | None = None,
        model: str | None = None,
    ) -> None:
        self.claude_bin = claude_bin or _default_claude_bin()
        self.max_output_bytes = max_output_bytes
        self._db_path = db_path
        self.model = model or os.environ.get("AGENT_CLAUDE_MODEL") or None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        task: str,
        working_dir: str,
        sandbox: str = _DEFAULT_SANDBOX,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        output_schema: dict | None = None,
        skip_git_repo_check: bool = False,
        experiment_logger: Any | None = None,
        phase: str = "claude",
        stall_seconds: float | None = None,
        on_process: Any | None = None,
        extra_args: list[str] | None = None,
    ) -> WorkerResult:
        """Execute *task* non-interactively via the ``claude`` CLI.

        The signature mirrors ``CodexWorker.run`` so the two workers can be
        swapped without the caller knowing which agent it got.

        Parameters
        ----------
        task:
            Natural-language coding task. Must be non-empty. Passed to the
            CLI on stdin.
        working_dir:
            Absolute path to the working directory. Must exist. It is the
            child's ``cwd`` and, for file tools, its confinement boundary.
        sandbox:
            One of ``read-only``, ``workspace-write``, ``danger-full-access``.
            Defaults to ``read-only``. See the module docstring for how each
            mode is enforced.
        timeout_seconds:
            Hard wall-clock timeout. The process is killed on expiry and the
            result carries ``timed_out=True``.
        output_schema:
            Optional JSON Schema (as a plain dict) describing the shape of
            the model's final response, passed via ``--json-schema``. Unlike
            Codex, the CLI takes the schema inline, so no temporary file is
            created. Defaults to ``None`` (free-form response).
        skip_git_repo_check:
            Accepted for signature compatibility with ``CodexWorker`` and
            ignored: the Claude CLI does not require the working directory
            to be a git repository, so there is no check to skip.
        experiment_logger:
            Optional ``modules.central_logging.ExperimentLogger`` (duck typed
            to avoid a hard import) used to record this call's start/outcome
            in the owning experiment's structured JSONL log.
        phase:
            Label used in log entries when *experiment_logger* is given
            (e.g. ``"implementation"``, ``"research"``).
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            validate_worker_config(task, working_dir, sandbox)
        except WorkerConfigError as exc:
            if experiment_logger is not None:
                experiment_logger.error(
                    phase, "claude_call_invalid_config",
                    f"Claude call rejected before starting: {exc}",
                )
            return self._failed_result(
                task=task,
                working_dir=working_dir,
                sandbox=sandbox,
                error=str(exc),
            )

        # A progress-watched run (#327) streams events so every step shows up.
        cmd = self._build_command(sandbox, output_schema, stream=stall_seconds is not None)
        # E.g. an MCP server and its pre-approved read tools for the planner (#384).
        cmd += list(extra_args or [])

        if experiment_logger is not None:
            experiment_logger.debug(
                phase, "claude_call_start",
                f"Starting Claude call (sandbox={sandbox}, timeout={timeout_seconds}s)",
                command=cmd, cwd=working_dir, sandbox=sandbox,
                timeout_seconds=timeout_seconds,
            )

        result = self._execute(
            cmd, task, working_dir, timeout_seconds, sandbox, stall_seconds, on_process,
            on_heartbeat=progress_heartbeat(experiment_logger, phase, "Claude"),
        )

        if experiment_logger is not None:
            self._log_result(experiment_logger, phase, cmd, working_dir, result)

        self._audit(result, timestamp)
        return result

    @staticmethod
    def _failed_result(
        *, task: str, working_dir: str, sandbox: str, error: str,
        runtime_ms: int = 0, exit_code: int | None = None,
        stdout: str = "", stderr: str = "",
        stdout_truncated: bool = False, stderr_truncated: bool = False,
        timed_out: bool = False,
    ) -> WorkerResult:
        return WorkerResult(
            success=False,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            runtime_ms=runtime_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            thread_id=None,
            final_message=None,
            usage=None,
            task=task,
            working_dir=working_dir,
            sandbox=sandbox,
            error=error,
            provider=PROVIDER,
        )

    @staticmethod
    def _log_result(
        experiment_logger: Any,
        phase: str,
        cmd: list[str],
        working_dir: str,
        result: WorkerResult,
    ) -> None:
        if result.timed_out:
            experiment_logger.log_process(
                phase, "claude_call_timeout",
                f"Claude call timed out after {result.runtime_ms}ms",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                timed_out=True, level="ERROR",
            )
        elif result.error:
            experiment_logger.log_process(
                phase, "claude_call_failed",
                f"Claude call failed: {result.error}",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                level="ERROR",
            )
        else:
            experiment_logger.log_process(
                phase, "claude_call_completed",
                f"Claude call completed (success={result.success})",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                level="INFO" if result.success else "WARNING",
            )
            if result.success:
                experiment_logger.debug(
                    phase, "claude_response_validated",
                    "Claude response received and parsed as a JSON result object",
                    thread_id=result.thread_id or "",
                )

    # ------------------------------------------------------------------
    # Command construction (no shell=True, no string composition)
    # ------------------------------------------------------------------

    def _build_command(
        self,
        sandbox: str,
        output_schema: dict | None = None,
        stream: bool = False,
    ) -> list[str]:
        cmd = [
            self.claude_bin,
            "--print",
            # stream-json needs --verbose in --print mode.
            *(["--output-format", "stream-json", "--verbose"] if stream else ["--output-format", "json"]),
            "--no-session-persistence",
            # Nothing may block on a human: whatever is not pre-approved below
            # is denied instead of turning into an interactive prompt.
            "--permission-prompts", "none",
        ]

        if sandbox == "read-only":
            # No tool that could write or execute anything exists for this run.
            cmd += ["--tools", _READ_ONLY_TOOLS]
        elif sandbox == "workspace-write":
            # Edits and commands are pre-approved (the run happens in a
            # throwaway worktree); file tools stay inside the working
            # directory, and a path outside it would prompt and be denied.
            cmd += [
                "--permission-mode", "acceptEdits",
                "--tools", _WORKSPACE_WRITE_TOOLS,
                "--allowedTools", _WORKSPACE_WRITE_TOOLS,
            ]
        else:  # danger-full-access
            cmd += ["--dangerously-skip-permissions"]

        if self.model:
            cmd += ["--model", self.model]
        if output_schema is not None:
            cmd += ["--json-schema", json.dumps(output_schema)]
        return cmd

    # ------------------------------------------------------------------
    # Process execution
    # ------------------------------------------------------------------

    def _execute(
        self,
        cmd: list[str],
        task: str,
        working_dir: str,
        timeout_seconds: int,
        sandbox: str,
        stall_seconds: float | None = None,
        on_process: Any | None = None,
        on_heartbeat: Any | None = None,
    ) -> WorkerResult:
        timed_out = False
        proc_error: str | None = None
        stdout_raw = ""
        stderr_raw = ""
        exit_code: int | None = None
        started = time.perf_counter()

        try:
            if stall_seconds is not None:
                watched = run_watched(
                    cmd, cwd=working_dir, env=self._safe_env(), input_text=task,
                    stall_seconds=stall_seconds, max_seconds=timeout_seconds,
                    on_process=on_process,
                    on_heartbeat=on_heartbeat,
                )
                stdout_raw, stderr_raw = watched.stdout, watched.stderr
                if watched.stop_reason:
                    timed_out = True
                    proc_error = watchdog_error("Claude", watched, stall_seconds, timeout_seconds)
                exit_code = watched.exit_code
            else:
                proc = subprocess.run(
                    cmd,
                    input=task,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    cwd=working_dir,
                    # Intentionally inherit only a minimal env; never pass full
                    # os.environ to avoid leaking secrets.
                    env=self._safe_env(),
                )
                exit_code = proc.returncode
                stdout_raw = proc.stdout
                stderr_raw = proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_raw = _decoded(exc.stdout)
            stderr_raw = _decoded(exc.stderr)
            proc_error = f"Claude process timed out after {timeout_seconds}s"
        except FileNotFoundError:
            proc_error = f"Claude binary not found: {cmd[0]!r}"
        except PermissionError:
            proc_error = f"Claude binary not executable: {cmd[0]!r}"
        except OSError as exc:
            proc_error = f"Failed to start Claude: {exc}"

        runtime_ms = int(round((time.perf_counter() - started) * 1000))

        stdout_capped, stdout_truncated = truncate_output(stdout_raw, self.max_output_bytes)
        stderr_capped, stderr_truncated = truncate_output(stderr_raw, self.max_output_bytes)

        if proc_error:
            return self._failed_result(
                task=task,
                working_dir=working_dir,
                sandbox=sandbox,
                error=proc_error,
                runtime_ms=runtime_ms,
                exit_code=exit_code,
                stdout=stdout_capped,
                stderr=stderr_capped,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                timed_out=timed_out,
            )

        parsed = _parse_result(stdout_raw)
        thread_id: str | None = None
        final_message: str | None = None
        usage: dict[str, int] | None = None
        error: str | None = None

        if parsed is None:
            # Exit code alone cannot be trusted as success when the agent
            # produced nothing parseable; report it as a worker-level error.
            error = "Claude produced no parseable result object"
        else:
            session_id = parsed.get("session_id")
            thread_id = str(session_id) if session_id else None
            message = parsed.get("result")
            final_message = message if isinstance(message, str) and message else None
            usage = _usage_from_result(parsed)
            if parsed.get("is_error"):
                error = final_message or f"Claude reported {parsed.get('subtype', 'an error')}"

        success = exit_code == 0 and error is None

        return WorkerResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout_capped,
            stderr=stderr_capped,
            runtime_ms=runtime_ms,
            timed_out=False,
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
        )

    @classmethod
    def _safe_env(cls) -> dict[str, str]:
        """Minimal env passed to the claude subprocess.

        Passes only variables the CLI requires to function. Never exposes
        AGENT_API_KEY or other application secrets.
        """
        return filtered_env(cls._EXTRA_ENV_KEYS)

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _audit(self, result: WorkerResult, timestamp: str) -> None:
        if self._db_path is None:
            return
        try:
            from modules.run_store import init_db, insert_run

            parsed = _parse_result(result.stdout) or {}
            cost = parsed.get("total_cost_usd")
            actual_cost = float(cost) if isinstance(cost, (int, float)) else None
            usage = result.usage or {}
            conn = init_db(self._db_path)
            insert_run(
                conn,
                timestamp=timestamp,
                provider=PROVIDER,
                model=_model_from_result(parsed),
                input_description=result.task[:500],
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                runtime_ms=result.runtime_ms,
                estimated_cost=None,
                actual_cost=actual_cost,
                cost_status="actual" if actual_cost is not None else "unknown",
                success=result.success,
                error=result.error,
            )
            conn.close()
        except Exception:
            # Audit failure must never surface to the caller.
            pass


def _decoded(stream: bytes | str | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream or ""


# ---------------------------------------------------------------------------
# CLI entry point for manual smoke-testing
# ---------------------------------------------------------------------------

def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m modules.claude_worker",
        description="Run a Claude Code task non-interactively and print the result.",
    )
    parser.add_argument("task", help="Natural-language coding task")
    parser.add_argument(
        "--working-dir", "-C",
        default="/agent",
        help="Absolute working directory (default: /agent)",
    )
    parser.add_argument(
        "--sandbox", "-s",
        default="read-only",
        choices=list(SANDBOX_MODES),
        help="Sandbox mode (default: read-only)",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Timeout in seconds (default: {_DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model override (default: AGENT_CLAUDE_MODEL or the CLI's own default)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite db path for audit logging (default: no logging)",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Print result as JSON instead of human-readable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from modules.logging_config import configure_logging

    configure_logging()
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    worker = ClaudeWorker(db_path=args.db, model=args.model)
    result = worker.run(
        task=args.task,
        working_dir=args.working_dir,
        sandbox=args.sandbox,
        timeout_seconds=args.timeout,
    )

    if args.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.success else 1

    print(f"success:     {result.success}")
    print(f"exit_code:   {result.exit_code}")
    print(f"runtime_ms:  {result.runtime_ms}")
    print(f"timed_out:   {result.timed_out}")
    print(f"session_id:  {result.thread_id}")
    print(f"sandbox:     {result.sandbox}")
    if result.usage:
        print(f"usage:       {result.usage}")
    if result.error:
        print(f"error:       {result.error}", file=sys.stderr)
    if result.final_message:
        print(f"\n--- final message ---\n{result.final_message}")
    if result.stderr.strip():
        print(f"\n--- stderr ---\n{result.stderr.strip()}", file=sys.stderr)

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
