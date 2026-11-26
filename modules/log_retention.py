"""Old logs are removed by the tick (owner 2026-11-26).

On the VM the experiment logs had never been cleaned (6351 files back to
August, 371 MB) and the Vibe session logs grew by about 40 MB a day (206 MB).
The results live in the database; the logs only serve debugging. Experiment
logs are kept 14 days (the weekly review compares with the previous week),
except for experiments still running or parked (needs_review); Vibe session
logs are kept 7 days. The systemd journal keeps its own size cap.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

EXPERIMENT_LOG_DAYS = 14
VIBE_LOG_DAYS = 7
KEEP_STATUSES = ("queued", "selecting", "implementing", "verifying", "needs_review")
_EXPERIMENT_ID = re.compile(r"^\.?([0-9a-f]{32})(?:\.jsonl|\.lock)$")


def enabled() -> bool:
    return os.environ.get("NEXUS_LOG_RETENTION", "1").strip().lower() not in {"0", "false", "off", "no"}


def _kept_experiments(db_path: str) -> set[str]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return set()
    try:
        marks = ",".join("?" * len(KEEP_STATUSES))
        return {str(r[0]) for r in conn.execute(
            f"SELECT id FROM experiments WHERE status IN ({marks})", KEEP_STATUSES)}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def prune_experiment_logs(log_dir: str | Path, db_path: str, *, days: int = EXPERIMENT_LOG_DAYS,
                          now: float | None = None) -> int:
    """Remove top-level log files older than *days*; returns how many went.
    Logs of running or parked experiments stay. If the database cannot be read,
    experiment logs stay too (nothing is removed blind)."""
    directory = Path(log_dir)
    if not directory.is_dir():
        return 0
    kept = _kept_experiments(db_path)
    db_readable = os.path.isfile(db_path)
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for entry in directory.iterdir():
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            match = _EXPERIMENT_ID.match(entry.name)
            if match and (not db_readable or match.group(1) in kept):
                continue
            if not match and entry.suffix != ".jsonl":
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def prune_vibe_logs(home: str | Path | None = None, *, days: int = VIBE_LOG_DAYS,
                    now: float | None = None) -> int:
    """Remove Vibe session directories untouched for *days*; returns how many went."""
    root = Path(home or Path.home()) / ".vibe" / "logs" / "session" / "unified"
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += not entry.exists()
        except OSError:
            continue
    return removed


def prune_all(repo_root: str, db_path: str) -> str | None:
    """Run both prunes; a log line when something was removed."""
    if not enabled():
        return None
    from modules.central_logging import resolve_log_dir

    log_dir = Path(resolve_log_dir())
    if not log_dir.is_absolute():
        log_dir = Path(repo_root) / log_dir
    logs = prune_experiment_logs(log_dir, db_path)
    vibe = prune_vibe_logs()
    if logs or vibe:
        return f"log retention: removed {logs} old experiment log file(s), {vibe} old Vibe session(s)"
    return None
