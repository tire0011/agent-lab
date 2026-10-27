"""What a Nexus ``new_experiment`` hands to the coding agent (#263).

Before this the experiment started from the bare task text: whatever Nexus
had already learned in the run - the knowledge briefing, a delegate's
analysis, what it had tried - was left behind, and the coding agent had to
rediscover it.
"""

from __future__ import annotations

from typing import Any

from modules.redaction import redact_secrets

MAX_HANDOVER_CHARS = 12000
HANDOVER_HEADING = "What Nexus already found (handover):"
_MAX_SECTION_ITEM_CHARS = 1500
_MAX_ATTEMPT_CHARS = 300


def _clip(text: Any, limit: int) -> str:
    # Redact before cutting: a secret split at the limit no longer matches
    # its configured literal value, and its prefix would be kept.
    value = redact_secrets(str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def experiment_handover(nexus_goal: Any, observations: list[dict[str, Any]]) -> str:
    """Nexus' own goal and what the run found, to append to the task.

    The task itself is left to the caller, which knows its full form (the
    original request plus any clarification replies). Empty when the run
    found nothing to hand over.
    """
    sections: list[str] = []
    goal = _clip(nexus_goal, 2000)
    if goal:
        sections.append(f"Nexus goal:\n{goal}")

    briefings: list[str] = []
    findings: list[str] = []
    attempts: list[str] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        tool = str(observation.get("tool", ""))
        if tool == "knowledge_collector" and observation.get("knowledge_briefing"):
            briefings.append(_clip(observation["knowledge_briefing"], _MAX_SECTION_ITEM_CHARS))
        elif tool == "delegate":
            result = observation.get("result")
            content = result.get("content") if isinstance(result, dict) else None
            if content:
                findings.append(_clip(content, _MAX_SECTION_ITEM_CHARS))
        elif tool in {"command", "python_run"}:
            what = observation.get("command") or observation.get("target") or observation.get("args")
            outcome = observation.get("reason") or observation.get("error") or observation.get("result")
            attempts.append(
                f"- {tool} {_clip(what, _MAX_ATTEMPT_CHARS)}: {_clip(outcome, _MAX_ATTEMPT_CHARS)}"
            )

    if briefings:
        sections.append("Knowledge briefing:\n" + "\n\n".join(briefings))
    if findings:
        sections.append("Delegate findings (unverified):\n" + "\n\n".join(findings))
    if attempts:
        sections.append("Earlier attempts in this run:\n" + "\n".join(attempts))
    if not sections:
        return ""
    handover = HANDOVER_HEADING + "\n\n" + "\n\n".join(sections)
    return _clip(handover, MAX_HANDOVER_CHARS)


def with_handover(goal: str, handover: str | None) -> str:
    """The experiment goal: the caller's task text followed by the handover."""
    return f"{goal}\n\n{handover}" if handover else goal
