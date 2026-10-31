"""#384: the planner explores its own code and Nexus's state before it plans.

It runs as a read-only Claude agent with the Nexus MCP read tools; a single
Azure call is the fallback.
"""

from __future__ import annotations

import ast
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nexus_tick
from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS
from modules.nexus_planner import IssuePlanner

ISSUES = [{"number": 351, "title": "Small", "body": "x"}]
ANSWER = json.dumps({"issue": 351, "reason": "small and testable", "proposal": None})


def _registered_tools() -> list[str]:
    tree = ast.parse(Path("nexus_mcp.py").read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                func = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    names.append(node.name)
    return names


class ToolClassificationTests(unittest.TestCase):
    def test_every_mcp_tool_is_either_read_only_or_write(self):
        registered = set(_registered_tools())
        self.assertEqual(registered, set(READ_ONLY_TOOLS) | set(WRITE_TOOLS))
        self.assertFalse(set(READ_ONLY_TOOLS) & set(WRITE_TOOLS))

    def test_the_planner_gets_issue_and_history_tools_but_no_shell(self):
        for name in (
            "github_issues", "git_log", "experiments_list", "experiment_logs",
            "experiments_gate_recheck",
        ):
            self.assertIn(name, READ_ONLY_TOOLS)
        for name in ("shell", "git_pull", "experiment_start", "chat"):
            self.assertNotIn(name, READ_ONLY_TOOLS)


class PlannerTests(unittest.TestCase):
    def test_the_exploring_agent_plans_first(self):
        prompts = {"explore": [], "complete": []}
        planner = IssuePlanner(
            complete=lambda p: prompts["complete"].append(p) or ANSWER,
            explore=lambda p: prompts["explore"].append(p) or ANSWER,
        )
        self.assertEqual(planner.choose(ISSUES).issue, 351)
        self.assertEqual(len(prompts["explore"]), 1)
        self.assertEqual(prompts["complete"], [])
        self.assertIn("github_issues", prompts["explore"][0])

    def test_azure_takes_over_when_exploring_fails(self):
        for explore in (lambda p: (_ for _ in ()).throw(RuntimeError("claude down")), lambda p: "no json"):
            with self.subTest(explore=explore):
                fallback = []
                planner = IssuePlanner(complete=lambda p: fallback.append(p) or ANSWER, explore=explore)
                self.assertEqual(planner.choose(ISSUES).issue, 351)
                self.assertEqual(len(fallback), 1)
                self.assertNotIn("github_issues", fallback[0])


class ClaudeRunTests(unittest.TestCase):
    def test_claude_runs_read_only_with_the_mcp_read_tools(self):
        seen = {}

        def run(self, **kwargs):
            seen.update(kwargs)
            args = kwargs["extra_args"]
            seen["config"] = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
            return SimpleNamespace(success=True, final_message=ANSWER, error=None)

        with patch.dict(os.environ, {"AGENT_API_KEY": "k"}), \
                patch("modules.claude_worker.ClaudeWorker.run", run):
            self.assertEqual(nexus_tick._explore_with_claude("/agent", "plan"), ANSWER)

        self.assertEqual(seen["sandbox"], "read-only")
        self.assertEqual(seen["working_dir"], "/agent")
        server = seen["config"]["mcpServers"]["nexus"]
        self.assertEqual(server["headers"], {"x-api-key": "k"})
        allowed = seen["extra_args"][seen["extra_args"].index("--allowedTools") + 1].split(",")
        self.assertIn("mcp__nexus__github_issues", allowed)
        self.assertNotIn("mcp__nexus__shell", allowed)
        denied = seen["extra_args"][seen["extra_args"].index("--disallowedTools") + 1].split(",")
        self.assertIn("mcp__nexus__shell", denied)


if __name__ == "__main__":
    unittest.main()
