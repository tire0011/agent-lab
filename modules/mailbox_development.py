"""Development extensions for the Git-backed Nexus mailbox.

The command channel is available for the trusted development mailbox.
Mailbox tasks are immediately approved and handed to the existing task worker,
so the controller can execute them without a manual approval step.
"""

from __future__ import annotations

import os
import re
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

_SELF_MANAGED_SERVICES = ("agent-api", "agent-api.service", "nexus-mailbox", "nexus-mailbox.service")
_SYSTEMCTL_DISRUPTIVE_ACTIONS = (
    "restart",
    "stop",
    "kill",
    "try-restart",
    "reload-or-restart",
    "reload-or-try-restart",
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


def _contains_self_disruptive_service_action(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    service_pattern = "(?:" + "|".join(re.escape(s) for s in _SELF_MANAGED_SERVICES) + ")"
    action_pattern = "(?:" + "|".join(re.escape(a) for a in _SYSTEMCTL_DISRUPTIVE_ACTIONS) + ")"
    if re.search(
        rf"(?:^|[;&|]\s*)(?:sudo\s+(?:-n\s+)?)?systemctl\s+{action_pattern}\b[^;&|]*\b{service_pattern}\b",
        normalized,
    ):
        return True
    if re.search(
        rf"(?:^|[;&|]\s*)(?:sudo\s+(?:-n\s+)?)?service\s+{service_pattern}\s+{action_pattern}\b",
        normalized,
    ):
        return True
    return False


def process_command(
    msg: Any,
    repo_path: Path,
    conn: Any,
    db_msg_id: int,
) -> tuple[str, None]:
    command = msg.body.strip()
    if not command:
        raise ValueError("command body must not be empty")
    if _contains_self_disruptive_service_action(command):
        raise ValueError(
            "mailbox command cannot stop/restart agent-api or nexus-mailbox while "
            "processing its own transaction; use an independent maintenance/deployment path"
        )

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
