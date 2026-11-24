"""Weekly open review of Nexus by one of its AIs (owner rule, 2026-11-22).

Fixed metrics only find what someone already looks for; on 2026-11-22 the
real problems (a 93 % full disk, an agent editing the deployed checkout) were
found by looking around freely. Once a week, at the end of a tick, a read-only
agent gets an open mandate plus a starter pack (the week's numbers, where the
database, logs and code are, last week's report). Codex can run read-only
commands, Claude can read files; they take turns, Azure (starter pack only)
stands in when neither can run. The report becomes a ``proposal`` issue:
the owner decides, Nexus implements nothing from it on its own.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable

INTERVAL = timedelta(days=7)
TIMEOUT_SECONDS = 1800
STALL_SECONDS = 600
LAST_AT = "nexus.weekly_review.last_at"
LAST_MODEL = "nexus.weekly_review.last_model"
LAST_REPORT = "nexus.weekly_review.last_report"
EXPLORERS = ("codex", "claude")  # take turns; azure only stands in
TITLE_PREFIX = "Wochenreview Nexus"


def enabled() -> bool:
    return os.environ.get("NEXUS_WEEKLY_REVIEW", "1").strip().lower() not in {"0", "false", "off", "no"}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS service_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return conn


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _set(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now.isoformat()),
    )


def due(db_path: str, now: datetime) -> bool:
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return False
    try:
        last = _get(conn, LAST_AT)
    finally:
        conn.close()
    try:
        return last is None or now - datetime.fromisoformat(last) >= INTERVAL
    except ValueError:
        return True


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        return [(f"n/a: {exc}",)]


def starter_pack(db_path: str, repo_root: str, now: datetime) -> str:
    """The week's numbers and where things are: a starting point, not the scope."""
    since = (now - INTERVAL).isoformat()
    lines = [f"Period: {since[:10]} to {now.isoformat()[:10]}", ""]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        conn = None
        lines.append(f"database not readable: {exc}")
    if conn is not None:
        try:
            sections = {
                "experiments by status and provider (count, avg minutes)":
                    ("SELECT status, implementation_provider, COUNT(*), "
                     "ROUND(AVG(implementation_runtime_ms)/60000.0, 1) FROM experiments "
                     "WHERE created_at >= ? GROUP BY 1, 2 ORDER BY 3 DESC", (since,)),
                "top experiment errors":
                    ("SELECT COUNT(*), substr(coalesce(error,''),1,140) FROM experiments "
                     "WHERE created_at >= ? AND status != 'succeeded' GROUP BY 2 ORDER BY 1 DESC LIMIT 8", (since,)),
                "adoptions by status":
                    ("SELECT status, COUNT(*) FROM experiment_adoptions WHERE started_at >= ? GROUP BY 1", (since,)),
                "initiative decisions":
                    ("SELECT decision, COUNT(*) FROM nexus_initiative_log WHERE created_at >= ? "
                     "GROUP BY 1 ORDER BY 2 DESC", (since,)),
                "planner shadow (rows, valid, agrees with paid)":
                    ("SELECT COUNT(*), SUM(local_valid), SUM(local_issue = paid_issue) FROM planner_shadow "
                     "WHERE created_at >= ?", (since,)),
                "re-verifications":
                    ("SELECT substr(verdict,1,40), COUNT(*) FROM reverifications WHERE created_at >= ? "
                     "GROUP BY 1", (since,)),
                # #810: money per coding agent this week, not just run counts.
                "coding-agent runs by provider (runs, with cost/subscription, subscription, cost USD)":
                    ("SELECT provider, COUNT(*), "
                     "SUM(actual_cost IS NOT NULL OR estimated_cost IS NOT NULL "
                     "OR cost_status IN ('subscription','free')), "
                     "SUM(cost_status = 'subscription'), "
                     "ROUND(SUM(COALESCE(actual_cost, estimated_cost, 0)), 4) "
                     "FROM runs WHERE timestamp >= ? GROUP BY 1 ORDER BY 5 DESC", (since,)),
            }
            for title, (sql, args) in sections.items():
                lines.append(f"## {title}")
                lines.extend("  " + " | ".join(str(v) for v in row) for row in _rows(conn, sql, args))
        finally:
            conn.close()
    usage = shutil.disk_usage("/")
    lines += ["", "## host", f"  disk /: {usage.free / 1024**3:.1f} GB free of {usage.total / 1024**3:.1f} GB"]
    try:
        lines.append(f"  entries in {tempfile.gettempdir()}: {sum(1 for _ in os.scandir(tempfile.gettempdir()))}")
    except OSError:
        pass
    try:
        log = subprocess.run(["git", "-C", repo_root, "log", "--oneline", f"--since={since}"],
                             capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        lines += ["", f"## commits on main this week ({len(log)})"] + [f"  {c}" for c in log[:40]]
    except (OSError, subprocess.SubprocessError):
        pass
    lines += [
        "",
        "## where things are",
        f"  database (open read-only): {db_path}",
        f"  code: {repo_root} (git history included)",
        f"  experiment logs: {repo_root}/state/experiment-logs/*.jsonl",
        f"  test logs: {repo_root}/state/experiment-logs/test-runs/ (per-test durations, *.slow-stacks.txt)",
        "  experiment worktrees: ~/agent-worktrees/",
        "  service logs: journalctl -u nexus-tick.service -u agent-api.service -u nexus-mailbox.service",
    ]
    return "\n".join(lines)


def review_prompt(pack: str, last_report: str | None, *, can_run_commands: bool, can_read_files: bool) -> str:
    ability = (
        "You can run read-only shell commands (sqlite3/python against the database opened "
        "read-only, journalctl, df, git log, reading logs). Use them."
        if can_run_commands else
        "You can read files (code, logs, test logs) but not run commands."
        if can_read_files else
        "You only have the starter pack below."
    )
    return (
        "You are an outside reviewer of Nexus, a self-developing agent that runs on this "
        "machine and changes its own code. Review it openly, the way an experienced engineer "
        "would after a week away: nobody can tell you in advance where something is wrong. "
        "Use the starter pack as a starting point, then look wherever something seems off - "
        "including places nobody suspects: resource use, loops, wasted budget, silent "
        "failures, code that grew or got duplicated, safety checks, tests, logs.\n\n"
        f"{ability} Change nothing: no writes, no restarts, no settings, no git commits. "
        "Never print secrets or the contents of credential files.\n\n"
        "Write the report in short plain German, Markdown:\n"
        "1. Was ich geprueft habe (one line per area).\n"
        "2. Trend gegenueber letzter Woche.\n"
        "3. Probleme, each with evidence (numbers, ids, file:line, log lines).\n"
        "4. Hoechstens 3 Vorschlaege, each small enough to approve or reject in one sentence.\n"
        "Say plainly when everything you checked looks fine.\n\n"
        + (f"LAST WEEK'S REPORT:\n{last_report[:6000]}\n\n" if last_report else "")
        + f"STARTER PACK:\n{pack}"
    )


def _run_codex(prompt: str, repo_root: str, db_path: str | None = None) -> str:
    from modules.codex_worker import CodexWorker

    # db_path is passed so the review run is audited into ``runs`` with its
    # cost (#810); without it the weekly review left no cost trace.
    result = CodexWorker(db_path=db_path).run(
        task=prompt, working_dir=repo_root, sandbox="read-only",
        timeout_seconds=TIMEOUT_SECONDS, stall_seconds=STALL_SECONDS, phase="weekly_review")
    if not result.success:
        raise RuntimeError(result.error or "codex review failed")
    return result.final_message or ""


def _run_claude(prompt: str, repo_root: str, db_path: str | None = None) -> str:
    from modules.claude_worker import ClaudeWorker

    result = ClaudeWorker(db_path=db_path).run(
        task=prompt, working_dir=repo_root, sandbox="read-only",
        timeout_seconds=TIMEOUT_SECONDS, stall_seconds=STALL_SECONDS, phase="weekly_review")
    if not result.success:
        raise RuntimeError(result.error or "claude review failed")
    return result.final_message or ""


def _run_azure(prompt: str, repo_root: str) -> str:
    from modules.nexus_reviewer import _run_azure as run

    return run(prompt, repo_root)


def default_runners(db_path: str) -> dict[str, Callable[[str, str], str]]:
    """The production runners, bound to *db_path* so their runs are audited (#810)."""
    return {
        "codex": lambda prompt, root: _run_codex(prompt, root, db_path),
        "claude": lambda prompt, root: _run_claude(prompt, root, db_path),
        "azure": _run_azure,
    }


RUNNERS: dict[str, Callable[[str, str], str]] = {"codex": _run_codex, "claude": _run_claude, "azure": _run_azure}
ABILITIES = {"codex": (True, True), "claude": (False, True), "azure": (False, False)}


def run(db_path: str, repo_root: str, open_issue: Callable[[str, str], None],
        now: datetime | None = None, runners: dict[str, Callable[[str, str], str]] | None = None) -> str | None:
    """Once a week: one open review, reported as an issue. A notification line, or ``None``."""
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    if not due(db_path, now):
        return None
    runners = runners if runners is not None else default_runners(db_path)
    conn = _connect(db_path)
    try:
        with conn:
            _set(conn, LAST_AT, now.isoformat(), now)  # the week counts from this attempt
        last_model, last_report = _get(conn, LAST_MODEL), _get(conn, LAST_REPORT)
    finally:
        conn.close()
    start = (EXPLORERS.index(last_model) + 1) % len(EXPLORERS) if last_model in EXPLORERS else 0
    order = [EXPLORERS[(start + i) % len(EXPLORERS)] for i in range(len(EXPLORERS))] + ["azure"]
    pack = starter_pack(db_path, repo_root, now)
    errors = []
    for name in order:
        runner = runners.get(name)
        if runner is None:
            continue
        commands, files = ABILITIES[name]
        try:
            report = runner(review_prompt(pack, last_report, can_run_commands=commands, can_read_files=files),
                            repo_root).strip()
        except Exception as exc:  # noqa: BLE001 - the next model stands in
            errors.append(f"{name}: {str(exc)[:200]}")
            continue
        if not report:
            errors.append(f"{name}: empty report")
            continue
        title = f"{TITLE_PREFIX} {now.date().isoformat()}"
        open_issue(title, f"{report}\n\n_Geprueft von: {name}_")
        conn = _connect(db_path)
        try:
            with conn:
                _set(conn, LAST_MODEL, name, now)
                _set(conn, LAST_REPORT, report[:6000], now)
        finally:
            conn.close()
        return f"Weekly review by {name}: issue \"{title}\" opened"
    return "Weekly review failed: " + "; ".join(errors or ["no reviewer available"])
