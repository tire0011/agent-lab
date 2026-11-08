"""Missing-executable capability gaps and their recovery (#120).

A ``command_not_found`` is treated as a capability gap, not only as a
transient error. This module holds the pieces that do not depend on the
orchestrator:

* ``ToolProfile`` - what a known executable provides, how to install it and
  which reusable Nexus operation covers the same capability.
* ``plan_recovery`` - the decision policy across the four alternatives
  (existing equivalent, install/provision, capability issue, reusable
  operation).
* ``CapabilityGapStore`` - SQLite history keyed by the normalized tool
  identity. It counts recurrences, remembers the linked GitHub issue (local
  deduplication) and records which recovery was chosen.
* ``provision_tool`` - installs a missing tool on the dedicated Nexus VM.

This is deliberately not an allowlist: unknown executables get a generic
profile and the same alternatives.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ALT_EXISTING_EQUIVALENT = "existing_equivalent"
ALT_INSTALL = "install_or_provision"
ALT_CAPABILITY_ISSUE = "capability_issue"
ALT_REUSABLE_OPERATION = "reusable_operation"
CHOICE_MISSING_CAPABILITY = "missing_capability"

PROVISION_OPERATION = "provision_tool"
AUTO_PROVISION_ENV = "NEXUS_AUTO_PROVISION_TOOLS"
# Seen this often, a gap is recurring: it gets an issue even without a profile,
# and a covering operation is promoted as the preferred route.
RECURRENCE_THRESHOLD = 2

# Executables whose absence the runtime already resolves itself (python ->
# resolved interpreter). They are aliases, not durable gaps.
_RUNTIME_ALIASES = {"python", "python3", "pip", "pip3"}
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


@dataclass(frozen=True)
class ToolProfile:
    executable: str
    capability: str
    display_name: str
    equivalents: tuple[str, ...] = ()
    apt_package: str | None = None
    pip_package: str | None = None
    operation: str | None = None
    known: bool = True


_PROFILES: dict[str, ToolProfile] = {
    "rg": ToolProfile(
        "rg",
        "repository text-search capability",
        "ripgrep",
        equivalents=("grep -R -E", "search_text"),
        apt_package="ripgrep",
        operation="search_text",
    ),
    "jq": ToolProfile(
        "jq",
        "JSON processing capability",
        "jq",
        equivalents=("python -m json.tool",),
        apt_package="jq",
    ),
    "fd": ToolProfile(
        "fd",
        "repository file-search capability",
        "fd",
        equivalents=("find",),
        apt_package="fd-find",
    ),
    "tree": ToolProfile(
        "tree",
        "directory listing capability",
        "tree",
        equivalents=("find",),
        apt_package="tree",
    ),
}


def normalized_identity(executable: str) -> str:
    return Path(str(executable or "").strip()).name.lower()[:120]


def tool_profile(executable: str) -> ToolProfile:
    identity = normalized_identity(executable)
    profile = _PROFILES.get(identity)
    if profile is not None:
        return profile
    # Unknown tools still get every alternative; the package name is a guess
    # the provisioning step verifies by checking the executable afterwards.
    return ToolProfile(
        identity,
        f"development capability provided by {identity}",
        identity,
        apt_package=identity if _TOOL_NAME_RE.match(identity) else None,
        known=False,
    )


def is_runtime_alias(executable: str) -> bool:
    return normalized_identity(executable) in _RUNTIME_ALIASES


def auto_provisioning_enabled(environ: dict[str, str] | None = None) -> bool:
    value = (environ if environ is not None else os.environ).get(AUTO_PROVISION_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def plan_recovery(
    *,
    executable: str,
    command: str,
    equivalent: tuple[str, str] | None,
    occurrences: int,
    auto_provision: bool,
    available_operations: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Describe all alternatives and choose one (the issue's decision policy).

    Order: an equivalent that is already installed is cheapest; installing
    is next when enabled for this VM; then a reusable Nexus operation the
    controller can call; otherwise a structured ``missing_capability``.
    Issue tracking is orthogonal - it is decided by ``durable``.
    """
    profile = tool_profile(executable)
    operation = profile.operation if profile.operation in available_operations else None
    can_install = bool(profile.apt_package or profile.pip_package)
    recurring = occurrences >= RECURRENCE_THRESHOLD
    durable = not is_runtime_alias(executable) and (profile.known or recurring)
    alternatives = [
        {
            "kind": ALT_EXISTING_EQUIVALENT,
            "available": equivalent is not None,
            "detail": equivalent[1] if equivalent else "no installed equivalent found",
            "candidates": list(profile.equivalents),
        },
        {
            "kind": ALT_INSTALL,
            "available": can_install,
            "automatic": auto_provision and can_install,
            "operation": PROVISION_OPERATION,
            "args": {"tool": profile.executable},
            "package": profile.pip_package or profile.apt_package,
            "manager": "pip" if profile.pip_package else ("apt" if profile.apt_package else None),
        },
        {
            "kind": ALT_CAPABILITY_ISSUE,
            "available": durable,
            "detail": "durable/recurring gap" if durable else "not durable yet",
        },
        {
            "kind": ALT_REUSABLE_OPERATION,
            "available": operation is not None,
            "operation": operation,
            "promoted": operation is not None and recurring,
        },
    ]
    if equivalent is not None:
        chosen = ALT_EXISTING_EQUIVALENT
    elif auto_provision and can_install:
        chosen = ALT_INSTALL
    elif operation is not None:
        chosen = ALT_REUSABLE_OPERATION
    else:
        chosen = CHOICE_MISSING_CAPABILITY
    return {
        "event": "capability_gap",
        "capability": profile.capability,
        "display_name": profile.display_name,
        "normalized_identity": profile.executable,
        "missing_executable": str(executable)[:120],
        "attempted_command": str(command)[:2000],
        "occurrences": occurrences,
        "recurring": recurring,
        "durable": durable,
        "alternatives": alternatives,
        "chosen": chosen,
        "recommended_operation": operation,
        "installation_attempted": False,
    }


def suggested_solution(executable: str) -> str:
    profile = tool_profile(executable)
    parts = []
    if profile.apt_package or profile.pip_package:
        manager = "pip (Nexus virtualenv)" if profile.pip_package else "apt"
        package = profile.pip_package or profile.apt_package
        parts.append(
            f"install `{package}` via {manager} on the Nexus VM "
            f"(operation `{PROVISION_OPERATION}` with tool=`{profile.executable}`)"
        )
    if profile.operation:
        parts.append(f"use/extend the portable Nexus operation `{profile.operation}`")
    else:
        parts.append("expose this capability as a reusable OperationsRegistry/tool-router operation")
    return "; ".join(parts) + "."


def provision_tool(
    tool: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] | None = None,
    python: str = sys.executable,
    timeout: int = 600,
) -> dict[str, Any]:
    """Install a missing tool on the dedicated Nexus VM and verify it.

    Python packages go into the running interpreter's environment (the Nexus
    virtualenv); system tools use non-interactive ``apt-get`` (``sudo -n``
    unless already root). The executed command is returned for the log.
    """
    which = which or shutil.which
    identity = normalized_identity(tool)
    if not _TOOL_NAME_RE.match(identity):
        raise ValueError("tool must be a plain executable name")
    profile = tool_profile(identity)
    if which(identity):
        return {"tool": identity, "status": "already_available", "command": None, "available": True}
    if profile.pip_package:
        command = [python, "-m", "pip", "install", profile.pip_package]
    elif profile.apt_package:
        prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo", "-n"]
        command = [*prefix, "apt-get", "install", "-y", "--no-install-recommends", profile.apt_package]
    else:
        return {"tool": identity, "status": "no_install_recipe", "command": None, "available": False}
    try:
        completed = runner(command, capture_output=True, text=True, timeout=timeout, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"tool": identity, "status": "failed", "command": command, "available": False, "detail": str(exc)[:1000]}
    available = bool(which(identity))
    output = ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
    return {
        "tool": identity,
        "status": "installed" if completed.returncode == 0 and available else "failed",
        "command": command,
        "exit_code": completed.returncode,
        "available": available,
        "detail": output,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapabilityGapStore:
    """Durable gap history: recurrence counts, chosen recovery, issue link."""

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_gaps (
                    identity TEXT PRIMARY KEY,
                    executable TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_command TEXT,
                    last_recovery TEXT,
                    last_origin TEXT,
                    issue_number INTEGER,
                    issue_url TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    def record(self, *, identity: str, executable: str, capability: str, command: str, origin: str | None) -> int:
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO capability_gaps
                    (identity, executable, capability, occurrences, first_seen, last_seen, last_command, last_origin, status)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, 'open')
                ON CONFLICT(identity) DO UPDATE SET
                    occurrences = occurrences + 1,
                    last_seen = excluded.last_seen,
                    last_command = excluded.last_command,
                    last_origin = COALESCE(excluded.last_origin, last_origin),
                    status = 'open'
                """,
                (identity, executable[:120], capability[:240], now, now, command[:2000], origin),
            )
        return int(self.get(identity)["occurrences"])

    def set_recovery(self, identity: str, recovery: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE capability_gaps SET last_recovery = ? WHERE identity = ?", (recovery[:500], identity)
            )

    def set_issue(self, identity: str, number: int, url: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE capability_gaps SET issue_number = ?, issue_url = ? WHERE identity = ?",
                (number, url, identity),
            )

    def clear_issue(self, identity: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE capability_gaps SET issue_number = NULL, issue_url = NULL WHERE identity = ?", (identity,)
            )

    def mark_resolved(self, identity: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE capability_gaps SET status = 'resolved' WHERE identity = ?", (identity,))

    def get(self, identity: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM capability_gaps WHERE identity = ?", (identity,)).fetchone()
        return dict(row) if row is not None else None
