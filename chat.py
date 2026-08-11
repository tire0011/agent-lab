"""Minimal, non-LLM chat for agent status/problems/todos questions.

IMPORTANT / documented gap: there is no LLM-backed chat wired into the API
yet. agent.py's Azure OpenAI + MCP agent is a separate, task-oriented CLI
runner (see agent.py, run-agent.sh) and is intentionally NOT invoked here,
because doing so would let /chat execute MCP tools / shell-adjacent actions,
which is explicitly disallowed for this endpoint.

This module only does keyword-based intent matching over the same
structured data exposed by /status, /problems, and /todos, so answers are
always consistent with those endpoints. It never executes shell commands,
tools, or arbitrary code, and never fabricates a general-purpose AI answer.
"""

from __future__ import annotations

from data_sources import list_problems, list_todos

API_VERSION = "0.2.0"
COMPONENTS = ["health", "status", "problems", "todos", "chat"]


def build_status() -> dict:
    return {
        "agent_status": "idle",
        "api_version": API_VERSION,
        "components": COMPONENTS,
    }


def _format_problems() -> str:
    problems = list_problems()
    if not problems:
        return "Aktuell sind keine Probleme erkannt."
    lines = [f"- {p.summary}" for p in problems[:10]]
    return f"Es sind {len(problems)} Problem(e) erkannt:\n" + "\n".join(lines)


def _format_todos() -> str:
    todos = list_todos()
    if not todos:
        return "Aktuell stehen keine offenen Aufgaben an."
    lines = [f"- [{t.section}] {t.text}" for t in todos[:10]]
    return f"Es stehen {len(todos)} offene Aufgabe(n) an:\n" + "\n".join(lines)


def answer(message: str) -> tuple[str, str]:
    """Return (reply, matched_intent) for a chat message.

    Rule-based only: keyword matching, no code execution.
    """
    text = message.strip().lower()

    if any(kw in text for kw in ("problem", "fehler", "issue")):
        return _format_problems(), "problems"

    if any(kw in text for kw in ("todo", "aufgabe", "next", "nächst", "naechst")):
        return _format_todos(), "todos"

    if any(kw in text for kw in ("status", "version", "komponente")):
        status = build_status()
        reply = (
            f"Agentstatus: {status['agent_status']}, "
            f"API-Version: {status['api_version']}, "
            f"Komponenten: {', '.join(status['components'])}."
        )
        return reply, "status"

    return (
        "Ich bin ein minimaler, regelbasierter Chat (kein LLM). "
        "Ich kann Fragen zu Status, Problemen und offenen Aufgaben "
        "beantworten, z.B. 'Welche Probleme gibt es?' oder "
        "'Was steht als Nächstes an?'.",
        "unhandled",
    )
