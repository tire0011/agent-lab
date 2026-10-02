"""Experimental probes: should a step go from the local model to a stronger one? (#297)

The local 7B model does not notice when it is wrong: in the #288 runs it
copied its own outdated summary from memory and was confident about it. These
probes test three concrete ways to detect such a step. They only *decide* and
*log*; nothing here calls an external model. They are used by live tests, not
by any production path.

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
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from modules.llama_cpp_client import json_schema_response_format

# Below this probability for the chosen answer token the model counts as unsure.
LOGPROB_CONFIDENCE_THRESHOLD = 0.8


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

    The first generated token carries the answer. Its probability and the
    best alternatives come from ``choices[0].logprobs.content[0]``.
    """
    try:
        first = response["choices"][0]["logprobs"]["content"][0]
    except (KeyError, IndexError, TypeError):
        return ProbeResult("logprobs", False, "no logprobs in the response", {"available": False})
    token = str(first.get("token", "")).strip().lower()
    probability = math.exp(float(first.get("logprob", -math.inf)))
    alternatives = {
        str(item.get("token", "")).strip().lower(): round(math.exp(float(item.get("logprob", -math.inf))), 4)
        for item in first.get("top_logprobs") or []
    }
    details = {
        "available": True,
        "token": token,
        "probability": round(probability, 4),
        "alternatives": alternatives,
        "threshold": threshold,
    }
    if token.startswith("no"):
        return ProbeResult("logprobs", True, "answered no", details)
    if probability < threshold:
        return ProbeResult("logprobs", True, f"unsure: p={probability:.2f} < {threshold}", details)
    if not token.startswith("yes"):
        return ProbeResult("logprobs", True, f"unexpected token {token!r}", details)
    return ProbeResult("logprobs", False, f"answered yes with p={probability:.2f}", details)


async def ask_with_logprobs(
    base_url: str,
    model: str,
    prompt: str,
    *,
    top_logprobs: int = 5,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """One short completion straight to llama-server with token log-probabilities.

    The job queue does not carry logprobs yet, so this experiment talks to
    llama-server directly. Run it while Nexus is idle.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt + "\nAnswer with exactly one word: yes or no."}],
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
