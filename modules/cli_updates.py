"""Weekly update of the coding-agent CLIs (owner rule, 2026-11-22: at least
once a week). Runs at the end of a tick, when this tick's work is done; each
CLI's own updater is used and its version is recorded before and after, so a
change in behaviour can be matched to an update. Best effort: a failed update
is reported and the old version keeps running."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

SETTING = "nexus.cli_update.last_at"
INTERVAL = timedelta(days=7)
TIMEOUT_SECONDS = 300
_LOCAL_BIN = Path.home() / ".local" / "bin"

# name, version argv, update argv
CLIS: tuple[tuple[str, list[str], list[str]], ...] = (
    ("claude", ["claude", "--version"], ["claude", "update"]),
    ("codex", ["codex", "--version"], ["codex", "update"]),
    ("vibe", ["vibe", "--version"], ["uv", "tool", "upgrade", "mistral-vibe"]),
)


def enabled() -> bool:
    return os.environ.get("NEXUS_CLI_UPDATE", "1").strip().lower() not in {"0", "false", "off", "no"}


def _resolve(argv: list[str]) -> list[str] | None:
    binary = shutil.which(argv[0]) or (str(_LOCAL_BIN / argv[0]) if (_LOCAL_BIN / argv[0]).exists() else None)
    return [binary, *argv[1:]] if binary else None


def _run(argv: list[str], timeout: float) -> tuple[int | None, str]:
    resolved = _resolve(argv)
    if resolved is None:
        return None, f"{argv[0]} not installed"
    try:
        done = subprocess.run(resolved, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    except OSError as exc:
        return None, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def _version(argv: list[str]) -> str:
    code, out = _run(argv, 30)
    return out.splitlines()[0][:80] if code == 0 and out else "?"


def _last_run(conn: sqlite3.Connection) -> datetime | None:
    try:
        row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (SETTING,)).fetchone()
    except sqlite3.Error:
        return None
    try:
        return datetime.fromisoformat(row[0]) if row else None
    except ValueError:
        return None


def due(db_path: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except sqlite3.Error:
        return False
    try:
        last = _last_run(conn)
    finally:
        conn.close()
    return last is None or now - last >= INTERVAL


def _mark(db_path: str, now: datetime) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS service_settings "
                         "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            conn.execute(
                "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (SETTING, now.isoformat(), now.isoformat()),
            )
    finally:
        conn.close()


def update_all(db_path: str, now: datetime | None = None) -> str | None:
    """Update every installed CLI once a week; one summary line, or ``None``
    when not due. The week counts from this attempt, failed or not."""
    if not enabled() or not due(db_path, now):
        return None
    now = now or datetime.now(timezone.utc)
    _mark(db_path, now)
    parts = []
    for name, version_argv, update_argv in CLIS:
        if _resolve(version_argv) is None:
            continue
        before = _version(version_argv)
        code, out = _run(update_argv, TIMEOUT_SECONDS)
        after = _version(version_argv)
        if code != 0:
            parts.append(f"{name} update failed ({(out.splitlines() or ['?'])[-1][:120]}), still {after}")
        elif before != after:
            parts.append(f"{name} {before} -> {after}")
        else:
            parts.append(f"{name} unchanged ({after})")
    return "Weekly CLI update: " + "; ".join(parts) if parts else None
