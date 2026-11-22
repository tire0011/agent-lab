"""Host health checks at the start of every initiative tick.

2026-11-22 the VM disk was 93 % full (30,000 leftover test directories in
/tmp) and nobody noticed. Each tick now checks fixed limits without a model:
free disk, free memory and leftover /tmp entries. On low disk Nexus first
cleans up after itself; if a blocking problem remains, the tick starts no new
work, and the owner hears about each problem at most once a day.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

GB = 1024 ** 3
DISK_BLOCK_FREE_RATIO = 0.10
DISK_BLOCK_FREE_BYTES = 3 * GB
DISK_WARN_FREE_RATIO = 0.20
DISK_WARN_FREE_BYTES = 5 * GB
MEMORY_WARN_AVAILABLE_RATIO = 0.10
TMP_WARN_ENTRIES = 5000
# Own leftovers in the temp dir older than this are removed when disk is low.
OWN_TMP_MAX_AGE_SECONDS = 2 * 86400
MEMINFO = "/proc/meminfo"
ALARM_DECISION = "health_alarm"
ALARM_REPEAT = timedelta(hours=24)


@dataclass(frozen=True)
class Problem:
    name: str
    detail: str
    blocking: bool = False


def _disk(path: str) -> Problem | None:
    usage = shutil.disk_usage(path)
    free, ratio = usage.free, usage.free / usage.total if usage.total else 1.0
    detail = f"disk {path}: {free / GB:.1f} GB free ({ratio:.0%})"
    if ratio < DISK_BLOCK_FREE_RATIO or free < DISK_BLOCK_FREE_BYTES:
        return Problem("disk_low", detail, blocking=True)
    if ratio < DISK_WARN_FREE_RATIO or free < DISK_WARN_FREE_BYTES:
        return Problem("disk_low", detail)
    return None


def _memory() -> Problem | None:
    try:
        values = {}
        for line in Path(MEMINFO).read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0])
        ratio = values["MemAvailable"] / values["MemTotal"]
    except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
        return None
    if ratio < MEMORY_WARN_AVAILABLE_RATIO:
        return Problem("memory_low", f"memory: {ratio:.0%} available")
    return None


def _tmp_entries(tmp: str) -> Problem | None:
    try:
        count = sum(1 for _ in os.scandir(tmp))
    except OSError:
        return None
    if count > TMP_WARN_ENTRIES:
        return Problem("tmp_leftovers", f"{tmp}: {count} entries (leftovers of test or agent runs?)")
    return None


def check(disk_path: str = "/", tmp: str | None = None) -> list[Problem]:
    tmp = tmp or tempfile.gettempdir()
    checks: list[Callable[[], Problem | None]] = [
        lambda: _disk(disk_path), _memory, lambda: _tmp_entries(tmp),
    ]
    problems = []
    for run in checks:
        try:
            problem = run()
        except OSError:
            problem = None
        if problem is not None:
            problems.append(problem)
    return problems


def clean_own_tmp(tmp: str | None = None, max_age_seconds: float = OWN_TMP_MAX_AGE_SECONDS,
                  now: float | None = None) -> int:
    """Remove this user's top-level temp entries untouched for *max_age_seconds*."""
    tmp = tmp or tempfile.gettempdir()
    now = time.time() if now is None else now
    uid = os.getuid()
    removed = 0
    try:
        entries = list(os.scandir(tmp))
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if info.st_uid != uid or now - info.st_mtime < max_age_seconds:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
            removed += not os.path.lexists(entry.path)
        except OSError:
            continue
    return removed


def _should_alarm(db_path: str, name: str, now: datetime) -> bool:
    """True once a day per problem; records the alarm."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except sqlite3.Error:
        return True
    try:
        rows = conn.execute(
            "SELECT created_at FROM nexus_initiative_log WHERE decision = ? AND source_ref = ? "
            "ORDER BY created_at DESC LIMIT 1", (ALARM_DECISION, name),
        ).fetchall()
        if rows and now - datetime.fromisoformat(rows[0][0]) < ALARM_REPEAT:
            return False
        with conn:
            conn.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision, source_ref) "
                "VALUES (?, ?, ?, ?)",
                (f"health-{name}-{now.isoformat()}", now.isoformat(), ALARM_DECISION, name),
            )
        return True
    except (sqlite3.Error, ValueError):
        return True
    finally:
        conn.close()


@dataclass
class HealthResult:
    problems: list[Problem]
    messages: list[str]
    cleaned: int = 0

    @property
    def blocking(self) -> list[Problem]:
        return [p for p in self.problems if p.blocking]


def run(db_path: str, *, disk_path: str = "/", tmp: str | None = None,
        cleanups: list[Callable[[], int]] | None = None,
        now: datetime | None = None) -> HealthResult:
    """Check, clean up once if the disk is low, check again; owner messages
    for new problems (at most once a day each)."""
    now = now or datetime.now(timezone.utc)
    problems = check(disk_path, tmp)
    cleaned = 0
    if any(p.name in {"disk_low", "tmp_leftovers"} for p in problems):
        for cleanup in cleanups if cleanups is not None else [lambda: clean_own_tmp(tmp)]:
            try:
                cleaned += cleanup()
            except Exception:  # noqa: BLE001 - a failed cleanup must not break the tick
                continue
        problems = check(disk_path, tmp)
    messages = []
    for problem in problems:
        if _should_alarm(db_path, problem.name, now):
            action = "no new work until fixed" if problem.blocking else "warning"
            messages.append(f"Health: {problem.detail} ({action}; cleaned {cleaned} own temp entries)")
    return HealthResult(problems, messages, cleaned)
