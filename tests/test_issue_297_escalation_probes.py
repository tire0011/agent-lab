"""#297: decision logic of the escalation probes (the live tests use the real model)."""

from __future__ import annotations

import json
import math

from modules.escalation_probes import (
    issue_text,
    memory_entry_text,
    logprob_decision,
)


def _response(token: str, probability: float, alternatives: dict[str, float] | None = None) -> dict:
    top = [{"token": t, "logprob": math.log(p)} for t, p in (alternatives or {token: probability}).items()]
    return {"choices": [{"logprobs": {"content": [{"token": token, "logprob": math.log(probability), "top_logprobs": top}]}}]}


def test_logprobs_confident_yes_stays_local():
    result = logprob_decision(_response("yes", 0.95))
    assert not result.escalate and result.details["probability"] == 0.95


def test_logprobs_no_escalates():
    assert logprob_decision(_response("no", 0.9)).escalate


def test_logprobs_unsure_yes_escalates():
    result = logprob_decision(_response("Yes", 0.55, {"Yes": 0.55, "No": 0.4}))
    assert result.escalate and "unsure" in result.reason
    assert result.details["alternatives"] == {"yes": 0.55, "no": 0.4}


def test_case_variants_are_added_up():
    result = logprob_decision(_response("Yes", 0.6, {"Yes": 0.6, " yes": 0.37, "No": 0.02}))
    assert not result.escalate and result.details["p_yes"] == 0.97


def test_missing_logprobs_is_reported_not_escalated():
    result = logprob_decision({"choices": [{"message": {"content": "yes"}}]})
    assert not result.escalate and result.details == {"available": False}


def test_issue_text_drops_the_metadata_line_and_chunk_prefixes():
    lines = [
        'issue: {"number": 7, "title": "Say \\"hi\\" first", "url": "[REMOTE]',
        "body[1]: first part ",
        "body[2]: second part",
    ]
    assert issue_text(lines) == 'GitHub issue #7: Say "hi" first\n\nfirst part second part'


def test_memory_entry_text_keeps_the_briefing_of_a_stored_collector_result():
    stored = json.dumps({"tool": "knowledge_collector", "knowledge_briefing": "facts", "used_sources": ["x"]})
    assert memory_entry_text(stored) == "facts"
    assert memory_entry_text("plain note") == "plain note"
    assert memory_entry_text('{"other": 1}') == '{"other": 1}'
