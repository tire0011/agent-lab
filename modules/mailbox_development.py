"""Development extensions for the Git-backed Nexus mailbox.

The command channel is intentionally unrestricted for the trusted development
mailbox, except for commands that would stop/restart the services responsible
for processing and publishing the mailbox itself.  Those lifecycle operations
must be performed by an independent maintenance/deployment path so the current
mailbox transaction can finish and commit cleanly.

NEXUS_DEVELOPMENT_MODE only controls optional task auto-approval.
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

# A mailbox command runs inside Nexus infrastructure.  Restarting/stopping the
# API or mailbox processor from that same command can terminate the process
# before its DB/outbox/git transaction is finalized, leaving the mailbox local
# checkout dirty and preventing subsequent fetch/rebase recovery.
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


def _contains_self_disruptive_service_action(command: str) -> bool:
    """Return True when a command would stop/restart its own control plane.

    This is deliberately narrow: normal development shell access stays
    unrestricted.  We only reject direct service lifecycle operations for the
    two services whose interruption can strand the current mailbox transaction.
    """
    normalized = " ".join(command.lower().split())
    service_pattern = "(?:" + "|".join(re.escape(s) for s in _SELF_MANAGED_SERVICES) + ")"
    action_pattern = "(?:" + "|".join(re.escape(a) for a in _SYSTEMCTL_DISRUPTIVE_ACTIONS) + ")"

    # sudo/systemctl restart|stop|kill ... agent-api[.service]
    if re.search(
        rf"(?:^|[;&|]\s*)(?:sudo\s+(?:-n\s+)?)?systemctl\s+{action_pattern}\b[^;&|]*\b{service_pattern}\b",
        normalized,
    ):
        return True

    # service agent-api restart|stop
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
    """Run the message body as trusted Bash in the code repository.

    Lifecycle changes to ``agent-api`` and ``nexus-mailbox`` are rejected here
    because this process may itself be executing the command.  Use an
    independent maintenance/deployment mechanism for those operations.
    """
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
    """Install trusted command + optional auto-approved task support.

    Calling this repeatedly is safe. Command execution is always available;
    NEXUS_DEVELOPMENT_MODE only gates automatic approval of task messages.
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
