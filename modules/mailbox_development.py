"""Development extensions for the Git-backed Nexus mailbox.

The command channel is available for the trusted development mailbox.
Mailbox tasks are immediately approved and handed to the existing task worker,
so the controller can execute them without a manual approval step.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.redaction import redact

MAX_OUTPUT_CHARS = 32_000
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


def process_command(
    msg: Any,
    repo_path: Path,
    conn: Any,
    db_msg_id: int,
) -> tuple[str, None]:
    command = msg.body.strip()
    if not command:
        raise ValueError("command body must not be empty")
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

    if exit_path.exists():
        active = _ACTIVE_COMMANDS.pop(db_msg_id, None)
        if active is not None:
            try:
                active.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            returncode = int(exit_path.read_text().strip())
        except (OSError, ValueError):
            returncode = 255
        try:
            started = float(started_path.read_text().strip())
        except (OSError, ValueError):
            started = time.time()
        duration = max(0.0, time.time() - started)
        stdout = _bounded_redacted(stdout_path.read_text(errors="replace") if stdout_path.exists() else "")
        stderr = _bounded_redacted(stderr_path.read_text(errors="replace") if stderr_path.exists() else "")
    else:
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
                return _DEFERRED_PROCESSING
            except (OSError, ValueError):
                stderr = "Mailbox command runner exited without recording an exit code."
                stdout = ""
                returncode = 255
                try:
                    started = float(started_path.read_text().strip())
                except (OSError, ValueError):
                    started = time.time()
                duration = max(0.0, time.time() - started)
        else:
            state_dir.mkdir(parents=True, exist_ok=True)
            started_path.write_text(str(time.time()))
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
