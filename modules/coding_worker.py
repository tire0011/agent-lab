"""Shared building blocks for the external coding-agent workers.

Nexus can delegate a bounded coding task to more than one external CLI agent
(Codex via ``modules.codex_worker``, Claude Code via ``modules.claude_worker``).
Both must be interchangeable for their callers — ``CodexTaskService`` and the
implementation phase of ``ExperimentService`` pick one at runtime — so the
result shape, the sandbox vocabulary, the validation rules, and the
environment minimisation live here once instead of being duplicated per agent.

A worker implementation is expected to:

  - run its CLI as a subprocess, never via ``shell=True`` and never from a
    shell-composed command string;
  - reject an invalid configuration before starting anything
    (``validate_worker_config``);
  - cap stdout/stderr (``truncate_output``) and flag the truncation;
  - pass only the variables its CLI needs (``filtered_env``), never the full
    environment, so application secrets such as ``AGENT_API_KEY`` cannot leak
    into an external agent;
  - return a :class:`WorkerResult` describing what happened, including which
    provider produced it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Sandbox vocabulary shared by every worker. Each implementation maps these
# to whatever its own CLI offers; the *meaning* is fixed here:
#   read-only          -> may inspect the working directory, must not change it
#   workspace-write    -> may change files inside the working directory
#   danger-full-access -> no confinement; only ever on explicit request
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# Environment variables every CLI needs regardless of provider. Provider
# specific additions (auth, endpoints, config homes) are passed by the worker.
BASE_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
)


class WorkerConfigError(ValueError):
    """Raised for invalid worker configuration (bad path, unknown sandbox, …)."""


@dataclass
class WorkerResult:
    """Outcome of one external coding-agent call.

    ``provider`` names the agent that produced this result (``"codex"``,
    ``"claude"``, …). It defaults to ``"codex"`` because that was the only
    provider before the Claude worker existed, so results constructed by
    older code keep their meaning.
    """

    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    runtime_ms: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    # Parsed from the CLI's structured event stream
    thread_id: str | None
    final_message: str | None
    usage: dict[str, int] | None  # keys: input_tokens, output_tokens, …
    # Internal metadata
    task: str
    working_dir: str
    sandbox: str
    error: str | None  # worker-level error (binary not found, invalid dir, …)
    provider: str = "codex"

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "runtime_ms": self.runtime_ms,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "thread_id": self.thread_id,
            "final_message": self.final_message,
            "usage": self.usage,
            "task": self.task,
            "working_dir": self.working_dir,
            "sandbox": self.sandbox,
            "error": self.error,
            "provider": self.provider,
        }


def truncate_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cap *text* at *max_bytes* UTF-8 bytes; report whether it was cut."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + "\n[... output truncated]", True


def validate_worker_config(task: str, working_dir: str, sandbox: str) -> None:
    """Reject an unusable call before any subprocess is started.

    Raises ``WorkerConfigError`` for an empty task, an unknown sandbox mode,
    a relative working directory, or a working directory that does not exist.
    """
    if not task or not task.strip():
        raise WorkerConfigError("task must not be empty")

    if sandbox not in SANDBOX_MODES:
        raise WorkerConfigError(
            f"invalid sandbox {sandbox!r}; must be one of {SANDBOX_MODES}"
        )

    if not os.path.isabs(working_dir):
        raise WorkerConfigError(
            f"working_dir must be an absolute path, got: {working_dir!r}"
        )

    resolved = str(Path(working_dir).resolve())
    if not os.path.isdir(resolved):
        raise WorkerConfigError(
            f"working_dir does not exist or is not a directory: {resolved!r}"
        )


def filtered_env(extra_keys: frozenset[str] | set[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """Minimal environment for a worker subprocess.

    Only ``BASE_ENV_PASSTHROUGH`` plus the provider's own *extra_keys* are
    passed on. Application secrets (``AGENT_API_KEY`` and friends) are never
    handed to an external agent.
    """
    allowed = BASE_ENV_PASSTHROUGH | frozenset(extra_keys)
    return {key: value for key, value in os.environ.items() if key in allowed}
