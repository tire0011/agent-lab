"""#224: a related-operation redirect must keep the values the goal named.

Nexus session ``3e2e565e49134703934e59bd57ad9118`` asked find_tool for a goal
that named the concrete issue number. The lookup was terminal, and its repeat
was redirected to ``github_issue`` - with a goal built from the operation's
description alone. The resolver binds argument values only from the goal it is
given, so ``issue_number`` could not be bound, the resolved operation was not
executed, and the controller spent another round supplying a number it had
already named.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_tool_router import related_operation_lookup_goal
from modules.operations_registry import OperationArgSpec, OperationSpec
from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator

TASK = "summarize the acceptance criteria of issue 224"
# The goal carries the concrete value; nothing in it resolves directly.
DEAD_GOAL = "look up the specification of issue 224"
REASON = "I need the acceptance criteria"
GITHUB_ISSUE_DESCRIPTION = (
    "Ein GitHub Issue read-only abrufen: Nummer, Titel, Beschreibung, Status, URL und Metadaten."
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _specs() -> list[OperationSpec]:
    return [
        OperationSpec(
            name="github_issue",
            description=GITHUB_ISSUE_DESCRIPTION,
            arguments=[
                OperationArgSpec(
                    "issue_number",
                    "integer",
                    "GitHub-Issue-Nummer.",
                    required=True,
                    minimum=1,
                )
            ],
            tags=["github", "issue", "read", "specification", "acceptance criteria"],
            mutates_state=False,
        ),
        OperationSpec(
            name="git_status",
            description="Inspect the current repository state, branch and working tree changes.",
            arguments=[],
            tags=["git", "repository", "status"],
            mutates_state=False,
        ),
    ]


class _SpyRegistry:
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
                "output_lines": ["#224 Preserve concrete arguments"],
                "duration_ms": 1,
            }
        )


class _ExperimentService:
    def resolve_experiment(self, experiment_id: str):
        raise KeyError(experiment_id)


def _controller_find_tool(goal: str) -> str:
    return '{"action":"find_tool","goal":"' + goal + '","reason":"' + REASON + '"}'


def _resolver_not_found() -> str:
    return (
        '{"status":"not_found","operation":"","reason":"no specification tool",'
        '"related_tools":["github_issue"]}'
    )


def _prior(goal: str, operation: str = "github_issue", arguments=None) -> dict:
    return {
        "goal": goal,
        "recommended_operation": operation,
        "related_operations": [
            {
                "operation": operation,
                "description": GITHUB_ISSUE_DESCRIPTION,
                "arguments": [{"name": "issue_number"}] if arguments is None else arguments,
            }
        ],
    }


class RelatedOperationLookupGoalTests(unittest.TestCase):
    def test_the_concrete_value_survives_the_redirect(self):
        goal = related_operation_lookup_goal(_prior(DEAD_GOAL))

        self.assertTrue(goal.startswith("github_issue: "))
        self.assertIn("224", goal)
        self.assertIn(DEAD_GOAL, goal)
        self.assertIn(GITHUB_ISSUE_DESCRIPTION, goal)

    def test_an_operation_without_arguments_keeps_the_bare_descriptor(self):
        goal = related_operation_lookup_goal(_prior(DEAD_GOAL, "git_status", arguments=[]))

        self.assertEqual(goal, f"git_status: {GITHUB_ISSUE_DESCRIPTION}")

    def test_a_goal_that_already_is_the_redirect_is_not_wrapped_again(self):
        # Otherwise every round would produce a new goal, and the redirect
        # could never be recognised as the same lookup.
        first = related_operation_lookup_goal(_prior(DEAD_GOAL))
        self.assertEqual(related_operation_lookup_goal(_prior(first)), first)

    def test_the_request_is_kept_when_the_goal_is_long(self):
        goal = related_operation_lookup_goal(
            {**_prior(DEAD_GOAL), "related_operations": [
                {"operation": "github_issue", "description": "x" * 2000,
                 "arguments": [{"name": "issue_number"}]}
            ]}
        )
        self.assertLessEqual(len(goal), 1000)
        self.assertIn("224", goal)


class Issue224RedirectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_redirected_github_issue_binds_and_runs_directly(self):
        registry = _SpyRegistry(str(Path(self.tmp.name) / "state.db"), self.tmp.name)
        client = _Client([
            _controller_find_tool(DEAD_GOAL),
            _resolver_not_found(),
            # the repeat, redirected to github_issue
            _controller_find_tool(DEAD_GOAL),
            '{"status":"found","operation":"github_issue","reason":"reads the issue",'
            '"arguments":{"issue_number":224},"related_tools":[]}',
            '{"action":"answer","message":"Issue read.","reason":"done"}',
        ])
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=_ExperimentService(),
            llama_client=client,
        )
        try:
            result = orchestrator.run(TASK, source_type="task", source_id="issue-224-redirect")
            redirects = orchestrator.task_store.recent_events(
                result.session_id, kind="find_tool_redirected"
            )
        finally:
            orchestrator.close()

        self.assertEqual(len(redirects), 1)
        self.assertEqual(redirects[0]["metadata"]["operation"], "github_issue")
        self.assertIn("224", redirects[0]["metadata"]["related_goal"])
        # The resolver was shown the value it had to bind.
        redirected_prompt = [p for p in client.prompts if "TOOL RESOLVER MODE" in p][-1]
        self.assertIn(DEAD_GOAL, redirected_prompt)
        resolved = next(
            observation for observation in result.observations
            if observation.get("tool") == "find_tool" and observation.get("status") == "found"
        )
        self.assertEqual(resolved["resolved_arguments"], {"issue_number": 224})
        # Executed straight from the resolution, with no extra controller round.
        self.assertEqual(registry.calls[0][:2], ("github_issue", {"issue_number": 224}))
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(client.responses, [])


if __name__ == "__main__":
    unittest.main()
