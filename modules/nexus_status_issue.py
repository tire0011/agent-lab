"""A pinned GitHub issue that shows what Nexus is doing right now (SKILL.md).

Outside agents read it instead of digging through the history (SKILL.md links
it). The tick rewrites its body at most once an hour; it carries the
``proposal`` label so Nexus never picks it up as work.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATUS_TITLE = "Nexus status"
UPDATE_EVERY = timedelta(hours=1)

Gh = Callable[[list[str]], str]


def _gh(repo_root: str) -> Gh:
    def run(argv: list[str]) -> str:
        proc = subprocess.run(["gh", *argv], cwd=repo_root, capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
        return proc.stdout
    return run


def render(conn: sqlite3.Connection, *, now: datetime, budget: int, spent: int,
           paused: bool, busy: str | None) -> str:
    lines = [f"_Updated by Nexus at {now.strftime('%Y-%m-%d %H:%M')} UTC, at most once an hour._", ""]
    if paused:
        state = "paused by the owner"
    elif busy:
        state = f"working: {busy}"
    else:
        state = "idle, waiting for the next tick (every 15 min)"
    lines.append(f"**State:** {state}")
    lines.append(f"**Budget:** {spent} of {budget or 'unlimited'} paid starts used in the last 24h")
    lines += ["", "**Recent experiments**", ""]
    try:
        rows = conn.execute(
            "SELECT substr(id,1,8), status, substr(created_at,1,16), goal FROM experiments "
            "ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for exp_id, status, created, goal in rows:
        first = (str(goal or "").strip().splitlines() or [""])[0][:100]
        lines.append(f"- `{exp_id}` {status} ({created}): {first}")
    if not rows:
        lines.append("- none yet")
    lines += [
        "",
        "Want to take part? Read [SKILL.md](https://github.com/tire0011/agent-lab/blob/main/SKILL.md). "
        "Open an issue; the owner approves it before Nexus starts work on it.",
    ]
    return "\n".join(lines)


def update(conn: sqlite3.Connection, repo_root: str, *, budget: int, spent: int, paused: bool,
           busy: str | None, now: datetime | None = None, gh: Gh | None = None) -> bool:
    """Rewrite the status issue if it is older than an hour; never raises."""
    gh = gh or _gh(repo_root)
    now = now or datetime.now(timezone.utc)
    try:
        found = json.loads(gh(["issue", "list", "--state", "open", "--search", f'in:title "{STATUS_TITLE}"',
                               "--json", "number,title,updatedAt"]) or "[]")
        mine = [i for i in found if str(i.get("title")) == STATUS_TITLE]
        body = render(conn, now=now, budget=budget, spent=spent, paused=paused, busy=busy)
        if not mine:
            gh(["issue", "create", "--title", STATUS_TITLE, "--body", body, "--label", "proposal"])
            try:
                number = json.loads(gh(["issue", "list", "--state", "open", "--search",
                                        f'in:title "{STATUS_TITLE}"', "--json", "number,title"]) or "[]")
                for issue in number:
                    if str(issue.get("title")) == STATUS_TITLE:
                        gh(["issue", "pin", str(issue["number"])])
            except Exception as exc:  # noqa: BLE001 - pinning is a nicety
                logger.info("status issue: pin failed: %s", exc)
            return True
        issue = mine[0]
        updated = datetime.fromisoformat(str(issue.get("updatedAt")).replace("Z", "+00:00"))
        if now - updated < UPDATE_EVERY:
            return False
        gh(["issue", "edit", str(issue["number"]), "--body", body])
        return True
    except Exception as exc:  # noqa: BLE001 - the status must never fail a tick
        logger.warning("status issue update failed: %s", exc)
        return False


def update_for(service: Any, repo_root: str) -> None:
    """Glue for the tick: read the state from the initiative service."""
    try:
        budget = int(getattr(service, "daily_budget", 0) or 0)
        spent = int(service._spent_today())
        paused = bool(service._paused())
        busy = service.busy_reason()
        conn = service._conn
    except Exception as exc:  # noqa: BLE001
        logger.warning("status issue: state unavailable: %s", exc)
        return
    update(conn, repo_root, budget=budget, spent=spent, paused=paused, busy=busy)
