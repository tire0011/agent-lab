"""#297 follow-up: check memory entries one by one instead of joined.

In the live #288 run after #298 the probe saw the 8 real memory hits joined
into one text and answered p_yes 0.961, just above the 0.95 threshold. The
hits are earlier collector briefings, two of them already headed with the
current title (#292). This file measures, on those real hits, the joined
check as production ran it against a per-entry check (duplicates removed,
plain task inputs skipped, lowest p_yes wins), on the raw texts and on the
cleaned texts (issue title + body, briefing prose only).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from live_tests.test_issue_297_escalation_probes import (
    ISSUE_288_BODY,
    ISSUE_288_TITLE,
    ISSUE_289_BODY,
    ISSUE_289_MEMORY,
    ISSUE_289_TITLE,
    _collector_sources,
)
from modules.escalation_probes import (
    LOGPROB_CONFIDENCE_THRESHOLD,
    ask_with_logprobs,
    issue_text,
    logprob_decision,
    memory_entry_text,
    same_requirement_logprob_prompt,
)

_OLD = (
    "An experiment interrupted by a restart should be resumed, not end the task. Known root cause: "
    "Interruption by service restart. Prior attempts: Fixing the issue by starting a new experiment directly. "
    "Acceptance criteria: A task whose experiment was interrupted by a restart gets a new experiment "
    "automatically and can still succeed. The automatic restart happens at most once per interrupted "
    "experiment and does not use up the 2-experiment limit. Other failures are still handed to Nexus. "
    "Tests cover all three cases."
)
_OLD_BODY = _OLD.split(". ", 1)[1]


def _briefing_entry(briefing: str) -> dict:
    return {"kind": "knowledge_collector", "content": json.dumps({"tool": "knowledge_collector", "knowledge_briefing": briefing})}


# The 8 hits memory.search("GitHub issue #288") returned on the VM (2026-10-03).
HITS_288 = [
    _briefing_entry(f"GitHub issue #288: {ISSUE_288_TITLE}\nGitHub issue #288: {_OLD}"),
    _briefing_entry(f"GitHub issue #288: {ISSUE_288_TITLE}. {_OLD_BODY}"),
    *[_briefing_entry(f"GitHub issue #288: {_OLD}") for _ in range(4)],
    {"kind": "input", "content": "solve github issue #288"},
    {"kind": "input", "content": "solve github issue #288"},
]
HITS_289 = [
    _briefing_entry(ISSUE_289_MEMORY),
    _briefing_entry(ISSUE_289_MEMORY),
    {"kind": "input", "content": "solve github issue #289"},
]

CASES = {
    "288": {"number": 288, "title": ISSUE_288_TITLE, "body": ISSUE_288_BODY, "hits": HITS_288, "expect": True},
    "289": {"number": 289, "title": ISSUE_289_TITLE, "body": ISSUE_289_BODY, "hits": HITS_289, "expect": False},
}


def _issue_lines(case: dict) -> list[str]:
    # The fetched issue as the production probe sees it: the operation's output lines.
    return _collector_sources({**case, "memory": ""})[1]["result"]["output_lines"]


def _issue_text(case: dict) -> str:
    return "\n".join(_issue_lines(case))


def _distinct_entries(hits: list[dict]) -> list[str]:
    seen: list[str] = []
    for hit in hits:
        text = str(hit.get("content", "")).strip()
        if hit.get("kind") == "input" or not text or text in seen:
            continue
        seen.append(text)
    return seen


def _probe(llama_server: dict, issue: str, memory: str) -> dict:
    started = time.monotonic()
    response = asyncio.run(
        ask_with_logprobs(llama_server["url"], llama_server["model"], same_requirement_logprob_prompt(issue, memory))
    )
    result = logprob_decision(response)
    return {
        "p_yes": result.details.get("p_yes"),
        "p_no": result.details.get("p_no"),
        "escalate": result.escalate,
        "seconds": round(time.monotonic() - started, 1),
    }


@pytest.mark.parametrize("case_name", list(CASES))
def test_joined_vs_per_entry(llama_server, live_result, case_name):
    case = CASES[case_name]
    issue = _issue_text(case)
    joined = _probe(llama_server, issue, "\n\n".join(str(hit["content"]) for hit in case["hits"]))
    entries = _distinct_entries(case["hits"])
    per_entry = [_probe(llama_server, issue, entry) for entry in entries]
    lowest = min(item["p_yes"] for item in per_entry)
    # Proposed production input: title + body, and only the briefing prose.
    clean_issue = issue_text(_issue_lines(case))
    per_entry_clean = [_probe(llama_server, clean_issue, memory_entry_text(entry)) for entry in entries]
    lowest_clean = min(item["p_yes"] for item in per_entry_clean)
    live_result.update(
        per_entry_clean=per_entry_clean,
        lowest_clean_p_yes=lowest_clean,
        case=case_name,
        threshold=LOGPROB_CONFIDENCE_THRESHOLD,
        joined=joined,
        per_entry=per_entry,
        distinct_entries=len(entries),
        lowest_p_yes=lowest,
    )
    assert (lowest_clean < LOGPROB_CONFIDENCE_THRESHOLD) == case["expect"], live_result
