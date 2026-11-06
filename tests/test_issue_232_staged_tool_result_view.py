"""#232: a staged controller view for large tool results, and paging back into one.

Compaction used to have two rungs - the whole result, or a bare stub saying a
call succeeded. Between them now sits a semantic summary: for an issue its
title, state, goal and acceptance criteria. The stub remains for when even the
summary does not fit. ``read_tool_result`` pages the stored raw result back by
event id, and the loop guard reads distinct pages as progress.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.controller_observations import controller_observation_view
from modules.nexus_task_store import NexusTaskStore
from modules.operations_registry import OperationArgSpec, OperationSpec
from modules.prompt_budget import compact_observations
from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator
from modules.tool_loop_guard import PersistentToolLoopGuard
from modules.tool_result_view import (
    TOOL_READ_TOOL_RESULT,
    normalize_read_arguments,
    read_tool_result_slice,
    semantic_summary,
)

GOAL = "Publish Nexus Outbox messages to the communication PR so humans can read them."
ACCEPTANCE = "- every outbox message appears in the PR\n- publishing is idempotent"
DETAIL_MARKER = "implementation detail line"
LARGE_DETAIL_LINES = 250


def _issue_body(detail_lines: int = 60) -> str:
    details = "\n".join(
        f"{DETAIL_MARKER} {index}: " + "x" * 90 for index in range(detail_lines)
    )
    return (
        f"## Goal\n{GOAL}\n\n## Background\n{details}\n\n"
        f"## Acceptance criteria\n{ACCEPTANCE}\n"
    )


def _issue_output_lines(detail_lines: int = 60) -> list[str]:
    """The production shape of ``github_issue`` output (operations_registry)."""
    metadata = {
        "number": 196,
        "title": "Publish Nexus Outbox messages to communication PR",
        "state": "OPEN",
        "labels": ["nexus"],
    }
    lines = [f"issue: {json.dumps(metadata, sort_keys=True)}"]
    body = _issue_body(detail_lines)
    for index in range(0, len(body), 450):
        lines.append(f"body[{index // 450 + 1}]: {body[index:index + 450]}")
    return lines[:80]


def _large_issue_observation(event_id: int = 158) -> dict:
    return {
        "tool": "operation",
        "operation": "github_issue",
        "args": {"issue_number": 196},
        "grant_event_id": 156,
        "result": {
            "operation": "github_issue",
            "success": True,
            "output_lines": _issue_output_lines(),
            "duration_ms": 412,
            "exit_code": 0,
        },
        "event_id": event_id,
    }


class SemanticSummaryRungTests(unittest.TestCase):
    BUDGET = 1500

    def _compacted(self, budget: int):
        views = controller_observation_view([_large_issue_observation()])
        self.assertGreater(len(json.dumps(views)), budget * 3, "the fixture must not fit")
        return compact_observations(views, budget_chars=budget, keep_recent_full=2)

    def test_a_large_result_is_summarised_rather_than_stubbed(self):
        compacted, omitted = self._compacted(self.BUDGET)

        self.assertEqual(omitted, [], "a summary carries content; it is not a stub")
        view = compacted[0]
        self.assertTrue(view["summarized"])
        self.assertNotIn("truncated", view)
        self.assertLessEqual(len(json.dumps(view, separators=(",", ":"))), self.BUDGET)
        # The bulk is gone from the prompt.
        self.assertNotIn(DETAIL_MARKER, json.dumps(view))

    def test_the_summary_keeps_the_decision_relevant_fields(self):
        compacted, _ = self._compacted(self.BUDGET)
        view = compacted[0]
        summary = view["summary"]

        self.assertEqual(summary["title"], "Publish Nexus Outbox messages to communication PR")
        self.assertEqual(summary["state"], "OPEN")
        self.assertEqual(summary["number"], 196)
        self.assertEqual(summary["goal"], GOAL)
        self.assertEqual(summary["acceptance_criteria"], ACCEPTANCE)
        # Still a call the controller can recognise as already made.
        self.assertEqual(view["operation"], "github_issue")
        self.assertEqual(view["args"], {"issue_number": 196})
        self.assertEqual(view["status"], "succeeded")
        self.assertEqual(view["event_id"], 158)
        # Orchestration noise stays stripped.
        self.assertNotIn("grant_event_id", view)
        # It says where the rest is and how to read it, by sections that exist.
        self.assertIn("read_tool_result", view["note"])
        self.assertIn("158", view["note"])
        self.assertIn("Acceptance criteria", view["sections"])

    def test_the_bare_stub_remains_only_when_even_the_summary_does_not_fit(self):
        compacted, omitted = self._compacted(150)

        self.assertEqual(omitted, [0])
        stub = compacted[0]
        self.assertTrue(stub["truncated"])
        self.assertNotIn("summary", stub)
        self.assertEqual(stub["operation"], "github_issue")
        self.assertEqual(stub["args"], {"issue_number": 196})
        self.assertEqual(stub["status"], "succeeded")
        self.assertEqual(stub["event_id"], 158)
        self.assertIn("not in this prompt", stub["note"])

    def test_a_log_keeps_its_head_tail_and_failure_lines(self):
        log = "\n".join(
            ["collected 300 items"]
            + [f"tests/test_{i}.py ." for i in range(150)]
            + ["E   AssertionError: expected 3, got 4"]
            + [f"tests/test_more_{i}.py ." for i in range(150)]
            + ["1 failed, 299 passed in 12.3s"]
        )
        obs = {"tool": "command", "command": "pytest", "result": log, "event_id": 7}

        view = semantic_summary(obs, 1500)

        self.assertIsNotNone(view)
        summary = view["summary"]
        self.assertEqual(summary["head"][0], "collected 300 items")
        self.assertEqual(summary["tail"][-1], "1 failed, 299 passed in 12.3s")
        self.assertTrue(any("AssertionError" in line for line in summary["signal_lines"]))
        self.assertEqual(view["command"], "pytest")

    def test_an_unstructured_blob_falls_through_to_the_old_rungs(self):
        self.assertIsNone(semantic_summary({"tool": "command", "result": "y" * 5000}, 2000))


class ReadToolResultSliceTests(unittest.TestCase):
    def test_a_named_section_is_returned_from_the_stored_result(self):
        page = read_tool_result_slice(
            _large_issue_observation(), event_id=158, section="acceptance criteria"
        )

        self.assertEqual(page["tool"], TOOL_READ_TOOL_RESULT)
        self.assertEqual(page["result_event_id"], 158)
        self.assertEqual(page["section"], "Acceptance criteria")
        self.assertEqual(page["lines"][1:], ACCEPTANCE.splitlines())
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["source_operation"], "github_issue")

    def test_offset_and_limit_page_deterministically_and_without_gaps(self):
        stored = _large_issue_observation()
        first = read_tool_result_slice(stored, event_id=158, offset=0, limit=10)
        again = read_tool_result_slice(stored, event_id=158, offset=0, limit=10)
        second = read_tool_result_slice(
            stored, event_id=158, offset=first["next_offset"], limit=10
        )

        self.assertEqual(first, again)
        self.assertEqual(first["returned_lines"], 10)
        self.assertEqual(first["next_offset"], 10)
        self.assertNotEqual(first["lines"], second["lines"])
        # Paging to the end reassembles the whole document, in order.
        pages, offset = [], 0
        while offset is not None:
            page = read_tool_result_slice(stored, event_id=158, offset=offset, limit=25)
            pages.extend(page["lines"])
            offset = page["next_offset"]
        self.assertEqual(len(pages), first["total_lines"])
        self.assertIn(GOAL, pages)

    def test_a_page_is_bounded_in_characters_too(self):
        page = read_tool_result_slice(
            _large_issue_observation(), event_id=158, offset=0, limit=200, max_chars=1000
        )
        self.assertLessEqual(sum(len(line) + 1 for line in page["lines"]), 1000)
        self.assertIsNotNone(page["next_offset"])

    def test_an_unknown_section_lists_the_ones_that_exist(self):
        page = read_tool_result_slice(_large_issue_observation(), event_id=158, section="nope")
        self.assertEqual(page["error"], "section_not_found")
        self.assertIn("Goal", page["sections"])

    def test_reading_never_changes_the_stored_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NexusTaskStore(str(Path(tmp) / "state.db"))
            try:
                session = store.get_or_create(source_type="task", source_id="232")
                content = json.dumps(_large_issue_observation(), separators=(",", ":"))
                event_id = store.append_event(
                    session.id, role="tool", kind="operation", content=content
                )
                before = store.get_event(session.id, event_id)
                read_tool_result_slice(json.loads(before["content"]), event_id=event_id)
                self.assertEqual(store.get_event(session.id, event_id), before)
                # Another session's journal is not readable through this one.
                other = store.get_or_create(source_type="task", source_id="other")
                self.assertIsNone(store.get_event(other.id, event_id))
            finally:
                store.close()


class PagingIsNotALoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.guard = PersistentToolLoopGuard(str(Path(self.tmp.name) / "guard.db"))
        self.addCleanup(self.guard.close)

    def _check(self, **page):
        return self.guard.check(
            "session",
            TOOL_READ_TOOL_RESULT,
            normalize_read_arguments({"event_id": 158, **page}),
            state_token="state",
            reason="reading the next page",
        )

    def test_successive_different_slices_do_not_trip_the_guard(self):
        for offset in range(0, 200, 40):
            check = self._check(offset=offset, limit=40)
            self.assertTrue(check.allowed, f"page at offset {offset} was refused")
            self.guard.record_result(check.call_id, {"offset": offset})
        self.assertTrue(self._check(section="Goal").allowed)

    def test_the_same_page_spelled_differently_is_the_same_call(self):
        first = self._check(offset=0)
        self.guard.record_result(first.call_id, {})
        repeat = self._check(offset="0", limit=40)
        self.assertFalse(repeat.allowed)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        return response(prompt) if callable(response) else response


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls: list[tuple[str, dict]] = []

    def catalog(self):
        return [
            OperationSpec(
                name="github_issue",
                description="Read one GitHub issue: title, body, state.",
                arguments=[
                    OperationArgSpec("issue_number", "integer", "Issue number.", required=True)
                ],
                tags=["github", "issue", "read"],
                mutates_state=False,
            )
        ]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, dict(args)))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                # Larger than the default controller observation budget, still
                # within github_issue's 80 output lines.
                "output_lines": _issue_output_lines(LARGE_DETAIL_LINES),
                "duration_ms": 1,
            }
        )


class _ExperimentService:
    def resolve_experiment(self, experiment_id: str):
        raise KeyError(experiment_id)


def _summarized_event_id(prompt: str) -> int:
    match = re.search(r'"event_id":(\d+)[^{}]*?"summarized":true', prompt)
    assert match, "the controller prompt must carry the summary and its event id"
    return int(match.group(1))


def _read(offset: int):
    def respond(prompt: str) -> str:
        return json.dumps({
            "action": TOOL_READ_TOOL_RESULT,
            "event_id": _summarized_event_id(prompt),
            "offset": offset,
            "limit": 5,
            "reason": "reading the background section page by page",
        })
    return respond


class ControllerPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_controller_pages_a_summarised_result_without_tripping_the_guard(self):
        registry = _Registry(str(Path(self.tmp.name) / "state.db"), self.tmp.name)
        client = _Client([
            '{"action":"find_tool","goal":"read GitHub issue 196","reason":"need the issue"}',
            '{"status":"found","operation":"github_issue","reason":"reads the issue",'
            '"arguments":{"issue_number":196},"related_tools":[]}',
            _read(0),
            _read(5),
            _read(5),  # the same page again: refused, with paging-aware recovery
            '{"action":"answer","message":"Read it.","reason":"done"}',
        ])
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=_ExperimentService(),
            llama_client=client,
        )
        try:
            result = orchestrator.run(
                "summarize issue 196", source_type="task", source_id="issue-232-paging"
            )
        finally:
            orchestrator.close()

        output_lines = _issue_output_lines(LARGE_DETAIL_LINES)
        self.assertLess(len(output_lines), 80)
        self.assertIn("publishing is idempotent", "".join(output_lines))
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(client.responses, [])
        self.assertEqual(len(registry.calls), 1, "paging never re-runs the operation")

        controller_prompts = [p for p in client.prompts if "TOOL RESOLVER MODE" not in p]
        first_after_issue = controller_prompts[1]
        # Rung 3: the large issue arrives as a summary, not a stub and not whole.
        self.assertIn('"summarized":true', first_after_issue)
        self.assertIn(GOAL, first_after_issue)
        self.assertIn("every outbox message appears in the PR", first_after_issue)
        self.assertNotIn(f"{DETAIL_MARKER} 40", first_after_issue)

        pages = [
            obs for obs in result.observations
            if obs.get("tool") == TOOL_READ_TOOL_RESULT and "lines" in obs
        ]
        self.assertEqual([page["offset"] for page in pages], [0, 5])
        self.assertNotEqual(pages[0]["lines"], pages[1]["lines"])
        issue_event = pages[0]["result_event_id"]
        self.assertTrue(all(page["result_event_id"] == issue_event for page in pages))

        refused = [
            obs for obs in result.observations
            if obs.get("tool") == TOOL_READ_TOOL_RESULT and obs.get("event")
        ]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["event"], "duplicate_tool_call")
        self.assertIn("offset 10", refused[0]["recovery"])
        self.assertNotIn("choose_different_tool", refused[0]["recovery"])

    def test_an_event_of_another_session_is_not_found(self):
        registry = _Registry(str(Path(self.tmp.name) / "state.db"), self.tmp.name)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=_ExperimentService(),
            llama_client=_Client([]),
        )
        try:
            other = orchestrator.task_store.get_or_create(source_type="task", source_id="o")
            event_id = orchestrator.task_store.append_event(
                other.id, role="tool", kind="operation", content="{}"
            )
            page = orchestrator._read_tool_result(
                {"event_id": event_id, "session_id": "not-that-session"}
            )
        finally:
            orchestrator.close()
        self.assertEqual(page["error"], "event_not_found")


if __name__ == "__main__":
    unittest.main()
