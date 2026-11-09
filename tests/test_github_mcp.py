"""#41: the official GitHub MCP server as a Nexus tool provider.

The real ``github-mcp-server`` binary and a token are not available in CI, so
the stdio round trip runs against ``tests/fake_github_mcp_server.py``, which
speaks real MCP over stdio with the same tool names.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules import github_issue_migration as migration
from modules.github_mcp import (
    DEFAULT_REPOSITORY,
    GitHubMcpConfig,
    GitHubMcpError,
    GitHubMcpToolResult,
    call_tool,
    list_tools,
    open_session,
)
from modules.operations_registry import OperationsRegistry, OperationValidationError

FAKE_SERVER = str(Path(__file__).resolve().parent / "fake_github_mcp_server.py")


def _fake_config(**overrides) -> GitHubMcpConfig:
    values = {
        "command": sys.executable,
        "args": (FAKE_SERVER,),
        "token": "test-token",
        "timeout_seconds": 30.0,
    }
    values.update(overrides)
    return GitHubMcpConfig(**values)


class ConfigTests(unittest.TestCase):
    def test_defaults_to_the_official_server_over_stdio(self):
        config = GitHubMcpConfig.from_env({})
        self.assertEqual(config.command, "github-mcp-server")
        self.assertEqual(config.args, ("stdio",))
        self.assertEqual(config.repository, DEFAULT_REPOSITORY)
        self.assertEqual((config.owner, config.repo), ("tire0011", "agent-lab"))
        self.assertIsNone(config.token)

    def test_token_falls_back_from_pat_to_github_token_to_gh_token(self):
        self.assertEqual(GitHubMcpConfig.from_env({"GH_TOKEN": "c"}).token, "c")
        self.assertEqual(GitHubMcpConfig.from_env({"GITHUB_TOKEN": "b", "GH_TOKEN": "c"}).token, "b")
        self.assertEqual(
            GitHubMcpConfig.from_env(
                {"GITHUB_PERSONAL_ACCESS_TOKEN": "a", "GITHUB_TOKEN": "b"}
            ).token,
            "a",
        )

    def test_overrides_are_read_from_the_environment(self):
        config = GitHubMcpConfig.from_env(
            {
                "GITHUB_MCP_SERVER_COMMAND": "docker",
                "GITHUB_MCP_SERVER_ARGS": "run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server",
                "GITHUB_MCP_TOOLSETS": "repos,issues",
                "GITHUB_REPOSITORY": "someone/else",
                "GITHUB_MCP_TIMEOUT_SECONDS": "12",
            }
        )
        self.assertEqual(config.command, "docker")
        self.assertEqual(config.args[-1], "ghcr.io/github/github-mcp-server")
        self.assertEqual(config.repository, "someone/else")
        self.assertEqual(config.timeout_seconds, 12.0)
        self.assertEqual(config.server_env()["GITHUB_TOOLSETS"], "repos,issues")

    def test_token_goes_to_the_subprocess_env_never_to_the_command_line(self):
        config = GitHubMcpConfig(token="secret-value")
        params = config.stdio_params()
        self.assertEqual(params["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"], "secret-value")
        self.assertNotIn("secret-value", " ".join([params["command"], *params["args"]]))
        self.assertNotIn("secret-value", repr(config))

    def test_readiness_names_what_is_missing(self):
        config = GitHubMcpConfig(command="definitely-not-installed-github-mcp", token=None)
        error = config.readiness_error()
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", error)
        self.assertIn("definitely-not-installed-github-mcp", error)
        self.assertFalse(config.configured)
        self.assertTrue(_fake_config().configured)

    def test_unconfigured_server_refuses_to_start(self):
        with self.assertRaises(GitHubMcpError):
            list_tools(GitHubMcpConfig(command="definitely-not-installed-github-mcp", token="t"))


class StdioRoundTripTests(unittest.TestCase):
    def test_tools_are_registered_and_callable(self):
        names = {tool.name for tool in list_tools(_fake_config())}
        self.assertTrue({"list_issues", "issue_write", "add_issue_comment", "list_commits"} <= names)

        me = call_tool("get_me", {}, _fake_config()).json()
        self.assertTrue(me["token_seen"])

        listing = call_tool("list_issues", {"owner": "o", "repo": "r", "state": "OPEN"}, _fake_config())
        self.assertFalse(listing.is_error)
        self.assertEqual(listing.json()["issues"][0]["title"], "Existing open issue")

    def test_create_comment_and_close_in_one_session(self):
        async def _flow():
            async with open_session(_fake_config()) as session:
                created = (await session.call_tool(
                    "issue_write", {"method": "create", "owner": "o", "repo": "r", "title": "T", "body": "B"}
                )).json()
                comment = await session.call_tool(
                    "add_issue_comment", {"owner": "o", "repo": "r", "issue_number": created["number"], "body": "c"}
                )
                closed = (await session.call_tool(
                    "issue_write",
                    {"method": "update", "owner": "o", "repo": "r", "issue_number": created["number"], "state": "closed"},
                )).json()
                missing = await session.call_tool(
                    "add_issue_comment", {"owner": "o", "repo": "r", "issue_number": 999, "body": "c"}
                )
                return created, comment, closed, missing

        created, comment, closed, missing = asyncio.run(_flow())
        self.assertEqual(created["number"], 2)
        self.assertFalse(comment.is_error)
        self.assertEqual(closed["state"], "CLOSED")
        self.assertTrue(missing.is_error)


class RegistryOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(self.tmp / "agent.db"),
            repo_root=str(self.tmp),
            github_mcp_config_factory=_fake_config,
        )
        self.addCleanup(self.registry.close)

    def test_catalog_declares_both_operations(self):
        specs = {spec.name: spec for spec in self.registry.catalog()}
        self.assertFalse(specs["github_mcp_tools"].mutates_state)
        self.assertTrue(specs["github_mcp_call"].mutates_state)
        self.assertTrue(specs["github_mcp_call"].controller_allowed)

    def test_github_mcp_tools_lists_the_server_tools(self):
        result = self.registry.execute("github_mcp_tools", {})
        self.assertTrue(result.success, result.error_summary)
        self.assertTrue(any(line.startswith("list_issues") for line in result.output_lines))

    def test_github_mcp_call_runs_a_tool_with_json_arguments(self):
        result = self.registry.execute(
            "github_mcp_call",
            {"tool": "list_issues", "arguments": json.dumps({"owner": "o", "repo": "r", "state": "OPEN"})},
        )
        self.assertTrue(result.success, result.error_summary)
        self.assertIn("Existing open issue", "\n".join(result.output_lines))

    def test_github_mcp_call_reports_a_tool_error_as_failure(self):
        result = self.registry.execute(
            "github_mcp_call",
            {"tool": "add_issue_comment", "arguments": {"owner": "o", "repo": "r", "issue_number": 999, "body": "x"}},
        )
        self.assertFalse(result.success)

    def test_github_mcp_call_rejects_non_object_arguments(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("github_mcp_call", {"tool": "list_issues", "arguments": "[1]"})
        with self.assertRaises(OperationValidationError):
            self.registry.execute("github_mcp_call", {"tool": ""})


def _experiments_db(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE experiments (id TEXT, goal TEXT, status TEXT, created_at TEXT, ended_at TEXT, error TEXT)"
        )
        conn.executemany(
            "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?)",
            [(i, goal, status, created, created, f"error of {i}") for i, goal, status, created in rows],
        )


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "agent.db"
        _experiments_db(
            self.db,
            [
                ("a" * 32, "Add a cancel button\n\nDetails", "failed", "2026-10-01"),
                ("b" * 32, "Add a cancel button\n\nRetry", "timed_out", "2026-10-02"),
                ("c" * 32, "Fix the flaky clock\nmore", "failed", "2026-10-01"),
                ("d" * 32, "Fix the flaky clock\nagain", "succeeded", "2026-10-03"),
                ("e" * 32, "GitHub Issue #12: Something tracked", "failed", "2026-10-01"),
                ("f" * 32, "solve github issue #340", "timed_out", "2026-10-01"),
                ("0" * 32, f"Resume and finish the existing experiment {'a' * 32}", "failed", "2026-10-04"),
            ],
        )

    def test_only_untracked_unsolved_problem_goals_are_candidates(self):
        items = migration.experiment_problems(self.db)
        self.assertEqual([item.title for item in items], ["Problem: Add a cancel button"])
        self.assertIn("`" + "b" * 32 + "` - timed_out", items[0].body)
        self.assertIn(f"{migration.KEY_PREFIX} {items[0].key}", items[0].body)

    def test_proposals_and_open_ideas_become_todos(self):
        markdown = "# x\n## Proposed issues\n### P1 First thing\nbody one\n### P2 Second\nbody two\n## Decision per idea\n### Other\n"
        ideas = [
            SimpleNamespace(id="idea-1", section="Inbox", text="Open idea", state="new"),
            SimpleNamespace(id="idea-2", section="Inbox", text="Done idea", state="adopted"),
        ]
        items = migration.collect_items(db_path=self.db, ideas=ideas, proposals_markdown=markdown)
        self.assertEqual(
            [item.title for item in items],
            ["Problem: Add a cancel button", "Open idea", "First thing", "Second"],
        )
        self.assertIn("body one", items[2].body)

    def test_reviewed_resolved_items_are_never_candidates(self):
        markdown = "## Proposed issues\n### P9 Multi-line idea input in the Web UI\nx\n"
        self.assertEqual(migration.collect_items(db_path=self.db, proposals_markdown=markdown)[1:], [])

    def test_plan_skips_title_and_key_duplicates(self):
        items = migration.collect_items(
            db_path=self.db,
            proposals_markdown="## Proposed issues\n### P1 First thing\nx\n### P2 Second\ny\n",
        )
        problem_key = items[0].key
        existing = [
            {"number": 7, "title": "first thing.", "state": "CLOSED", "body": ""},
            {"number": 8, "title": "Renamed", "body": f"... `{migration.KEY_PREFIX} {problem_key}`"},
        ]
        decisions = {d.item.title: d for d in migration.plan(items, existing)}
        self.assertEqual(decisions["First thing"].action, "skip_duplicate")
        self.assertIn("#7", decisions["First thing"].detail)
        self.assertEqual(decisions["Problem: Add a cancel button"].action, "skip_duplicate")
        self.assertEqual(decisions["Second"].action, "create")

    def test_apply_creates_through_mcp_and_a_rerun_creates_nothing(self):
        items = migration.collect_items(db_path=self.db)

        async def _migrate():
            async with open_session(_fake_config()) as session:
                tools = {tool.name: tool for tool in await session.list_tools()}
                first = migration.plan(items, await migration.existing_issues(session, tools))
                await migration.apply(session, tools, first)
                second = migration.plan(items, await migration.existing_issues(session, tools))
                return first, second

        first, second = asyncio.run(_migrate())
        self.assertEqual([d.action for d in first], ["created"])
        self.assertIn("/issues/2", first[0].detail)
        self.assertEqual([d.action for d in second], ["skip_duplicate"])
        self.assertIn("summary", migration.report(first))

    def test_result_json_prefers_structured_content(self):
        self.assertEqual(GitHubMcpToolResult("t", False, "not json", {"a": 1}).json(), {"a": 1})
        self.assertEqual(GitHubMcpToolResult("t", False, '{"b": 2}').json(), {"b": 2})
        self.assertIsNone(GitHubMcpToolResult("t", False, "plain").json())


if __name__ == "__main__":
    unittest.main()
