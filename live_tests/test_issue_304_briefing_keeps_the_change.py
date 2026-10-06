"""#304: the collector's briefing keeps what the issue asks to change.

In the #301 run the briefing said "Wanted: External fallback logs, ...", the
request to *add* logging was lost, and the controller went looking for logs
instead of starting an experiment. This runs the real collector prompt on the
#301 issue, then one controller turn on its briefing. Nothing is executed.
"""

from __future__ import annotations

import asyncio
import json

from live_tests.conftest import live_budget
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, _KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT

ISSUE_301_TITLE = "Log the answers of the models in the knowledge collector escalation"
ISSUE_301_BODY = """## Observed

In the live #288 run after #299 (session `9e78d0d6…`, 2026-10-03 12:39), the knowledge collector escalated (#297) to the external fallback chain:

```
knowledge_collector_external_fallback: provider=azure_openai success=False
knowledge_collector_external_fallback: provider=codex_readonly success=True
knowledge_escalation ... unsure: p_yes=0.95 < 0.95 provider=codex_readonly failed=False
```

It is impossible to tell from the logs or the task events what went wrong:
- why `azure_openai` failed (no HTTP status, error or detail is logged; `DelegationResult.metadata` has them but is dropped)
- what `codex_readonly` actually answered (it was valid JSON but not a briefing)
- the local probe answers per memory entry, which are only visible as raw job results in `local_llm_jobs`

## Wanted

Make every model answer in this path visible, so a failed or odd escalation can be diagnosed from the task events and logs alone:

1. **External fallback (`_external_fallback_completion`):** for each provider attempt, log and keep the outcome:
   - provider and success
   - for a failure, the error metadata (`error`, `http_status`, `error_detail`, …), without secrets
   - for a success, the start of the answer (bounded, e.g. 1500 characters)
2. **Knowledge collection event:** the `knowledge_collection` metadata (or a dedicated event) records:
   - the attempts above
   - for an escalated answer that was rejected, the start of that answer
3. **Memory check:** per memory entry, record the start of the entry, its p_yes/p_no and the answer token, next to `entries_p_yes`.
4. Anything else in this path that is needed to tell why an escalation did or did not help.

Keep logged text bounded and run it through the existing redaction; never log API keys.

## Acceptance criteria

- A failed external provider leaves its error reason in the task events.
- A successful external answer that is rejected leaves its beginning in the task events.
- Per-entry probe results are visible in the `knowledge_collection` event.
- Tests cover the three cases.
"""

ISSUE_301 = {
    "tool": "operation",
    "operation": "github_issue",
    "result": {
        "operation": "github_issue",
        "success": True,
        "output_lines": [
            "issue: " + json.dumps({"number": 301, "title": ISSUE_301_TITLE, "state": "open"}),
            "body[1]: " + ISSUE_301_BODY,
        ],
    },
}


def test_301_briefing_leads_to_new_experiment(orchestrator, controller_turn, live_result):
    task = "solve github issue #301"
    session = orchestrator.task_store.get_or_create(source_type="task", source_id="live-304", title=task)
    budget = live_budget()
    prompt = orchestrator._knowledge_collector_prompt(
        orchestrator._knowledge_request(task), [ISSUE_301], budget, force_briefing=True
    )
    raw, _provider = asyncio.run(
        orchestrator._schema_completion(
            prompt,
            session_id=session.id,
            max_tokens=650,
            response_format=_KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
            budget_plan=budget,
            role="knowledge_collector",
        )
    )
    briefing = orchestrator._parse_knowledge_collector(raw)["briefing"]
    observation = {
        "tool": "knowledge_collector",
        "knowledge_briefing": briefing,
        "used_sources": ["github_issue"],
        "source_count": 1,
        "deterministic_retrieval": True,
    }
    decision = controller_turn(task, [observation])
    action = str(decision.get("action", "")).strip().lower()
    live_result.update(scenario="301-real-briefing", briefing=briefing, action=action)
    assert action == ACTION_NEW_EXPERIMENT, f"briefing: {briefing!r}; got {action!r}"
