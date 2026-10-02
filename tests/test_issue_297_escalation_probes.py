"""#297: decision logic of the escalation probes (the live tests use the real model)."""

from __future__ import annotations

import json
import math

from modules.escalation_probes import (
    ProbeResult,
    combine,
    escalate_collector_prompt,
    logprob_decision,
    parse_escalate_collector,
    parse_same_requirement,
)


def _response(token: str, probability: float, alternatives: dict[str, float] | None = None) -> dict:
    top = [{"token": t, "logprob": math.log(p)} for t, p in (alternatives or {token: probability}).items()]
    return {"choices": [{"logprobs": {"content": [{"token": token, "logprob": math.log(probability), "top_logprobs": top}]}}]}


def test_escalate_action_is_parsed():
    raw = json.dumps({"action": "escalate", "source": "none", "query": "", "briefing": "", "reason": "memory differs"})
    result = parse_escalate_collector(raw)
    assert result.escalate and result.reason == "memory differs"


def test_a_briefing_does_not_escalate():
    raw = json.dumps({"action": "briefing", "source": "none", "query": "", "briefing": "facts", "reason": ""})
    assert not parse_escalate_collector(raw).escalate


def test_escalation_rules_go_before_the_closing_instruction():
    prompt = escalate_collector_prompt("BODY\nReturn exactly one JSON object matching the schema.")
    assert prompt.index("ESCALATION:") < prompt.index("Return exactly one JSON object")


def test_the_forced_briefing_line_allows_escalation():
    prompt = escalate_collector_prompt("Return action=briefing now.\nReturn exactly one JSON object matching the schema.")
    assert "or action=escalate" in prompt


def test_yes_no_answers():
    assert parse_same_requirement('{"answer":"no"}').escalate
    assert not parse_same_requirement('{"answer":"yes"}').escalate
    assert not parse_same_requirement("maybe").escalate


def test_logprobs_confident_yes_stays_local():
    result = logprob_decision(_response("yes", 0.95))
    assert not result.escalate and result.details["probability"] == 0.95


def test_logprobs_no_escalates():
    assert logprob_decision(_response("no", 0.9)).escalate


def test_logprobs_unsure_yes_escalates():
    result = logprob_decision(_response("Yes", 0.55, {"Yes": 0.55, "No": 0.4}))
    assert result.escalate and "unsure" in result.reason
    assert result.details["alternatives"] == {"yes": 0.55, "no": 0.4}


def test_missing_logprobs_is_reported_not_escalated():
    result = logprob_decision({"choices": [{"message": {"content": "yes"}}]})
    assert not result.escalate and result.details == {"available": False}


def test_combined_escalates_when_any_probe_does():
    quiet = ProbeResult("a", False, "")
    loud = ProbeResult("b", True, "x")
    assert combine([quiet, loud]).escalate and combine([quiet, loud]).reason == "escalate: b"
    assert not combine([quiet]).escalate
