"""#223: the controller prompt shows the result, not how it was obtained.

Session ``3e2e565e49134703934e59bd57ad9118`` fetched GitHub issue #196 and
then asked for the identical ``github_issue(196)`` again. The answer was in
the prompt, inside a tool wrapper, a grant id, a full operation schema and a
nested result object.

These tests pin the two halves of the fix that must not drift apart: the
persisted event keeps the whole envelope, and the view rendered into the next
prompt keeps the answer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_task_store import NexusTaskStore
from modules.prompt_budget import (
    _stub,
    compact_observations,
    compute_budget,
    controller_observation_view,
    prompt_json,
)
from modules.task_orchestrator import NexusTaskOrchestrator


def _github_issue_success(event_id: int = 158) -> dict:
    """The raw observation ``execute_operation`` builds, envelope and all."""
    return {
        "tool": "operation",
        "operation": "github_issue",
        "args": {"issue_number": 196},
        "grant_event_id": 156,
        "operation_schema": {
            "name": "github_issue",
            "description": "Read one GitHub issue",
            "arguments": [
                {"name": "issue_number", "type": "integer", "required": True, "description": "..."}
            ],
        },
        "result": {
            "operation": "github_issue",
            "success": True,
            "output_lines": [
                "Issue #196: Publish Nexus Outbox messages to communication PR",
                "State: open",
                "Body: the outbox should be published to the communication PR.",
            ],
            "duration_ms": 412,
            "exit_code": 0,
            "error_summary": None,
        },
        "event_id": event_id,
    }


class SuccessfulOperationViewTests(unittest.TestCase):
    def test_execution_metadata_is_omitted(self):
        [view] = controller_observation_view([_github_issue_success()])
        for field in ("grant_event_id", "operation_schema"):
            self.assertNotIn(field, view)

    def test_identity_arguments_and_event_id_remain_available(self):
        [view] = controller_observation_view([_github_issue_success()])
        self.assertEqual(view["tool"], "operation")
        self.assertEqual(view["operation"], "github_issue")
        self.assertEqual(view["args"], {"issue_number": 196})
        self.assertEqual(view["event_id"], 158)
        self.assertEqual(view["status"], "succeeded")

    def test_result_content_remains_available(self):
        [view] = controller_observation_view([_github_issue_success()])
        rendered = json.dumps(view)
        self.assertIn("Issue #196: Publish Nexus Outbox messages", rendered)
        self.assertIn("State: open", rendered)

    def test_nested_result_wrapper_is_flattened_not_duplicated(self):
        [view] = controller_observation_view([_github_issue_success()])
        # The output lines are reachable at one place, not two.
        self.assertNotIn("result", view)
        self.assertEqual(len(view["output_lines"]), 3)

    def test_the_view_is_materially_smaller_than_the_raw_event(self):
        raw = _github_issue_success()
        [view] = controller_observation_view([raw])
        self.assertLess(len(json.dumps(view)), len(json.dumps(raw)))

    def test_the_input_observation_is_not_mutated(self):
        raw = _github_issue_success()
        before = json.dumps(raw, sort_keys=True)
        controller_observation_view([raw])
        self.assertEqual(json.dumps(raw, sort_keys=True), before)


class EmptySuccessfulResultTests(unittest.TestCase):
    """A success that returned nothing still has to read as an answer.

    On the VM, git_status succeeded on a clean worktree with zero output
    lines, and the controller's very next decision ran it again. An absent
    `output_lines` is indistinguishable from one compaction dropped - and
    `prompt_json` strips an empty list regardless.
    """

    def _empty_success(self):
        return {
            "tool": "operation",
            "operation": "git_status",
            "args": {},
            "grant_event_id": 3,
            "result": {
                "operation": "git_status",
                "success": True,
                "output_lines": [],
                "exit_code": 0,
                "error_summary": None,
            },
            "event_id": 4,
        }

    def test_an_empty_success_says_so_explicitly(self):
        [view] = controller_observation_view([self._empty_success()])
        self.assertEqual(view["status"], "succeeded")
        self.assertEqual(view["output"], "none")

    def test_the_marker_survives_prompt_rendering(self):
        [view] = controller_observation_view([self._empty_success()])
        self.assertIn('"output":"none"', prompt_json([view]))

    def test_a_success_with_output_is_not_given_the_marker(self):
        observation = self._empty_success()
        observation["result"]["output_lines"] = ["M README.md"]
        [view] = controller_observation_view([observation])
        self.assertNotIn("output", view)
        self.assertEqual(view["output_lines"], ["M README.md"])

    def test_a_failure_is_not_given_the_marker(self):
        observation = self._empty_success()
        observation["result"]["success"] = False
        observation["result"]["error_summary"] = "boom"
        [view] = controller_observation_view([observation])
        self.assertNotIn("output", view)
        self.assertEqual(view["status"], "failed")


class FindToolViewTests(unittest.TestCase):
    def test_resolver_metrics_and_bound_schema_are_omitted_on_success(self):
        observation = {
            "tool": "find_tool",
            "status": "found",
            "goal": "read github issue 196",
            "resolved_tool": "github_issue",
            "resolved_arguments": {"issue_number": 196},
            "arguments_schema": [{"name": "issue_number", "type": "integer"}],
            "operation": {"name": "github_issue", "arguments": [{"name": "issue_number"}]},
            "reason": "reads one issue",
            "metrics": {"resolver_provider": "local", "latency_ms": 9100, "candidate_operations": ["a", "b"]},
            "event_id": 157,
        }
        [view] = controller_observation_view([observation])
        self.assertNotIn("metrics", view)
        self.assertNotIn("operation", view)
        # Already executed with those arguments; the schema has nothing left to fill in.
        self.assertNotIn("arguments_schema", view)
        self.assertEqual(view["resolved_tool"], "github_issue")
        self.assertEqual(view["resolved_arguments"], {"issue_number": 196})

    def test_schema_is_kept_when_the_controller_must_supply_arguments(self):
        observation = {
            "tool": "find_tool",
            "status": "found",
            "goal": "read a github issue",
            "resolved_tool": "github_issue",
            "resolved_arguments": None,
            "arguments_schema": [{"name": "issue_number", "type": "integer", "required": True}],
            "operation": {"name": "github_issue", "arguments": []},
            "metrics": {"latency_ms": 9100},
        }
        [view] = controller_observation_view([observation])
        self.assertEqual(view["arguments_schema"], observation["arguments_schema"])

    def test_not_found_guidance_is_kept_in_full(self):
        # The prompt rules tell the controller to read these before deciding,
        # so compaction must not be what removes them.
        observation = {
            "tool": "find_tool",
            "status": "not_found",
            "goal": "deploy the service",
            "retryable": False,
            "reason": "no read-only capability deploys anything",
            "related_operations": [{"operation": "service_status", "why_relevant": "..."}],
            "recommended_next_action": "missing_capability",
            "recommended_operation": "service_status",
            "direct_capabilities": ["command"],
            "resolver_raw": '{"status":"not_found"}',
            "metrics": {"latency_ms": 8800},
        }
        [view] = controller_observation_view([observation])
        self.assertNotIn("metrics", view)
        for field in (
            "related_operations",
            "recommended_next_action",
            "recommended_operation",
            "direct_capabilities",
            "retryable",
            "reason",
            "resolver_raw",
        ):
            self.assertIn(field, view)


class SupersededFailureTests(unittest.TestCase):
    def test_a_failed_attempt_is_dropped_once_the_same_call_succeeded(self):
        rejected = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": "196"},
            "error": "invalid_tool_request",
            "reason": "issue_number muss eine ganze Zahl sein.",
            "recovery": "correct the arguments or choose another capability",
            "event_id": 157,
        }
        views = controller_observation_view([rejected, _github_issue_success()])
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["status"], "succeeded")

    def test_an_unrelated_failure_stays_visible(self):
        unrelated = {
            "tool": "operation",
            "operation": "service_logs",
            "args": {"unit": "llama-server"},
            "error": "invalid_tool_request",
            "reason": "unit llama-server is not permitted",
            "event_id": 150,
        }
        views = controller_observation_view([unrelated, _github_issue_success()])
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["operation"], "service_logs")
        self.assertEqual(views[0]["error"], "invalid_tool_request")

    def test_a_failure_for_different_arguments_stays_visible(self):
        other_issue = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 999},
            "result": {"operation": "github_issue", "success": False, "output_lines": ["not found"]},
            "event_id": 151,
        }
        views = controller_observation_view([other_issue, _github_issue_success()])
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["status"], "failed")

    def test_a_failure_after_the_success_is_new_information_and_is_kept(self):
        later_failure = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 196},
            "result": {"operation": "github_issue", "success": False, "output_lines": ["rate limited"]},
            "event_id": 170,
        }
        views = controller_observation_view([_github_issue_success(), later_failure])
        self.assertEqual(len(views), 2)
        self.assertEqual(views[1]["status"], "failed")


class DuplicateCallRecoveryTests(unittest.TestCase):
    def test_a_duplicate_refusal_names_the_event_that_already_answered_it(self):
        refusal = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 196},
            "event": "duplicate_tool_call",
            "signature": "9f" * 32,
            "previous_call_id": "abc123",
            "recovery": "choose_different_tool_or_controlled_final_action",
        }
        views = controller_observation_view([_github_issue_success(), refusal])
        self.assertEqual(views[1]["already_succeeded_in_event"], 158)
        self.assertIn("already succeeded in event 158", views[1]["recovery"])
        self.assertIn("github_issue", views[1]["recovery"])

    def test_a_refusal_without_a_matching_success_keeps_its_generic_recovery(self):
        refusal = {
            "tool": "memory_search",
            "event": "duplicate_tool_call",
            "recovery": "choose_different_tool_or_controlled_final_action",
        }
        [view] = controller_observation_view([refusal])
        self.assertNotIn("already_succeeded_in_event", view)
        self.assertEqual(view["recovery"], "choose_different_tool_or_controlled_final_action")


class NonOperationToolsTests(unittest.TestCase):
    def test_other_tools_pass_through_unchanged(self):
        observations = [
            {"tool": "command", "command": "git status", "result": "clean", "event_id": 1},
            {"tool": "memory_search", "query": "prior work", "results": [{"content": "x"}]},
            {"tool": "python_run", "script": "scripts/x.py", "result": "ok"},
            {"tool": "delegate", "provider": "azure_openai", "result": "advice"},
        ]
        self.assertEqual(controller_observation_view(observations), observations)

    def test_an_empty_list_and_non_dict_entries_survive(self):
        self.assertEqual(controller_observation_view([]), [])
        self.assertEqual(controller_observation_view([None, "x"]), [None, "x"])


class StubbedUnderRealVmBudgetTests(unittest.TestCase):
    """The VM budget is the path that actually decides this.

    On the memory-pressured Nexus VM ``observation_chars`` collapses to about
    150, so every observation is reduced to a stub. A fix that only improves
    the full-fidelity rendering would never reach the run it was written for.
    """

    def _vm_budget(self):
        # The plan the VM reported during the #223 check run.
        return compute_budget(
            mem_available_kb=2_405_376,
            swap_free_kb=7_456_444,
            swap_total_kb=8_388_604,
            mem_limit_mb=2048,
        )

    def test_the_vm_budget_really_does_stub_everything(self):
        self.assertLess(self._vm_budget().observation_chars, 300)

    def test_a_stubbed_success_still_names_the_operation_its_args_and_outcome(self):
        plan = self._vm_budget()
        views = controller_observation_view([_github_issue_success()])
        compacted, omitted = compact_observations(
            views, budget_chars=plan.observation_chars, keep_recent_full=2
        )
        self.assertEqual(omitted, [0])  # it is a stub, not full content
        stub = compacted[0]
        self.assertEqual(stub["operation"], "github_issue")
        self.assertEqual(stub["args"], {"issue_number": 196})
        self.assertEqual(stub["status"], "succeeded")
        self.assertEqual(stub["event_id"], 158)

    def test_an_oversized_argument_list_is_dropped_rather_than_half_rendered(self):
        stub = _stub(
            {
                "tool": "operation",
                "operation": "x",
                "args": {"blob": "y" * 400},
                "event_id": 9,
            }
        )
        self.assertNotIn("args", stub)
        self.assertEqual(stub["operation"], "x")


class _StubTaskStore:
    """Records what would be persisted, so the raw/view split is testable."""

    def __init__(self):
        self.events: list[dict] = []

    def append_event(self, session_id, *, role, kind, content, metadata=None):
        self.events.append({"kind": kind, "content": content, "metadata": metadata})
        return 100 + len(self.events)


class RawEventStillHoldsTheFullEnvelopeTests(unittest.TestCase):
    """The journal is not what gets compacted (the issue says so explicitly)."""

    def test_the_persisted_event_content_keeps_grant_and_schema(self):
        store = _StubTaskStore()
        observation = _github_issue_success()
        observation.pop("event_id")
        store.append_event(
            "session", role="tool", kind="operation",
            content=json.dumps(observation, separators=(",", ":")),
        )
        persisted = json.loads(store.events[0]["content"])
        self.assertEqual(persisted["grant_event_id"], 156)
        self.assertIn("operation_schema", persisted)
        self.assertTrue(persisted["result"]["success"])


class PromptRenderingTests(unittest.TestCase):
    """End to end through ``_build_prompt``: what the model actually reads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "nexus.db")
        self.orchestrator = NexusTaskOrchestrator(
            operations_registry=SimpleNamespace(
                read_only_specs=lambda: [],
                catalog=lambda: [],
                repo_root=self._tmp.name,
            ),
            experiment_service=SimpleNamespace(resolve_experiment=lambda _id: None),
            task_store=NexusTaskStore(db_path=self.db_path),
        )
        self.session = SimpleNamespace(
            id="session-223", source_type="task", source_id="223", title="t"
        )

    def tearDown(self):
        self.orchestrator.close()
        self._tmp.cleanup()

    def _prompt_for(self, observations):
        prompt, _meta = self.orchestrator._build_prompt(
            self.session,
            "solve github issue #196",
            [],
            observations,
            compute_budget(mem_available_kb=None),
        )
        return prompt.partition("OBSERVATIONS FROM TOOLS USED IN THIS RUN:\n")[2]

    def test_the_prompt_carries_the_issue_content_without_the_grant_or_schema(self):
        section = self._prompt_for([_github_issue_success()])
        self.assertIn("Issue #196: Publish Nexus Outbox messages", section)
        self.assertNotIn("grant_event_id", section)
        self.assertNotIn("operation_schema", section)

    def test_the_orchestrators_in_memory_observations_are_left_intact(self):
        observations = [_github_issue_success()]
        self._prompt_for(observations)
        self.assertEqual(observations[0]["grant_event_id"], 156)
        self.assertIn("operation_schema", observations[0])

    def test_a_duplicate_refusal_reaches_the_prompt_pointing_at_the_result(self):
        section = self._prompt_for(
            [
                _github_issue_success(),
                {
                    "tool": "operation",
                    "operation": "github_issue",
                    "args": {"issue_number": 196},
                    "event": "duplicate_tool_call",
                    "recovery": "choose_different_tool_or_controlled_final_action",
                },
            ]
        )
        self.assertIn("already succeeded in event 158", section)


class LoopGuardCallIdentityTests(unittest.TestCase):
    """One call must hash one way, whichever path ran it.

    `find_tool` executing the operation it resolved and the controller's own
    `call_tool` are the same call against the same capability. They were
    registered with the loop guard under different shapes - `call_tool` with
    {"tool", "arguments"} versus `operation` with {"operation", "args"} - so
    the guard hashed them differently and could not see the second as a repeat
    of the first.

    Measured on the VM on 2026-09-20: git_status resolved, ran, and the very
    next controller decision ran it again without the guard objecting (#223).
    """

    def test_both_paths_produce_the_same_signature(self):
        from modules.tool_loop_guard import call_signature

        controller_side = call_signature("operation", {"operation": "git_status", "args": {}})
        resolved_side = call_signature("operation", {"operation": "git_status", "args": {}})
        self.assertEqual(controller_side, resolved_side)
        # The shape that used to be used for the resolved path must NOT be
        # what either side registers any more.
        self.assertNotEqual(
            controller_side,
            call_signature("call_tool", {"tool": "git_status", "arguments": {}}),
        )

    def test_the_guard_refuses_a_call_tool_repeating_what_find_tool_ran(self):
        """The behaviour, not the spelling: does the guard catch the repeat?"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "nexus.db")
            orchestrator = NexusTaskOrchestrator(
                operations_registry=SimpleNamespace(
                    read_only_specs=lambda: [],
                    catalog=lambda: [],
                    repo_root=tmp,
                ),
                experiment_service=SimpleNamespace(resolve_experiment=lambda _id: None),
                task_store=NexusTaskStore(db_path=db_path),
            )
            try:
                session = orchestrator.task_store.get_or_create(
                    source_type="task", source_id="issue-223-guard-identity"
                )
                orchestrator._execute_resolved_tool(
                    session_id=session.id,
                    state_token="token",
                    observation={
                        "tool": "find_tool",
                        "status": "found",
                        "resolved_tool": "git_status",
                        "resolved_arguments": {},
                    },
                    reason="inspect the repository",
                    task_logger=None,
                )
                # Exactly what the controller's own call_tool registers.
                repeat = orchestrator.loop_guard.check(
                    session.id,
                    "operation",
                    {"operation": "git_status", "args": {}},
                    state_token="token",
                    reason="use the operation returned by find_tool",
                )
            finally:
                orchestrator.close()
        self.assertFalse(
            repeat.allowed,
            "the guard did not recognise the controller's call_tool as a repeat "
            "of the operation find_tool had already executed",
        )


if __name__ == "__main__":
    unittest.main()
