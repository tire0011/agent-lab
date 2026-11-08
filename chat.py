"""Minimal, non-LLM chat for agent status/problems/ideas questions. Documented gap: no LLM-backed
chat is wired into the API yet - agent.py Azure OpenAI + MCP agent is a separate task-oriented CLI
runner and is intentionally NOT invoked here, because that would let /chat execute MCP tools or
shell-adjacent actions, which is explicitly disallowed for this endpoint. This module only does
keyword-based intent matching over the same structured data exposed by /status, /problems and /ideas,
so answers are always consistent with those endpoints; it never executes shell commands, tools or
arbitrary code, and never fabricates a general-purpose AI answer."""

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
