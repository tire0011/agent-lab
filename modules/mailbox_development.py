"""Opt-in development extensions for the Git-backed Nexus mailbox.

This module intentionally provides a broad development escape hatch for a trusted
mailbox. It is inactive unless NEXUS_DEVELOPMENT_MODE is explicitly enabled.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from modules.redaction import redact

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 3600
MAX_OUTPUT_CHARS = 32_000
_TRUE_VALUES = {"1", "true", "yes", "on"}
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


def _parse_timeout(value: str | None) -> int:
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(value)
    except ValueError as exc:
        raise ValueError("timeout must be an integer number of seconds") from exc
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds")
    return timeout


def _command_environment() -> dict[str, str]:
    """Preserve normal execution context but do not hand API secrets to commands."""
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
    """Run the message body as unrestricted Bash in the code repository."""
    if not development_mode_enabled():
        return (
            "Command rejected: development mode is disabled. Set "
            "`NEXUS_DEVELOPMENT_MODE=1` on the Nexus service to enable it.",
            None,
        )

    command = msg.body.strip()
    if not command:
        raise ValueError("command body must not be empty")

    timeout = _parse_timeout(msg.meta.get("timeout"))
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=str(repo_path),
            env=_command_environment(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        stdout = _bounded_redacted(exc.stdout if isinstance(exc.stdout, str) else "")
        stderr = _bounded_redacted(exc.stderr if isinstance(exc.stderr, str) else "")
        return (
            f"Command timed out after {timeout}s (runtime {duration:.3f}s).\n\n"
            f"### stdout\n\n```text\n{stdout}\n```\n\n"
            f"### stderr\n\n```text\n{stderr}\n```",
            None,
        )

    duration = time.monotonic() - started
    stdout = _bounded_redacted(result.stdout)
    stderr = _bounded_redacted(result.stderr)
    return (
        "\n".join(
            [
                f"**Exit code:** {result.returncode}",
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
    """Install command + auto-approved task support into modules.mailbox.

    Calling this repeatedly is safe. The extensions are registered even while
    development mode is disabled so command messages get a useful rejection
    response instead of failing parsing; execution itself remains opt-in.
    """
    mailbox_module.SUPPORTED_TYPES.add("command")
    mailbox_module._PROCESSORS["command"] = process_command

    if getattr(mailbox_module, "_development_adapter_installed", False):
        return

    original_task_processor = mailbox_module._PROCESSORS["task"]

    def process_task_with_development_mode(
        msg: Any,
        repo_path: Path,
        conn: Any,
        db_msg_id: int,
    ) -> tuple[str, None]:
        result_body, idea_id = original_task_processor(
            msg, repo_path, conn, db_msg_id
        )
        auto_approve = (
            development_mode_enabled()
            and msg.meta.get("auto_approve", "").strip().lower() in _TRUE_VALUES
        )
        if not auto_approve:
            return result_body, idea_id

        now = mailbox_module._now_iso()
        conn.execute(
            """
            UPDATE mailbox_task_approvals
            SET status='approved', updated_at=?
            WHERE mailbox_msg_id=? AND status='pending_approval'
            """,
            (now, db_msg_id),
        )
        conn.commit()
        return (
            "Your task request has been recorded and automatically approved because "
            "Nexus development mode is enabled. The mailbox task worker will submit "
            "it for execution.",
            idea_id,
        )

    mailbox_module._PROCESSORS["task"] = process_task_with_development_mode
    mailbox_module._development_adapter_installed = True
