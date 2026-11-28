"""Minimal, non-LLM chat for agent status/problems/ideas questions.

IMPORTANT / documented gap: there is no LLM-backed chat wired into the API
yet. agent.py's Azure OpenAI + MCP agent is a separate, task-oriented CLI
runner (see agent.py, run-agent.sh) and is intentionally NOT invoked here,
because doing so would let /chat execute MCP tools / shell-adjacent actions,
which is explicitly disallowed for this endpoint.

This module only does keyword-based intent matching over the same
structured data exposed by /status, /problems, and /ideas, so answers are
always consistent with those endpoints. It never executes shell commands,
tools, or arbitrary code, and never fabricates a general-purpose AI answer.
"""

from __future__ import annotations

import os

from nexus_version import VERSION


API_VERSION = VERSION
COMPONENTS = ["health", "status", "problems", "ideas", "chat"]


def build_status() -> dict:
    return {
        "agent_status": "idle",
        "api_version": API_VERSION,
        "components": COMPONENTS,
        "home_gpu": _home_gpu_status(),
    }


def _home_gpu_status() -> dict:
    """The ``home_gpu`` block of ``/status`` (#761): PC-off state, VM night
    window and the savings summary, read from the run database read-only.

    The database path is resolved at call time (tests point ``AGENT_DB_PATH``
    at a temporary file) and never appears in the result. Any failure - no
    database yet, unreadable file - degrades to the flag alone instead of
    failing the status endpoint.
    """
    from modules.home_gpu.fallback import home_gpu_status
    from modules.home_gpu.jobs import home_gpu_enabled

    db_path = (os.environ.get("AGENT_DB_PATH") or "").strip() or "/var/lib/nexus/agent.db"
    try:
        return home_gpu_status(db_path)
    except Exception:  # noqa: BLE001 - /status must never fail on this block
        return {"enabled": home_gpu_enabled(), "available": False}
