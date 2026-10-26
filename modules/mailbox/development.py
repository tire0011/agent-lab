"""Development extensions for the Git-backed Nexus mailbox.

The command channel is available for the trusted development mailbox.
Mailbox tasks are immediately approved and handed to the existing task worker,
so the controller can execute them without a manual approval step.
"""

from __future__ import annotations

import logging
import os
import signal
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.redaction import redact

from .command_recovery import (
    evaluate_interrupted_command,
    parse_expected_services,
    write_recovery_state,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 32_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 3_600
MAX_COMMAND_TIMEOUT_SECONDS = 21_600
COMMAND_TIMEOUT_EXIT_CODE = 124
_COMMAND_META_KEYS = {
    "type",
    "created_by",
    "request_id",
    "timeout",
    "recovery_services",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_ACTIVE_COMMANDS: dict[int, subprocess.Popen] = {}
_SENSITIVE_ENV_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AZURE_OPENAI",
)

def development_mode_enabled() -> bool:
    return os.environ.get("NEXUS_DEVELOPMENT_MODE", "").strip().lower() in _TRUE_VALUES


def _command_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
    }


def _bounded_redacted(text: str | None) -> str:
    clean = redact(text or "")
    if len(clean) <= MAX_OUTPUT_CHARS:
        return clean
    omitted = len(clean) - MAX_OUTPUT_CHARS
    return clean[:MAX_OUTPUT_CHARS] + f"\n...[truncated {omitted} chars]"


def _command_timeout(meta: dict[str, str] | None) -> int:
    raw = (meta or {}).get("timeout", "").strip()
    if not raw:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise ValueError("command timeout must be a whole number of seconds") from exc
    if timeout < 1:
        raise ValueError("command timeout must be at least 1 second")
    if timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(
            f"command timeout must not exceed {MAX_COMMAND_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def _unknown_command_meta(meta: dict[str, str] | None) -> list[str]:
    return sorted(set(meta or {}) - _COMMAND_META_KEYS)


def _record_progress(conn: Any, db_msg_id: int, elapsed: float, timeout: int) -> None:
    message = f"command running: {elapsed:.1f}s elapsed; deadline {timeout}s"
    logger.info("Mailbox %s %s", db_msg_id, message)
    if conn is not None:
        conn.execute(
            "UPDATE mailbox_messages SET last_error=? WHERE id=?",
            (message, db_msg_id),
        )
        conn.commit()


def _terminate_process_group(pid: int) -> None:
    """Terminate the detached command and its children, escalating if needed."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _reap_active_command(db_msg_id: int) -> None:
    active = _ACTIVE_COMMANDS.pop(db_msg_id, None)
    if active is None:
        return
    try:
        active.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def process_command(
    msg: Any,
    repo_path: Path,
    conn: Any,
    db_msg_id: int,
) -> tuple[str, None]:
    command = msg.body.strip()
    if not command:
        raise ValueError("command body must not be empty")

    timeout = _command_timeout(msg.meta)
    unknown_meta = _unknown_command_meta(msg.meta)
    if unknown_meta:
        logger.warning("Unknown command front-matter keys: %s", ", ".join(unknown_meta))

    if conn is None and db_msg_id == 0:
        started = time.time()
        try:
            completed = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=str(repo_path),
                env=_command_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            returncode = completed.returncode
            raw_stdout = completed.stdout
            raw_stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = COMMAND_TIMEOUT_EXIT_CODE
            raw_stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout
            )
            raw_stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
            timed_out = True
        duration = max(0.0, time.time() - started)
        stdout = _bounded_redacted(raw_stdout)
        stderr = _bounded_redacted(raw_stderr)
        return (
            "\n".join(
                [
                    f"**Exit code:** {returncode}",
                    f"**Duration:** {duration:.3f}s",
                    f"**Deadline:** {timeout}s",
                    *(["**Timed out:** yes"] if timed_out else []),
                    *(
                        [f"**Warning:** Unknown front-matter keys: {', '.join(unknown_meta)}"]
                        if unknown_meta
                        else []
                    ),
                    "",
                    "### stdout",
                    "",
                    "```text",
                    stdout,
                    "```",
                    "",
                    "### stderr",
                    "",
                    "```text",
                    stderr,
                    "```",
                ]
            ),
            None,
        )

    from modules.mailbox import _DEFERRED_PROCESSING

    state_root = Path(os.environ.get(
        "NEXUS_MAILBOX_COMMAND_STATE_DIR",
        str(Path(tempfile.gettempdir()) / "nexus-mailbox-command-jobs"),
    ))
    state_dir = state_root / str(db_msg_id)
    stdout_path = state_dir / "stdout"
    stderr_path = state_dir / "stderr"
    exit_path = state_dir / "exit_code"
    pid_path = state_dir / "pid"
    started_path = state_dir / "started"
    timeout_path = state_dir / "timeout"
    timed_out_path = state_dir / "timed_out"

    if exit_path.exists():
        _reap_active_command(db_msg_id)
        try:
            returncode = int(exit_path.read_text().strip())
        except (OSError, ValueError):
            returncode = 255
        try:
            started = float(started_path.read_text().strip())
        except (OSError, ValueError):
            started = time.time()
        duration = max(0.0, time.time() - started)
        try:
            persisted_timeout = int(timeout_path.read_text().strip())
        except (OSError, ValueError):
            persisted_timeout = timeout
        timed_out = timed_out_path.exists()
        stdout = _bounded_redacted(stdout_path.read_text(errors="replace") if stdout_path.exists() else "")
        stderr = _bounded_redacted(stderr_path.read_text(errors="replace") if stderr_path.exists() else "")
    else:
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
                try:
                    started = float(started_path.read_text().strip())
                except (OSError, ValueError):
                    started = time.time()
                try:
                    persisted_timeout = int(timeout_path.read_text().strip())
                except (OSError, ValueError):
                    persisted_timeout = timeout
                duration = max(0.0, time.time() - started)
                if duration < persisted_timeout:
                    _record_progress(conn, db_msg_id, duration, persisted_timeout)
                    return _DEFERRED_PROCESSING
                _terminate_process_group(pid)
                _reap_active_command(db_msg_id)
                returncode = COMMAND_TIMEOUT_EXIT_CODE
                timed_out = True
                timed_out_path.write_text(
                    f"deadline {persisted_timeout}s exceeded after {duration:.3f}s\n"
                )
                exit_path.write_text(str(returncode))
                raw_stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
                timeout_line = (
                    f"[timeout] deadline {persisted_timeout}s exceeded; "
                    f"terminated after {duration:.3f}s"
                )
                stderr_path.write_text(
                    (raw_stderr.rstrip("\n") + "\n" if raw_stderr else "") + timeout_line + "\n"
                )
                stdout = _bounded_redacted(
                    stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
                )
                stderr = _bounded_redacted(stderr_path.read_text(errors="replace"))
            except (OSError, ValueError):
                _reap_active_command(db_msg_id)
                try:
                    started = float(started_path.read_text().strip())
                except (OSError, ValueError):
                    started = time.time()
                duration = max(0.0, time.time() - started)

                partial_stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
                partial_stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""

                outcome = evaluate_interrupted_command(state_dir, repo_path)
                returncode = {"succeeded": 0, "failed": 1, "unknown": 255}[outcome.status]
                evidence_line = f"[restart recovery: {outcome.status}] {outcome.evidence}"
                raw_stderr = (
                    partial_stderr.rstrip("\n") + "\n" if partial_stderr.strip() else ""
                ) + evidence_line

                # Persist the recovered outcome once so a later poll (including
                # one triggered by a second restart) reads this cached result
                # via the exit_path branch above instead of re-deriving it —
                # this is what keeps the eventual Outbox reply a single,
                # idempotent publication.
                try:
                    exit_path.write_text(str(returncode))
                    stderr_path.write_text(raw_stderr)
                except OSError:
                    logger.warning(
                        "Failed to persist recovered mailbox command result in %s", state_dir
                    )

                stdout = _bounded_redacted(partial_stdout)
                stderr = _bounded_redacted(raw_stderr)
                persisted_timeout = timeout
                timed_out = False
        else:
            state_dir.mkdir(parents=True, exist_ok=True)
            started_path.write_text(str(time.time()))
            timeout_path.write_text(str(timeout))
            write_recovery_state(
                state_dir, repo_path, parse_expected_services(msg.meta)
            )
            wrapper = (
                "set +e\n(" + command + "\n)\n"
                "rc=$?\n"
                f"printf '%s\n' \"$rc\" > {shlex.quote(str(exit_path))}\n"
            )
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                proc = subprocess.Popen(
                    ["/bin/bash", "-lc", wrapper],
                    cwd=str(repo_path),
                    env=_command_environment(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            pid_path.write_text(str(proc.pid))
            _ACTIVE_COMMANDS[db_msg_id] = proc
            return _DEFERRED_PROCESSING
    return (
        "\n".join(
            [
                f"**Exit code:** {returncode}",
                f"**Duration:** {duration:.3f}s",
                f"**Deadline:** {persisted_timeout}s",
                *(["**Timed out:** yes"] if timed_out else []),
                *(
                    [f"**Warning:** Unknown front-matter keys: {', '.join(unknown_meta)}"]
                    if unknown_meta
                    else []
                ),
                "",
                "### stdout",
                "",
                "```text",
                stdout,
                "```",
                "",
                "### stderr",
                "",
                "```text",
                stderr,
                "```",
            ]
        ),
        None,
    )


def install(mailbox_module: Any) -> None:
    """Install trusted command execution and unconditional task auto-approval."""
    mailbox_module.SUPPORTED_TYPES.add("command")
    mailbox_module._PROCESSORS["command"] = process_command

    if getattr(mailbox_module, "_development_adapter_installed", False):
        return

    original_task_processor = mailbox_module._PROCESSORS["task"]

    def process_task_without_manual_approval(
        msg: Any,
        repo_path: Path,
        conn: Any,
        db_msg_id: int,
    ) -> tuple[str, None]:
        _result_body, idea_id = original_task_processor(
            msg, repo_path, conn, db_msg_id
        )
        now = mailbox_module._now_iso()
        conn.execute(
            """
            UPDATE mailbox_task_approvals
            SET status='approved', approved_at=?, updated_at=?
            WHERE mailbox_msg_id=? AND status='pending_approval'
            """,
            (now, now, db_msg_id),
        )
        conn.commit()
        return (
            "Your task request has been recorded and approved automatically. "
            "The mailbox task worker will submit it for execution.",
            idea_id,
        )

    mailbox_module._PROCESSORS["task"] = process_task_without_manual_approval
    mailbox_module._development_adapter_installed = True
