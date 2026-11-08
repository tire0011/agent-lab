"""Probes deciding whether a step escalates from the local model to a stronger one (#297).

Three ways to detect an overconfident-but-wrong local answer (the #288 runs):
an ``escalate`` action, a narrow yes/no question, and a measured-confidence
logprob question. Only the logprob approach (3) is used in production; these
only decide, they never call an external model themselves.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any



# Below this p_yes the model counts as unsure; measured on 10 live cases (#297):
# accurate memory >= 0.968, outdated <= 0.905.
LOGPROB_CONFIDENCE_THRESHOLD = 0.95


@dataclass
class ProbeResult:
    probe: str
    escalate: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def same_requirement_prompt(current: str, memory: str) -> str:
    return (
        "Compare two texts about the same work item.\n\n"
        f"CURRENT ISSUE (authoritative, fetched now):\n{current[:4000]}\n\n"
        f"MEMORY (written earlier):\n{memory[:3000]}\n\n"
        "Question: Does MEMORY describe the same requirement as CURRENT ISSUE "
        "(same goal, same component, same acceptance criteria)? "
        "Answer yes or no."
    )


def issue_text(output_lines: list[str]) -> str:
    """``GitHub issue #N: <title>`` and the body, from github_issue output lines.

    The raw lines carry a JSON metadata line and ``body[i]:`` chunk prefixes;
    in the live #288 run that framing raised p_yes for outdated memory.
    """
    heading = ""
    body: list[str] = []
    for line in output_lines:
        line = str(line)
        if line.startswith("issue: "):
            number = re.search(r'"number":\s*(\d+)', line)
            title = re.search(r'"title":\s*"((?:[^"\\]|\\.)*)"', line)
            if number and title:
                title_text = json.loads('"' + title.group(1) + '"')
                heading = f"GitHub issue #{number.group(1)}: {title_text}"
        else:
            chunk = re.match(r"body\[\d+\]: ?(.*)", line, re.DOTALL)
            body.append(chunk.group(1) if chunk else line)
    return (heading + "\n\n" + "".join(body)).strip()


def memory_entry_text(content: str) -> str:
    """The prose of one memory hit: a stored collector result keeps its briefing."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return str(content).strip()
    if isinstance(data, dict) and data.get("knowledge_briefing"):
        return str(data["knowledge_briefing"]).strip()
    return str(content).strip()


def same_requirement_logprob_prompt(current: str, memory: str) -> str:
    """The yes/no question for approach 3: the answer must be the first token."""
    return same_requirement_prompt(current, memory) + "\nAnswer with exactly one word: yes or no."


# --- 3. measured confidence (logprobs) ---------------------------------------

def logprob_decision(response: dict[str, Any], *, threshold: float = LOGPROB_CONFIDENCE_THRESHOLD) -> ProbeResult:
    """Decide from a chat completion requested with ``logprobs``/``top_logprobs``.

    The first generated token carries the answer. Its top alternatives come
    from ``choices[0].logprobs.content[0]``; case and spacing variants of
    yes/no are added up before deciding.
    """
    try:
        first = response["choices"][0]["logprobs"]["content"][0]
    except (KeyError, IndexError, TypeError):
        return ProbeResult("logprobs", False, "no logprobs in the response", {"available": False})
    token = str(first.get("token", "")).strip().lower()
    probability = math.exp(float(first.get("logprob", -math.inf)))
    # "Yes", " yes" and "yes" are different tokens; add them up per answer.
    raw_top = [
        (str(item.get("token", "")), round(math.exp(float(item.get("logprob", -math.inf))), 4))
        for item in first.get("top_logprobs") or []
    ]
    alternatives: dict[str, float] = {}
    for raw_token, p in raw_top:
        key = raw_token.strip().lower()
        alternatives[key] = round(alternatives.get(key, 0.0) + p, 4)
    details = {
        "available": True,
        "token": token,
        "probability": round(probability, 4),
        "p_yes": alternatives.get("yes", 0.0),
        "p_no": alternatives.get("no", 0.0),
        "alternatives": alternatives,
        "top_tokens": [repr(t) for t, _ in raw_top],
        "threshold": threshold,
    }
    p_yes, p_no = details["p_yes"], details["p_no"]
    if p_no >= p_yes:
        return ProbeResult("logprobs", True, f"leans no: p_yes={p_yes:.2f} p_no={p_no:.2f}", details)
    if p_yes < threshold:
        return ProbeResult("logprobs", True, f"unsure: p_yes={p_yes:.2f} < {threshold}", details)
    return ProbeResult("logprobs", False, f"yes with p_yes={p_yes:.2f}", details)

