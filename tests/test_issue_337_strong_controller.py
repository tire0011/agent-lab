"""#337: the controller runs on Azure, judgement goes to a strong model, and
commands do not write to GitHub.

In the first #336 run the local controller asked the same small local model
which issues were done; it invented reasons and tried to close issues.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.task_orchestrator import ACTION_ANSWER, ACTION_COMMAND, NexusTaskOrchestrator
from tests.test_issue_111_controller_protocol import _Client, _Registry

ANSWER = '{"action":"answer","message":"done","reason":"done"}'


class _Result:
    def __init__(self, provider, success, content, metadata=None):
        self._data = {"provider": provider, "success": success, "content": content, "metadata": metadata or {}}

    def to_dict(self):
        return dict(self._data)


class _AzureDelegation:
    """Azure is configured; records what reaches it."""

    def __init__(self, *, chat_ok=True):
        self.chat_ok = chat_ok
        self.chats: list[str] = []
        self.delegated: list[str] = []

    def catalog(self):
        return [{"provider": "local_worker"}, {"provider": "azure_openai"}]

    async def azure_chat(self, prompt, *, max_tokens):
        self.chats.append(prompt)
        if self.chat_ok:
            return _Result("azure_openai", True, ANSWER)
        return _Result("azure_openai", False, "", {"error": "HTTP 500"})

    async def delegate(self, provider, task, *, context=""):
        self.delegated.append(provider)
        return _Result(provider, True, "findings")

    def close(self):
        pass


def _orchestrator(tmp, delegation, local_responses):
    return NexusTaskOrchestrator(
        operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
        experiment_service=MagicMock(),
        llama_client=_Client(local_responses),
        delegation_service=delegation,
    )


class ControllerProviderTests(unittest.TestCase):
    def _run(self, delegation, local_responses, env=None):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env or {}, clear=False):
            orchestrator = _orchestrator(tmp, delegation, local_responses)
            try:
                result = orchestrator.run("answer")
            finally:
                orchestrator.close()
            return result, orchestrator.llama

    def test_the_controller_runs_on_azure_when_configured(self):
        delegation = _AzureDelegation()
        result, local = self._run(delegation, [])
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(delegation.chats), 1)
        self.assertEqual(local.prompts, [])

    def test_the_whole_prompt_reaches_azure(self):
        delegation = _AzureDelegation()
        self._run(delegation, [])
        # The delegate wrapper cut prompts at 6000 characters.
        self.assertIn("Return exactly one next action.", delegation.chats[0])

    def test_the_local_model_takes_over_when_azure_fails(self):
        delegation = _AzureDelegation(chat_ok=False)
        result, local = self._run(delegation, [ANSWER])
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(local.prompts), 1)

    def test_local_can_be_forced(self):
        delegation = _AzureDelegation()
        result, local = self._run(delegation, [ANSWER], env={"NEXUS_CONTROLLER_PROVIDER": "local"})
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(delegation.chats, [])
        self.assertEqual(len(local.prompts), 1)


class StrongDelegateTests(unittest.TestCase):
    def test_a_local_worker_delegate_goes_to_azure(self):
        delegation = _AzureDelegation()
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = _orchestrator(tmp, delegation, [])
            try:
                observation = asyncio.run(orchestrator._delegate_with_fallback(
                    "local_worker", "Which issues are done?", reason="judge"
                ))
            finally:
                orchestrator.close()
        self.assertEqual(delegation.delegated, ["azure_openai"])
        self.assertEqual(observation["provider_used"], "azure_openai")
        self.assertEqual(observation["requested_provider"], "local_worker")


class GitHubWriteGuardTests(unittest.TestCase):
    def _command(self, command):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = _orchestrator(tmp, _AzureDelegation(), [])
            try:
                with patch.object(orchestrator, "_run_command_with_recovery", return_value={"tool": ACTION_COMMAND, "ran": True}) as run:
                    observation = asyncio.run(orchestrator._execute_tool({"tool": ACTION_COMMAND, "command": command}))
            finally:
                orchestrator.close()
        return observation, run

    def test_github_writes_are_refused(self):
        for command in (
            "gh issue close 158 --reason completed",
            "gh issue comment 336 --body 'done'",
            "gh pr merge 12 --squash",
            "gh api repos/tire0011/agent-lab/issues/1 -X PATCH -f state=closed",
            "gh api repos/tire0011/agent-lab/issues -f title=x",
        ):
            with self.subTest(command=command):
                observation, run = self._command(command)
                self.assertEqual(observation.get("error"), "github_write_refused")
                run.assert_not_called()

    def test_github_reads_still_run(self):
        for command in (
            "gh issue list --state open --json number,title",
            "gh issue view 336",
            "gh api repos/tire0011/agent-lab/issues?state=open --jq '.[].number'",
        ):
            with self.subTest(command=command):
                observation, run = self._command(command)
                self.assertNotIn("error", observation)
                run.assert_called_once()

    def test_live_repository_writes_are_refused(self):
        for command in (
            "git checkout -b fix-issue-238",
            "git apply /tmp/change.patch",
            "sed -i 's/old/new/' modules/task_orchestrator.py",
            "touch modules/new_feature.py",
            "printf x > modules/new_feature.py",
        ):
            with self.subTest(command=command):
                observation, run = self._command(command)
                self.assertEqual(observation.get("error"), "repository_write_refused")
                self.assertIn("new_experiment", observation.get("reason", ""))
                run.assert_not_called()

    def test_repository_reads_still_run(self):
        for command in (
            "git status --short",
            "git diff -- modules/task_orchestrator.py",
            "git log -1 --oneline",
            "sed -n '1,20p' modules/task_orchestrator.py",
        ):
            with self.subTest(command=command):
                observation, run = self._command(command)
                self.assertNotIn("error", observation)
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
