"""Probes: should a step go from the local model to a stronger one? (#297)

The local 7B model does not notice when it is wrong: in the #288 runs it
copied its own outdated summary from memory and was confident about it. These
probes test three concrete ways to detect such a step. They only *decide*;
nothing here calls an external model.

Only approach 3 worked in the live tests and is used in production by the
knowledge collector; 1 and 2 stay for the live measurements.

1. ``escalate`` action: the knowledge collector may answer ``escalate`` when
   fixed triggers hold (current source and memory disagree, ...).
2. Narrow yes/no question: "does the memory describe the same requirement as
   the current issue?" ``no`` escalates.
3. Measured confidence: the same question, asked directly with token
   log-probabilities; ``no`` or a low probability for the answer escalates.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from modules.llama_cpp_client import json_schema_response_format

# Below this probability for "yes" the model counts as unsure. Measured on 10
# live cases (#297): accurate memory gave p_yes >= 0.968, outdated <= 0.905.
LOGPROB_CONFIDENCE_THRESHOLD = 0.95


@dataclass
class ProbeResult:
    probe: str
    escalate: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- 1. escalate action ------------------------------------------------------

ESCALATE_COLLECTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["lookup", "briefing", "escalate"]},
        "source": {"type": "string", "enum": ["none", "memory", "read_only"]},
        "query": {"type": "string", "maxLength": 120},
        "briefing": {"type": "string", "maxLength": 5000},
        "reason": {"type": "string", "maxLength": 400},
    },
    "required": ["action", "source", "query", "briefing", "reason"],
    "additionalProperties": False,
}
ESCALATE_COLLECTOR_RESPONSE_FORMAT = json_schema_response_format(
    "nexus_knowledge_collector_escalate", ESCALATE_COLLECTOR_SCHEMA
)

ESCALATE_RULES = """
ESCALATION:
You may answer action=escalate instead of a briefing. Then source=none, query and
briefing are empty, and reason names the trigger. Escalate exactly when one of these holds:
- A current source (fetched now, e.g. a GitHub issue) and memory describe different
  requirements, titles, components or acceptance criteria for the same item.
- Two collected sources contradict each other on a fact the briefing would contain.
- The request needs a plan or design decision, not a summary of facts.
Otherwise return a normal briefing with reason empty.
"""


def escalate_collector_prompt(base_prompt: str) -> str:
    """The production collector prompt plus the escalation rules (approach 1)."""
    # The forced-briefing line would otherwise forbid the new action.
    base_prompt = base_prompt.replace(
        "Return action=briefing now.",
        "Return action=briefing now, or action=escalate when an escalation trigger holds.",
    )
    marker = "Return exactly one JSON object matching the schema."
    if marker in base_prompt:
        return base_prompt.replace(marker, ESCALATE_RULES.strip() + "\n" + marker)
    return base_prompt + "\n" + ESCALATE_RULES


def parse_escalate_collector(raw: str) -> ProbeResult:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return ProbeResult("escalate_action", False, f"unparseable answer: {exc}", {"raw": raw[:2000]})
    action = str(data.get("action", ""))
    return ProbeResult(
        "escalate_action",
        action == "escalate",
        str(data.get("reason") or f"action={action}")[:400],
        {"action": action, "briefing": str(data.get("briefing", ""))[:2000]},
    )


# --- 2. narrow yes/no question ----------------------------------------------

SAME_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}
SAME_REQUIREMENT_RESPONSE_FORMAT = json_schema_response_format(
    "nexus_same_requirement", SAME_REQUIREMENT_SCHEMA
)


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


def parse_same_requirement(raw: str) -> ProbeResult:
    try:
        answer = str(json.loads(raw).get("answer", "")).lower()
    except (TypeError, ValueError, AttributeError):
        answer = str(raw).strip().lower().strip('."')
    if answer not in {"yes", "no"}:
        return ProbeResult("yes_no", False, f"unclear answer {raw[:80]!r}", {"answer": answer})
    return ProbeResult(
        "yes_no",
        answer == "no",
        "memory differs from the current issue" if answer == "no" else "memory matches",
        {"answer": answer},
    )


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

async def ask_with_logprobs(
    base_url: str,
    model: str,
    prompt: str,
    *,
    top_logprobs: int = 5,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """One short completion straight to llama-server with token log-probabilities.

    Live-test helper: it bypasses the job queue, so run it while Nexus is
    idle. Production goes through the queue's ``top_logprobs`` option.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 2,
        "logprobs": True,
        "top_logprobs": top_logprobs,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload)
    response.raise_for_status()
    return response.json()


# --- combined ------------------------------------------------------------------

def combine(results: list[ProbeResult]) -> ProbeResult:
    """Escalate when any probe says so."""
    escalating = [result.probe for result in results if result.escalate]
    return ProbeResult(
        "combined",
        bool(escalating),
        ("escalate: " + ", ".join(escalating)) if escalating else "no probe escalates",
        {"probes": [result.as_dict() for result in results]},
    )
