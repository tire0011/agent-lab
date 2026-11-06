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

from nexus_version import VERSION


API_VERSION = VERSION
COMPONENTS = ["health", "status", "problems", "ideas", "chat"]


def build_status() -> dict:
    return {
        "agent_status": "idle",
        "api_version": API_VERSION,
        "components": COMPONENTS,
    }
