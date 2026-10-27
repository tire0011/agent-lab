"""Non-interactive Codex worker for controlled coding tasks.

Executes ``codex exec`` as a subprocess (never via shell=True) and returns
a structured result. The process is always started with:

  --ephemeral          no session files persisted
  --json               JSONL event stream on stdout
  --approve-for-me     route approvals through automatic review (avoids
                       interactive waiting; combined with read-only sandbox
                       this means write operations are blocked outright)
  --sandbox <mode>     default read-only; never accepts free-form shell cmds

Security constraints enforced here:
  - Working directory must be absolute and exist.
  - No shell=True, no shell-composed command strings.
  - Outputs are capped at max_output_bytes; truncation is flagged in result.
  - No secrets or full env vars are logged.

Usage (CLI smoke-test):
  python3 -m modules.codex_worker --working-dir /agent \
      "Inspect README.md and summarize the project. Do not change files."
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from modules.coding_worker import (
    SANDBOX_MODES,
    WorkerConfigError,
    WorkerResult,
    filtered_env,
    run_watched,
    truncate_output,
    validate_worker_config,
    watchdog_error,
)

#: This worker's provider name as recorded on every :class:`WorkerResult`.
PROVIDER = "codex"

_DEFAULT_CODEX_BIN: str = os.environ.get(
    "AGENT_CODEX_BIN", "/home/agent/.local/bin/codex"
)
_DEFAULT_MAX_OUTPUT_BYTES: int = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT_SECONDS: int = 120
_DEFAULT_SANDBOX: str = "read-only"


# The result type is shared with every other coding-agent worker (see
# ``modules.coding_worker.WorkerResult``) so a Codex and a Claude run are
# interchangeable for their callers. ``CodexResult`` stays importable under
# its original name.
CodexResult = WorkerResult

# Historical private alias kept for callers/tests importing it from here.
_truncate = truncate_output


def _parse_jsonl(stdout: str) -> tuple[str | None, str | None, dict[str, int] | None]:
    """Parse Codex JSONL event stream.

    Returns (thread_id, final_message, usage).
    final_message is the last agent_message text found.
    usage is from the last turn.completed event.
    """
    thread_id: str | None = None
    final_message: str | None = None
    usage: dict[str, int] | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")

        if etype == "thread.started":
            thread_id = event.get("thread_id")

        elif etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    final_message = text

        elif etype == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = {k: int(v) for k, v in raw_usage.items() if isinstance(v, (int, float))}

    return thread_id, final_message, usage


class CodexWorker:
    """Runs a coding task non-interactively via ``codex exec``.

    Parameters
    ----------
    codex_bin:
        Path to the ``codex`` executable.
        Defaults to ``AGENT_CODEX_BIN`` env var or ``/home/agent/.local/bin/codex``.
    max_output_bytes:
        Maximum bytes kept from stdout/stderr individually.
        Output beyond this is truncated and flagged in the result.
    db_path:
        Optional path to the SQLite run store for audit entries.
        Pass ``None`` to skip audit logging.
    """

    #: Provider name recorded on every result this worker produces.
    provider = PROVIDER

    def __init__(
        self,
        codex_bin: str = _DEFAULT_CODEX_BIN,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        db_path: str | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.max_output_bytes = max_output_bytes
        self._db_path = db_path

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
        phase: str = "codex",
        stall_seconds: float | None = None,
        on_process: Any | None = None,
    ) -> CodexResult:
        """Execute *task* non-interactively via ``codex exec``.

        Parameters
        ----------
        task:
            Natural-language coding task. Must be non-empty.
        working_dir:
            Absolute path to the working directory. Must exist.
        sandbox:
            One of ``read-only``, ``workspace-write``, ``danger-full-access``.
            Defaults to ``read-only``.
        timeout_seconds:
            Hard wall-clock timeout. The process is killed on expiry and the
            result carries ``timed_out=True``.
        output_schema:
            Optional JSON Schema (as a plain dict) describing the shape of
            the model's final response. When provided, it is written to a
            secure temporary file and passed to Codex via
            ``--output-schema <FILE>``; the temp file is always removed
            afterwards (success, timeout, or exception). Defaults to
            ``None``, which preserves the exact previous command-line
            behavior for all existing callers (including the public
            ``/codex/tasks`` API).
        skip_git_repo_check:
            Passes ``--skip-git-repo-check`` to ``codex exec``. Codex
            normally refuses to run outside a trusted git repository; this
            is required for working directories that are intentionally
            NOT a git repository (e.g. an isolated, empty scratch
            directory used to guarantee no further repository research is
            possible). Defaults to ``False`` (preserves prior behavior for
            all existing callers, all of which operate inside the real
            repository or a git worktree).
        experiment_logger:
            Optional ``modules.central_logging.ExperimentLogger`` (duck
            typed here to avoid a hard import) used to record this call's
            start/outcome into the owning experiment's structured JSONL
            log. Defaults to ``None`` (no experiment-log entries; standalone
            callers such as the public ``/codex/tasks`` API are unaffected).
        phase:
            Label used in log entries when *experiment_logger* is given
            (e.g. ``"research"``, ``"selection"``, ``"implementation"``).
        stall_seconds:
            When set, the run is progress-watched (#327): it is stopped only
            after this long without any output, and *timeout_seconds* becomes
            the absolute cap instead of the normal limit.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            self._validate(task, working_dir, sandbox)
        except WorkerConfigError as exc:
            if experiment_logger is not None:
                experiment_logger.error(
                    phase, "codex_call_invalid_config",
                    f"Codex call rejected before starting: {exc}",
                )
            return CodexResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                runtime_ms=0,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
                thread_id=None,
                final_message=None,
                usage=None,
                task=task,
                working_dir=working_dir,
                sandbox=sandbox,
                error=str(exc),
                provider=PROVIDER,
            )

        schema_path: str | None = None
        try:
            if output_schema is not None:
                fd, schema_path = tempfile.mkstemp(prefix="codex_schema_", suffix=".json")
                with os.fdopen(fd, "w") as f:
                    json.dump(output_schema, f)

            cmd = self._build_command(task, working_dir, sandbox, schema_path, skip_git_repo_check)

            if experiment_logger is not None:
                experiment_logger.debug(
                    phase, "codex_call_start",
                    f"Starting Codex call (sandbox={sandbox}, timeout={timeout_seconds}s)",
                    command=cmd, cwd=working_dir, sandbox=sandbox,
                    timeout_seconds=timeout_seconds,
                )

            result = self._execute(cmd, working_dir, timeout_seconds, task, sandbox, stall_seconds, on_process)
        finally:
            if schema_path is not None:
                try:
                    os.remove(schema_path)
                except OSError:
                    pass

        if experiment_logger is not None:
            self._log_result(experiment_logger, phase, cmd, working_dir, result)

        self._audit(result, timestamp)
        return result

    @staticmethod
    def _log_result(
        experiment_logger: Any,
        phase: str,
        cmd: list[str],
        working_dir: str,
        result: "CodexResult",
    ) -> None:
        if result.timed_out:
            experiment_logger.log_process(
                phase, "codex_call_timeout",
                f"Codex call timed out after {result.runtime_ms}ms",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                timed_out=True, level="ERROR",
            )
        elif result.error:
            experiment_logger.log_process(
                phase, "codex_call_failed",
                f"Codex call failed: {result.error}",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                level="ERROR",
            )
        else:
            experiment_logger.log_process(
                phase, "codex_call_completed",
                f"Codex call completed (success={result.success})",
                command=cmd, cwd=working_dir, duration_ms=result.runtime_ms,
                exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
                level="INFO" if result.success else "WARNING",
            )
            if result.success:
                experiment_logger.debug(
                    phase, "codex_response_validated",
                    "Codex response received and parsed as JSONL event stream",
                    thread_id=result.thread_id or "",
                )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, task: str, working_dir: str, sandbox: str) -> None:
        validate_worker_config(task, working_dir, sandbox)

    # ------------------------------------------------------------------
    # Command construction (no shell=True, no string composition)
    # ------------------------------------------------------------------

    def _build_command(
        self,
        task: str,
        working_dir: str,
        sandbox: str,
        schema_path: str | None = None,
        skip_git_repo_check: bool = False,
    ) -> list[str]:
        cmd = [
            self.codex_bin,
            "exec",
            "--ephemeral",
            "--json",
            "-C", working_dir,
        ]
        if skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        # --approve-for-me is only compatible with workspace-write, and the
        # installed Codex CLI rejects `--sandbox` when `--approve-for-me` is
        # also given ("cannot be used with '--approve-for-me'") because
        # --approve-for-me already routes approvals through the
        # workspace-write sandbox. So for workspace-write we pass only
        # --approve-for-me (never --sandbox); for every other mode we pass
        # --sandbox and never --approve-for-me.
        # In read-only mode, writes are blocked outright (no approval dialog).
        # In danger-full-access mode, use --dangerously-bypass-approvals-and-sandbox
        # only when explicitly requested; we do not set it by default.
        if sandbox == "workspace-write":
            cmd.append("--approve-for-me")
        else:
            cmd += ["--sandbox", sandbox]
        if schema_path is not None:
            cmd += ["--output-schema", schema_path]
        cmd += ["--", task]
        return cmd

    # ------------------------------------------------------------------
    # Process execution
    # ------------------------------------------------------------------

    def _execute(
        self,
        cmd: list[str],
        working_dir: str,
        timeout_seconds: int,
        task: str,
        sandbox: str,
        stall_seconds: float | None = None,
        on_process: Any | None = None,
    ) -> CodexResult:
        timed_out = False
        proc_error: str | None = None
        stdout_raw = ""
        stderr_raw = ""
        exit_code: int | None = None
        started = time.perf_counter()

        try:
            if stall_seconds is not None:
                watched = run_watched(
                    cmd, cwd=working_dir, env=self._safe_env(),
                    stall_seconds=stall_seconds, max_seconds=timeout_seconds,
                    on_process=on_process,
                )
                stdout_raw, stderr_raw = watched.stdout, watched.stderr
                if watched.stop_reason:
                    timed_out = True
                    proc_error = watchdog_error("Codex", watched, stall_seconds, timeout_seconds)
                exit_code = watched.exit_code
            else:
                proc = subprocess.run(
                    cmd,
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
            stdout_raw = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr_raw = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            proc_error = f"Codex process timed out after {timeout_seconds}s"
        except FileNotFoundError:
            proc_error = f"Codex binary not found: {cmd[0]!r}"
        except PermissionError:
            proc_error = f"Codex binary not executable: {cmd[0]!r}"
        except OSError as exc:
            proc_error = f"Failed to start Codex: {exc}"

        runtime_ms = int(round((time.perf_counter() - started) * 1000))

        stdout_capped, stdout_truncated = _truncate(stdout_raw, self.max_output_bytes)
        stderr_capped, stderr_truncated = _truncate(stderr_raw, self.max_output_bytes)

        if proc_error:
            return CodexResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout_capped,
                stderr=stderr_capped,
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
                error=proc_error,
                provider=PROVIDER,
            )

        thread_id, final_message, usage = _parse_jsonl(stdout_raw)
        success = (exit_code == 0)

        return CodexResult(
            success=success,
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
            error=None,
            provider=PROVIDER,
        )

    #: Codex-specific variables added to the shared base passthrough set.
    _EXTRA_ENV_KEYS = frozenset(
        {
            # Codex auth / config
            "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            # Azure OpenAI (codex may use either)
            "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
        }
    )

    @classmethod
    def _safe_env(cls) -> dict[str, str]:
        """Minimal env passed to the codex subprocess.

        Passes only variables Codex requires to function. Never exposes
        AGENT_API_KEY or other application secrets.
        """
        return filtered_env(cls._EXTRA_ENV_KEYS)

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _audit(self, result: CodexResult, timestamp: str) -> None:
        if self._db_path is None:
            return
        try:
            from modules.run_store import init_db, insert_run
            conn = init_db(self._db_path)
            usage = result.usage or {}
            insert_run(
                conn,
                timestamp=timestamp,
                provider=PROVIDER,
                model="codex",
                input_description=result.task[:500],
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                runtime_ms=result.runtime_ms,
                estimated_cost=None,
                actual_cost=None,
                cost_status="unknown",
                success=result.success,
                error=result.error,
            )
            conn.close()
        except Exception:
            # Audit failure must never surface to the caller.
            pass


# ---------------------------------------------------------------------------
# CLI entry point for manual smoke-testing
# ---------------------------------------------------------------------------

def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m modules.codex_worker",
        description="Run a Codex coding task non-interactively and print the result.",
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

    worker = CodexWorker(db_path=args.db)
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
    print(f"thread_id:   {result.thread_id}")
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
