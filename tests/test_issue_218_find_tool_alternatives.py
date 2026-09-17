"""#218: a terminal find_tool:not_found has to be usable, not just true.

A real Nexus session (``1ca2d781836a481d8152744f5c1ff94d``) was given
``fix github issue #196``, decided it needed to look at the repository, and
asked ``find_tool(goal="run a shell command in the repository")``. The
resolver answered ``not_found`` and listed ``git_status``, ``git_log`` and
``git_worktrees`` - as bare names. With nothing to compare against its own
stated reason ("I need to inspect the repository state"), the controller asked
the identical question again, the loop guard stopped the session and the task
ended in ``needs_review``.

The alternatives were right there. What was missing was everything that makes
an alternative choosable: what each operation does, why it was offered, and
what it takes. These tests pin both halves of the fix - the descriptors the
resolver now returns, and the controller path that must not spend a session
re-asking a question that has already been answered.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_tool_router import (
    ACTION_COMMAND,
    ACTION_RESOLVE_RELATED_OPERATION,
    TOOL_FIND_TOOL,
)
from modules.operations_registry import OperationArgSpec, OperationSpec
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
    NexusTaskOrchestrator,
)

TASK = "fix github issue #196"
# The goal and reason the failing session actually produced.
SHELL_GOAL = "run a shell command in the repository"
SHELL_REASON = "I need to inspect the repository state"
# A goal that find_tool really could serve, so resolving a related operation is
# the recommendation and a repeat of it is redirected.
INSPECT_GOAL = "inspect the repository lifecycle state"
# What the redirect narrows that dead goal down to.
NARROWED_GOAL = (
    "git_status: Inspect the current repository state, branch and working tree changes."
)


class _Client:
    """The local llama-server, scripted.

    Both roles the controller drives - the controller itself and the tool
    resolver - go through this one client, exactly as they do against the real
    local model, so the order of the responses below is the order of the real
    calls.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _specs() -> list[OperationSpec]:
    """Real registry specs, so the descriptors cannot drift from the catalog."""
    return [
        OperationSpec(
            name="git_status",
            description=(
                "Inspect the current repository state, branch and working tree changes."
            ),
            arguments=[],
            tags=["git", "repository", "status"],
            mutates_state=False,
        ),
        OperationSpec(
            name="git_log",
            description="Inspect recent commits and repository history.",
            arguments=[
                OperationArgSpec(
                    name="limit",
                    type="integer",
                    description="How many commits to return.",
                    required=False,
                    default=10,
                    minimum=1,
                    maximum=50,
                )
            ],
            tags=["git", "repository", "history"],
            mutates_state=False,
        ),
        OperationSpec(
            name="git_worktrees",
            description="Inspect the repository worktrees currently checked out.",
            arguments=[],
            tags=["git", "repository", "worktree"],
            mutates_state=False,
        ),
    ]


class _SpyRegistry:
    """The catalog, with execution replaced by a recorder.

    The regression has to be provable without touching a real repository or a
    real GitHub issue, so the candidate operation keeps its real schema and
    loses only its body.
    """

    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls: list[tuple[str, dict, str]] = []
        self._specs = _specs()

    def catalog(self):
        return list(self._specs)

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, dict(args), caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": ["On branch main", "nothing to commit"],
                "duration_ms": 1,
            }
        )


class _ExperimentService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve_experiment(self, experiment_id: str):
        self.calls.append(experiment_id)
        raise KeyError(f"unknown experiment {experiment_id!r}")


def _resolver_not_found() -> str:
    return (
        '{"status":"not_found","operation":"","reason":"no shell execution capability",'
        '"related_tools":["git_status","git_log","git_worktrees"]}'
    )


def _controller_find_tool(goal: str, reason: str) -> str:
    return (
        '{"action":"find_tool","goal":"' + goal + '","reason":"' + reason + '"}'
    )


class Issue218NotFoundAlternativesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _orchestrator(self, responses):
        self.registry = _SpyRegistry(str(Path(self.tmp.name) / "state.db"), self.tmp.name)
        self.service = _ExperimentService()
        self.client = _Client(responses)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=self.service,
            llama_client=self.client,
        )
        self.addCleanup(orchestrator.close)
        return orchestrator

    # ------------------------------------------------------------------
    # 1. the response itself
    # ------------------------------------------------------------------

    def test_alternatives_are_descriptors_not_bare_names(self):
        orchestrator = self._orchestrator([])
        metadata = orchestrator._find_tool_not_found_metadata(
            TASK, INSPECT_GOAL, ["git_status", "git_log", "git_worktrees"]
        )

        self.assertEqual(
            [item["operation"] for item in metadata["related_operations"]],
            ["git_status", "git_log", "git_worktrees"],
        )
        git_status, git_log, _ = metadata["related_operations"]
        self.assertEqual(
            git_status["description"],
            "Inspect the current repository state, branch and working tree changes.",
        )
        self.assertIn("repository", git_status["why_relevant"])
        self.assertEqual(git_status["arguments"], [])
        # The argument schema is the registry's own, so a caller can build the
        # call from the observation without a second lookup.
        self.assertEqual(
            git_log["arguments"],
            [
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "How many commits to return.",
                    "required": False,
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                }
            ],
        )
        self.assertEqual(metadata["recommended_next_action"], ACTION_RESOLVE_RELATED_OPERATION)
        self.assertEqual(metadata["recommended_operation"], "git_status")
        self.assertFalse(metadata["retryable"])
        self.assertIn("Do not repeat find_tool with the same goal", metadata["guidance"])

    def test_a_goal_asking_to_run_something_names_the_native_capability(self):
        # find_tool only ever searches the read-only OperationsRegistry, so a
        # request for shell execution is unresolvable however it is worded. The
        # controller owns `command` natively; saying so is what stops the search.
        orchestrator = self._orchestrator([])
        metadata = orchestrator._find_tool_not_found_metadata(TASK, SHELL_GOAL, [])

        self.assertEqual(metadata["direct_capabilities"], [ACTION_COMMAND])
        self.assertEqual(metadata["recommended_next_action"], ACTION_COMMAND)
        self.assertEqual(metadata["related_operations"], [])

    def test_the_owning_capability_outranks_a_merely_related_operation(self):
        # The scorer produces git_* candidates for any goal mentioning the
        # repository, so a request to run the test suite would otherwise be
        # answered "resolve git_status" - and, on a repeat, executed. That is
        # right-looking evidence about the wrong subject.
        orchestrator = self._orchestrator([])
        metadata = orchestrator._find_tool_not_found_metadata(
            TASK,
            "run a shell command to execute the test suite in the repository",
            ["git_status", "git_log"],
        )

        self.assertEqual(metadata["recommended_next_action"], ACTION_COMMAND)
        self.assertEqual(metadata["direct_capabilities"], [ACTION_COMMAND])
        # Still offered as alternatives, just not as the recommendation - and
        # with no recommended_operation there is nothing for a repeat to be
        # redirected into.
        self.assertEqual(
            [item["operation"] for item in metadata["related_operations"]],
            ["git_status", "git_log"],
        )
        self.assertNotIn("recommended_operation", metadata)

    def test_mentioning_a_shell_or_python_noun_is_not_asking_to_run_one(self):
        # Both of these are read-only lookups. Answering either with "execute
        # this yourself" turns a request to read state into a request to run
        # something, which is the one direction that must never be guessed.
        orchestrator = self._orchestrator([])
        for goal in (
            "inspect the python module versions installed",
            "show the command line arguments of the running process",
        ):
            with self.subTest(goal=goal):
                metadata = orchestrator._find_tool_not_found_metadata(TASK, goal, [])
                self.assertEqual(metadata["direct_capabilities"], [])
                self.assertEqual(
                    metadata["recommended_next_action"], ACTION_MISSING_CAPABILITY
                )

    def test_a_related_name_with_no_spec_behind_it_is_dropped(self):
        # Describing an operation the registry does not have would invent both
        # the description and the argument schema.
        orchestrator = self._orchestrator([])
        metadata = orchestrator._find_tool_not_found_metadata(
            TASK, INSPECT_GOAL, ["git_status", "run_shell"]
        )

        self.assertEqual(
            [item["operation"] for item in metadata["related_operations"]], ["git_status"]
        )

    def test_nothing_to_offer_still_ends_at_missing_capability(self):
        orchestrator = self._orchestrator([])
        metadata = orchestrator._find_tool_not_found_metadata(
            TASK, "inspect GPU utilization", []
        )

        self.assertEqual(metadata["related_operations"], [])
        self.assertEqual(metadata["direct_capabilities"], [])
        self.assertEqual(metadata["recommended_next_action"], ACTION_MISSING_CAPABILITY)

    # ------------------------------------------------------------------
    # 2. the controller path
    # ------------------------------------------------------------------

    def test_controller_that_reads_the_alternatives_calls_one_of_them(self):
        # The contract, not the wording: a controller that consumes the
        # descriptors resolves a listed operation and it is actually invoked.
        orchestrator = self._orchestrator([
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            _resolver_not_found(),
            _controller_find_tool(NARROWED_GOAL, "git_status inspects repository state"),
            '{"status":"found","operation":"git_status","reason":"named directly",'
            '"related_tools":[]}',
            '{"action":"answer","message":"Repository inspected.","reason":"done"}',
        ])
        result = orchestrator.run(TASK, source_type="task", source_id="issue-218-consumed")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual([call[0] for call in self.registry.calls], ["git_status"])
        self.assertEqual(self.registry.calls[0][1], {})
        redirects = orchestrator.task_store.recent_events(
            result.session_id, kind="find_tool_redirected"
        )
        # It got there on its own; no fallback was needed.
        self.assertEqual(redirects, [])
        self.assertNotIn("duplicate_tool_call", str(result.observations))

    def test_a_repeated_dead_lookup_is_redirected_to_the_alternative(self):
        # The same goal asked twice. It must reach git_status instead of the
        # loop guard.
        orchestrator = self._orchestrator([
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            _resolver_not_found(),
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            '{"status":"found","operation":"git_status","reason":"named directly",'
            '"related_tools":[]}',
            '{"action":"answer","message":"Repository inspected.","reason":"done"}',
        ])
        result = orchestrator.run(TASK, source_type="task", source_id="issue-218-redirect")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertNotEqual(result.action, ACTION_CLARIFICATION)
        # The alternative was really called, with the arguments its schema declares.
        self.assertEqual([call[0] for call in self.registry.calls], ["git_status"])
        self.assertEqual(self.registry.calls[0][1], {})
        # The identical terminal lookup was executed exactly once: the repeat
        # never became a second resolver request for the same goal.
        resolver_prompts = [
            prompt for prompt in self.client.prompts if "TOOL RESOLVER MODE" in prompt
        ]
        self.assertEqual(
            sum(1 for prompt in resolver_prompts if INSPECT_GOAL in prompt), 1
        )
        # And the loop guard never had to be the thing that ended the task.
        self.assertNotIn("duplicate_tool_call", str(result.observations))
        self.assertNotIn("non_progressing_cycle", str(result.observations))

    def test_the_observed_session_is_pointed_at_the_capability_that_owns_it(self):
        # The failing session's own goal. It asked to run a shell command, which
        # find_tool can never resolve, so the answer is `command` - not a
        # silently executed git_status - and the repeat is not redirected.
        orchestrator = self._orchestrator([
            _controller_find_tool(SHELL_GOAL, SHELL_REASON),
            _resolver_not_found(),
            '{"action":"command","command":"git status","reason":"inspect the repo"}',
            '{"action":"answer","message":"Repository inspected.","reason":"done"}',
        ])
        result = orchestrator.run(TASK, source_type="task", source_id="issue-218-native")

        not_found = result.observations[0]
        self.assertEqual(not_found["recommended_next_action"], ACTION_COMMAND)
        self.assertEqual(not_found["direct_capabilities"], [ACTION_COMMAND])
        self.assertNotIn("recommended_operation", not_found)
        # The alternatives are still there to be chosen; they are just not the
        # recommendation, and nothing ran them on the controller's behalf.
        self.assertEqual(
            [item["operation"] for item in not_found["related_operations"]],
            ["git_status", "git_log", "git_worktrees"],
        )
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(result.action, ACTION_ANSWER)

    def test_every_refused_repeat_stays_actionable_not_just_the_first(self):
        # The redirect must not cost the #164 enrichment. Once the redirect
        # target is itself terminal there is nothing left to offer, and the
        # guard's verdict has to carry that lookup's own alternatives rather
        # than a bare duplicate_tool_call.
        orchestrator = self._orchestrator([
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            _resolver_not_found(),
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            # the redirected lookup, which also finds nothing
            _resolver_not_found(),
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            '{"action":"missing_capability","message":"no repository inspection tool",'
            '"reason":"nothing left to try","information_gap":"capability",'
            '"gap_kind":"missing_capability"}',
        ])
        result = orchestrator.run(TASK, source_type="task", source_id="issue-218-second-repeat")

        blocked = result.observations[-1]
        self.assertEqual(blocked["event"], "duplicate_tool_call")
        # Still an answer, not a bare verdict.
        self.assertEqual(blocked["status"], "not_found")
        self.assertFalse(blocked["retryable"])
        self.assertEqual(blocked["goal"], NARROWED_GOAL)
        self.assertEqual(
            [item["operation"] for item in blocked["related_operations"]],
            ["git_status", "git_log", "git_worktrees"],
        )
        self.assertEqual(
            blocked["recommended_next_action"], ACTION_RESOLVE_RELATED_OPERATION
        )
        self.assertIn("Do not repeat find_tool with the same goal", blocked["guidance"])
        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)

    def test_the_redirect_leaves_diagnosable_trace_evidence(self):
        orchestrator = self._orchestrator([
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            _resolver_not_found(),
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            '{"status":"found","operation":"git_status","reason":"named directly",'
            '"related_tools":[]}',
            '{"action":"answer","message":"Repository inspected.","reason":"done"}',
        ])
        result = orchestrator.run(TASK, source_type="task", source_id="issue-218-trace")
        redirects = orchestrator.task_store.recent_events(
            result.session_id, kind="find_tool_redirected"
        )
        stats = orchestrator.task_store.controller_turn_stats()

        self.assertEqual(len(redirects), 1)
        metadata = redirects[0]["metadata"]
        self.assertEqual(metadata["controller_action"], TOOL_FIND_TOOL)
        self.assertEqual(metadata["next_action"], ACTION_RESOLVE_RELATED_OPERATION)
        self.assertEqual(metadata["operation"], "git_status")
        self.assertEqual(metadata["goal"], INSPECT_GOAL)
        self.assertEqual(metadata["controller_reason"], SHELL_REASON)
        self.assertEqual(metadata["related_goal"], NARROWED_GOAL)
        # The turn record still says what the controller itself decided.
        self.assertEqual(stats["by_outcome"][TOOL_FIND_TOOL], 2)

        not_found = result.observations[0]
        self.assertEqual(not_found["status"], "not_found")
        self.assertEqual(not_found["goal"], INSPECT_GOAL)
        self.assertEqual(not_found["recommended_operation"], "git_status")
        self.assertEqual(
            [item["operation"] for item in not_found["related_operations"]],
            ["git_status", "git_log", "git_worktrees"],
        )

    def test_the_observation_the_controller_reads_carries_the_descriptors(self):
        orchestrator = self._orchestrator([
            _controller_find_tool(INSPECT_GOAL, SHELL_REASON),
            _resolver_not_found(),
            '{"action":"answer","message":"nothing to do","reason":"done"}',
        ])
        orchestrator.run(TASK, source_type="task", source_id="issue-218-prompt")

        controller_prompts = [
            prompt for prompt in self.client.prompts if "TOOL RESOLVER MODE" not in prompt
        ]
        last = controller_prompts[-1]
        self.assertIn("why_relevant", last)
        self.assertIn(
            "Inspect the current repository state, branch and working tree changes.", last
        )
        self.assertIn("Do not repeat find_tool with the same goal", last)


if __name__ == "__main__":
    unittest.main()
