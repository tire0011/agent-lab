"""Every two weeks Nexus looks outward (owner rule, 2026-11-28).

The weekly review looks at Nexus itself; nothing looked at what is new
outside: smaller or faster local models, llama.cpp and CLI features, new ideas
for coding agents, provider models and prices. A read-only Claude agent with
web search gets the current setup and last outlook, and writes at most three
concrete, linked proposals. The report becomes a ``proposal`` issue: the owner
decides; nothing is installed or changed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

INTERVAL = timedelta(days=14)
TIMEOUT_SECONDS = 1800
STALL_SECONDS = 600
LAST_AT = "nexus.outlook.last_at"
LAST_REPORT = "nexus.outlook.last_report"
TITLE_PREFIX = "Ausblick Nexus"


def enabled() -> bool:
    return os.environ.get("NEXUS_OUTLOOK", "1").strip().lower() not in {"0", "false", "off", "no"}


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


def setup_summary() -> str:
    """What Nexus runs today, so the search can compare against it."""
    model = "unknown"
    try:
        for line in Path("/etc/agent-lab/llama.env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("LLAMA_MODEL", "MODEL")) and "=" in line:
                model = line.split("=", 1)[1].strip()
    except OSError:
        pass
    providers = os.environ.get("AGENT_CODING_PROVIDERS", "codex,claude,mistral")
    return (
        f"- Local model on the VM: {model} via llama.cpp llama-server, CPU only.\n"
        "- VM: Azure, 2 vCPU, 8 GB RAM, ~29 GB disk; the local model generates ~2.5-2.8 tokens/s, "
        "too slow for many local attempts.\n"
        "- Owner's PC (batch lane via the mailbox, public data only): NVIDIA RTX 2000 Ada, 8 GB VRAM, "
        "llama.cpp with Ornith-1.5-9B Q4_K_M.\n"
        f"- Paid coding agents in rotation: {providers} (Codex CLI, Claude Code, Mistral via the Vibe CLI "
        "on a flat subscription); reviewers claude, codex, mistral, azure.\n"
        "- Nexus: Python, SQLite, systemd timers; it plans, implements, verifies, reviews and merges "
        "its own changes."
    )


def outlook_prompt(setup: str, last_report: str | None) -> str:
    return (
        "You scout what is new outside for Nexus, a self-developing coding agent. Search the web "
        "(model hubs, release notes, GitHub, papers, provider announcements) for things released or "
        "improved recently that would help Nexus concretely:\n"
        "1. Local models: small coding models that fit the VM (CPU, 8 GB RAM) or the owner's 8 GB "
        "GPU and are better or faster than the current one (quantizations, distills).\n"
        "2. Tooling: llama.cpp features (speculative decoding, new quant types, CPU speedups), "
        "Codex CLI / Claude Code / Vibe CLI features worth using.\n"
        "3. Ideas: coding-agent techniques, open-source projects or benchmarks Nexus could adopt.\n"
        "4. Providers: models or prices at Mistral, OpenAI, Anthropic that change what is cheapest "
        "or best for Nexus.\n\n"
        "Only read: do not install, download models, change files or run commands. Skip what the "
        "last outlook already proposed unless something changed.\n\n"
        "Write in short plain German, Markdown: 1. Was ich gefunden habe (short, with links). "
        "2. Hoechstens 3 Vorschlaege, each with a link, the expected gain for Nexus and how to "
        "measure it, small enough to approve or reject in one sentence. Say plainly when nothing "
        "relevant is new.\n\n"
        "End with exactly one fenced ```json block listing the same proposals, each as an issue "
        "Nexus can work on: {\"proposals\": [{\"title\": \"<short English title>\", "
        "\"body\": \"<English: evidence with link, change, how to measure>\"}]} - an empty list "
        "when nothing is worth doing.\n\n"
        f"CURRENT SETUP:\n{setup}\n\n"
        + (f"LAST OUTLOOK:\n{last_report[:4000]}\n" if last_report else "")
    )


MAX_PROPOSALS = 3
_JSON_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def parse_proposals(report: str) -> tuple[str, list[dict[str, str]]]:
    """``(report without the json block, proposals)``; a broken block yields none."""
    match = None
    for match in _JSON_BLOCK.finditer(report):
        pass
    if match is None:
        return report, []
    text = (report[:match.start()] + report[match.end():]).strip()
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return text, []
    out = []
    for item in (data.get("proposals") or [])[:MAX_PROPOSALS] if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:150]
        body = str(item.get("body") or "").strip()
        if title and body:
            out.append({"title": title, "body": body[:20000]})
    return text, out


def _run_claude(prompt: str, repo_root: str, db_path: str | None = None) -> str:
    from modules.claude_worker import ClaudeWorker

    result = ClaudeWorker(db_path=db_path, web=True).run(
        task=prompt, working_dir=repo_root, sandbox="read-only",
        timeout_seconds=TIMEOUT_SECONDS, stall_seconds=STALL_SECONDS, phase="outlook")
    if not result.success:
        raise RuntimeError(result.error or "claude outlook failed")
    return result.final_message or ""


def run(db_path: str, repo_root: str, open_issue: Callable[[str, str], None],
        now: datetime | None = None, runner: Callable[[str, str], str] | None = None) -> str | None:
    """Every 14 days one outlook, reported as an issue. A notification line, or ``None``."""
    if not enabled():
        return None
    now = now or datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        last = _get(conn, LAST_AT)
        try:
            if last is not None and now - datetime.fromisoformat(last) < INTERVAL:
                return None
        except ValueError:
            pass
        with conn:
            _set(conn, LAST_AT, now.isoformat(), now)  # the interval counts from this attempt
        last_report = _get(conn, LAST_REPORT)
    finally:
        conn.close()
    runner = runner or (lambda prompt, root: _run_claude(prompt, root, db_path))
    try:
        report = runner(outlook_prompt(setup_summary(), last_report), repo_root).strip()
    except Exception as exc:  # noqa: BLE001 - reported, tried again in 14 days
        return f"Outlook failed: {str(exc)[:200]}"
    if not report:
        return "Outlook failed: empty report"
    title = f"{TITLE_PREFIX} {now.date().isoformat()}"
    report, proposals = parse_proposals(report)
    open_issue(title, report)
    # Owner 2026-11-28: each proposal becomes its own issue, a proposal like the
    # planner's (the owner can object; otherwise Nexus releases it after the wait).
    for proposal in proposals:
        open_issue(f"Outlook: {proposal['title']}",
                   f"{proposal['body']}\n\nFrom the outlook \"{title}\".")
    conn = _connect(db_path)
    try:
        with conn:
            _set(conn, LAST_REPORT, report[:6000], now)
    finally:
        conn.close()
    return f"Outlook: issue \"{title}\" opened with {len(proposals)} proposal issue(s)"
