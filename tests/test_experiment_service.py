"""Unit tests for modules.experiment_service.ExperimentService.

All tests use a fake/mock CodexWorker and small temporary git repositories.
No real Codex process is invoked and no token quota is consumed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from modules.codex_worker import CodexResult

_ORIGINAL_SUBPROCESS_RUN = subprocess.run
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
    ExperimentNotFoundError,
    ExperimentNotRunnableError,
    ExperimentService,
    _build_direct_implementation_prompt,
    _build_implementation_prompt,
    _build_plan_prompt,
    _build_policy_replan_prompt,
    _build_reformat_retry_prompt,
    _build_research_prompt,
    _check_goal_policy,
    _extract_diagnostic_log,
    extract_single_json_object,
    parse_plan,
    plan_violates_policy,
)


# Sanitized, real Codex raw response captured while reproducing the
# originally rejected selection task (id 19afaac6981f421ebe4d5b4f22d74688)
# with the same goal prompt. No secrets were present in the original
# output; this is the actual text, used as a regression fixture for the
# JSON-extraction fix. It is valid, pure JSON on its own — the *original*
# rejected run's exact raw bytes are no longer available (the temporary
# experiment DB from that run was already cleaned up), so this capture is
# used both as a realistic "pure JSON" fixture and as the base for the
# "extra prose" / "code fence" / "nested braces in strings" variants below.
REAL_SANITIZED_PLAN_JSON = (
    '{"title":"Add unit tests for MCP filesystem client construction",'
    '"motivation":"IDEAS.md explicitly calls for tests covering the V1 MCP '
    'path, while modules/agent_clients.py currently has no dedicated tests. '
    'Testing its configuration boundary is small, low-risk, requires no '
    'credentials or live MCP process, and verifies the read-only tool '
    'restriction.",'
    '"affected_files":["tests/test_agent_clients.py"],'
    '"expected_changes":"Add isolated unittest coverage for '
    'build_filesystem_server using mocked SDK constructors. Verify default '
    'command and arguments, custom command and shlex-parsed arguments, '
    'AGENT_FS_ROOT propagation, and the exact read-only tool allowlist '
    'containing only list_directory and read_file.",'
    '"success_criteria":"The new tests pass without network access, Azure '
    'credentials, package installation, or launching an MCP subprocess.",'
    '"planned_tests":"Run python -m unittest tests.test_agent_clients -v.",'
    '"risks":"Mock assertions may need minor adjustment if the installed '
    'SDK changes constructor call shapes, e.g. calls like {inner: {a:1}} in '
    'examples.",'
    '"estimated_scope":"One new test file, approximately 6-8 unit tests."}'
)

VALID_PLAN_JSON = json.dumps(
    {
        "title": "Add a docstring test",
        "motivation": "Improve coverage of a small utility.",
        "affected_files": ["modules/foo.py", "tests/test_foo.py"],
        "expected_changes": "Add a docstring and a unit test.",
        "success_criteria": "New test passes.",
        "planned_tests": "tests/test_foo.py::test_docstring",
        "risks": "None significant.",
        "estimated_scope": "5 minutes",
    }
)

POLICY_VIOLATING_PLAN_JSON = json.dumps(
    {
        "title": "Create a new Azure VM",
        "motivation": "Need more compute.",
        "affected_files": ["infra/vm.tf"],
        "expected_changes": "Create a new Azure VM for the worker pool.",
        "success_criteria": "VM is running.",
        "planned_tests": "Manual check.",
        "risks": "Cost.",
        "estimated_scope": "1 hour",
    }
)

SECOND_VALID_PLAN_JSON = json.dumps(
    {
        "title": "Add a second docstring test",
        "motivation": "Improve coverage of another small utility.",
        "affected_files": ["modules/bar.py", "tests/test_bar.py"],
        "expected_changes": "Add a docstring and a unit test.",
        "success_criteria": "New test passes.",
        "planned_tests": "tests/test_bar.py::test_docstring",
        "risks": "None significant.",
        "estimated_scope": "5 minutes",
    }
)


def _make_result(**overrides) -> CodexResult:
    defaults = dict(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        runtime_ms=10,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
        thread_id="tid-1",
        final_message=VALID_PLAN_JSON,
        usage={"input_tokens": 100, "output_tokens": 20},
        task="task",
        working_dir="/agent",
        sandbox="read-only",
        error=None,
    )
    defaults.update(overrides)
    return CodexResult(**defaults)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "IDEAS.md").write_text("# IDEAS\n\n- small thing\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_placeholder.py").write_text(
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _write_affected_files_side_effect(message: str = "implemented"):
    """A reusable ``implementation_worker.run`` side effect for tests.

    Pretends to actually implement the selected plan by creating each file
    listed in the "Affected files: ..." line of the implementation prompt
    inside the (real, temporary) git worktree — so the run ends with a
    genuine, non-empty, plan-matching diff. This is required by
    ``modules.experiment_evaluation``'s deterministic quality gate: an
    "implementation" plan that produces no non-test file changes (as a
    bare ``MagicMock`` that never touches the filesystem would) is now
    correctly downgraded to ``needs_review`` rather than ``succeeded``.
    """

    def _run(*args, **kwargs):
        working_dir = kwargs.get("working_dir")
        task_text = kwargs.get("task") or (args[0] if args else "")
        match = re.search(r"Affected files:\s*(.+)", task_text)
        if working_dir and match:
            for rel_path in (p.strip() for p in match.group(1).split(",")):
                if not rel_path:
                    continue
                target = Path(working_dir) / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    if "test" in target.name:
                        target.write_text(
                            "def test_docstring():\n    assert True\n"
                        )
                    else:
                        target.write_text('"""Docstring."""\n')
        elif working_dir:
            # New single-call pipeline: no "Affected files:" in prompt.
            # Create a minimal non-test file so the quality gate sees at
            # least one production-code change.
            target = Path(working_dir) / "stub.py"
            if not target.exists():
                target.write_text('"""Stub implementation created by experiment."""\n')
        return _make_result(final_message=message, sandbox="workspace-write")

    return _run


class ExperimentServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary: investigated IDEAS.md and repo structure."
        )

        self.plan_worker = MagicMock()
        self.plan_worker.run.return_value = _make_result()
        self.plan_worker_factory_calls: list[str] = []

        def plan_factory(scratch_dir: str):
            self.plan_worker_factory_calls.append(scratch_dir)
            return self.plan_worker

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()
        self.worker_factory_calls: list[str] = []

        def factory(worktree_path: str):
            self.worker_factory_calls.append(worktree_path)
            return self.implementation_worker

        self.service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=plan_factory,
            worker_factory=factory,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
        )
        self.addCleanup(self.service.close)

    # ------------------------------------------------------------------
    # create_experiment
    # ------------------------------------------------------------------

    def test_create_experiment_is_queued(self):
        experiment = self.service.create_experiment(goal="Pick something small.")
        self.assertEqual(experiment.status, STATUS_QUEUED)
        self.assertEqual(experiment.goal, "Pick something small.")
        self.assertIsNone(experiment.plan)

        reloaded = self.service.get_experiment(experiment.id)
        self.assertEqual(reloaded.status, STATUS_QUEUED)

    def test_create_experiment_rejects_empty_goal(self):
        with self.assertRaises(ValueError):
            self.service.create_experiment(goal="   ")

    def test_get_unknown_experiment_raises(self):
        with self.assertRaises(ExperimentNotFoundError):
            self.service.get_experiment("does-not-exist")

    # ------------------------------------------------------------------
    # Selection phase behavior
    # ------------------------------------------------------------------

    @unittest.skip("research phase removed")
    def test_research_phase_runs_read_only_in_repo_root(self):
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        self.selection_worker.run.assert_called_once()
        _, kwargs = self.selection_worker.run.call_args
        self.assertEqual(kwargs["sandbox"], "read-only")
        self.assertEqual(kwargs["working_dir"], str(self.repo_root))
        self.assertLessEqual(kwargs["timeout_seconds"], 120)
        self.assertNotIn("output_schema", kwargs)

    @unittest.skip("plan phase removed")
    def test_plan_phase_uses_schema_but_isolated_scratch_dir(self):
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        self.plan_worker.run.assert_called_once()
        _, kwargs = self.plan_worker.run.call_args
        self.assertEqual(kwargs["sandbox"], "read-only")
        self.assertIsNotNone(kwargs.get("output_schema"))
        # Never repo_root: an isolated scratch dir, so even a stray tool
        # invocation could not do any further repository research.
        self.assertNotEqual(kwargs["working_dir"], str(self.repo_root))
        self.assertEqual(self.plan_worker_factory_calls, [kwargs["working_dir"]])
        # The scratch dir is intentionally not a git repository; codex
        # exec must be told to skip its trusted-git-repo check there.
        self.assertTrue(kwargs.get("skip_git_repo_check"))

    @unittest.skip("plan phase removed")
    def test_plan_phase_receives_research_summary(self):
        self.selection_worker.run.return_value = _make_result(
            final_message="RESEARCH_MARKER: found interesting file X."
        )
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        _, kwargs = self.plan_worker.run.call_args
        self.assertIn("RESEARCH_MARKER: found interesting file X.", kwargs["task"])

    @unittest.skip("research/plan phases removed")
    def test_research_runs_before_plan(self):
        call_order: list[str] = []
        self.selection_worker.run.side_effect = lambda **kw: (
            call_order.append("research") or _make_result(final_message="research text")
        )
        self.plan_worker.run.side_effect = lambda **kw: (
            call_order.append("plan") or _make_result()
        )
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)
        self.assertEqual(call_order, ["research", "plan"])

    @unittest.skip("research phase removed")
    def test_research_phase_diagnostic_log_may_contain_tool_events(self):
        self.selection_worker.run.return_value = _make_result(
            final_message="Investigated the repo.",
            stdout=json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "i0",
                        "type": "command_execution",
                        "command": "cat IDEAS.md",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ) + "\n",
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertIsNotNone(result.research_diagnostic_log)
        self.assertIn("command_execution", result.research_diagnostic_log)

    @unittest.skip("research phase removed")
    def test_research_raw_summary_is_size_limited(self):
        long_text = "y" * 20000
        self.selection_worker.run.return_value = _make_result(final_message=long_text)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertLessEqual(
            len(result.research_raw_summary.encode("utf-8")), self.service.max_raw_message_bytes + 64
        )
        self.assertIn("truncated", result.research_raw_summary)

    @unittest.skip("research phase removed")
    def test_research_raw_summary_secrets_are_redacted(self):
        leaky_message = "Found config with AZURE_OPENAI_API_KEY=sk-verysecretvalue1234"
        self.selection_worker.run.return_value = _make_result(final_message=leaky_message)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertNotIn("sk-verysecretvalue1234", result.research_raw_summary or "")
        self.assertIn("[REDACTED]", result.research_raw_summary or "")

    @unittest.skip("research phase removed")
    def test_research_diagnostic_log_excludes_reasoning_and_full_command_output(self):
        self.selection_worker.run.return_value = _make_result(
            final_message="Investigated the repo.",
            stdout="\n".join([
                json.dumps({
                    "type": "item.completed",
                    "item": {"id": "r0", "type": "reasoning", "text": "secret chain of thought"},
                }),
                json.dumps({
                    "type": "item.completed",
                    "item": {
                        "id": "c0",
                        "type": "command_execution",
                        "command": "cat IDEAS.md",
                        "exit_code": 0,
                        "status": "completed",
                        "aggregated_output": "full file contents that must not be persisted",
                    },
                }),
            ]),
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertNotIn("secret chain of thought", result.research_diagnostic_log or "")
        self.assertNotIn("full file contents that must not be persisted", result.research_diagnostic_log or "")
        self.assertIn("command_execution", result.research_diagnostic_log or "")

    @unittest.skip("research/plan phases removed")
    def test_research_placeholder_text_not_parsed_as_plan(self):
        # The research phase may itself emit prose that superficially
        # mentions JSON-like content; it must never be fed into
        # parse_plan()/plan_violates_policy() directly.
        self.selection_worker.run.return_value = _make_result(
            final_message='Still investigating... maybe {"title": "NONE"} later.'
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        # The plan actually used/stored must come from the plan_worker's
        # (schema-conformant, mocked) response, not the research prose.
        self.assertNotEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.plan["title"], "Add a docstring test")

    def test_accepted_plan_creates_isolated_worktree(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertIsNotNone(result.worktree_path)
        self.assertTrue(Path(result.worktree_path).is_dir())
        self.assertTrue(result.branch_name.startswith("experiment/"))
        # Main repo untouched: still on its original branch, no local changes.
        status = subprocess.run(
            ["git", "-C", str(self.repo_root), "status", "--short"],
            capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "")

    def test_implementation_uses_workspace_write_only_in_worktree(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.implementation_worker.run.assert_called_once()
        _, kwargs = self.implementation_worker.run.call_args
        self.assertEqual(kwargs["sandbox"], "workspace-write")
        self.assertEqual(kwargs["working_dir"], result.worktree_path)
        self.assertEqual(self.worker_factory_calls, [result.worktree_path])

    def test_main_working_directory_unchanged_after_run(self):
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        status = subprocess.run(
            ["git", "-C", str(self.repo_root), "status", "--short"],
            capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "")
        branch = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        self.assertNotIn("experiment/", branch.stdout)

    def test_worker_called_exactly_once_each_phase(self):
        # Single-call pipeline: only the implementation worker is invoked.
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)
        self.selection_worker.run.assert_not_called()
        self.plan_worker.run.assert_not_called()
        self.implementation_worker.run.assert_called_once()

    def test_succeeded_when_tests_pass(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.test_exit_code, 0)
        self.assertIn("OK", result.test_output or "")

    def test_test_command_uses_configured_python_interpreter(self):
        # Default interpreter is the main repo's own venv python so a
        # worktree without its own virtualenv can still run tests.
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.test_command[0], self.service.test_python_executable)

    def test_test_command_interpreter_is_configurable(self):
        custom_python = sys.executable
        service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=lambda p: self.plan_worker,
            worker_factory=lambda p: self.implementation_worker,
            db_path=str(Path(self.tmp.name) / "agent2.db"),
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            test_python_executable=custom_python,
        )
        self.addCleanup(service.close)
        experiment = service.create_experiment(goal="g")
        result = service.run_experiment(experiment.id)
        self.assertEqual(result.test_command[0], custom_python)

    @patch("subprocess.run")
    def test_test_command_runs_with_worktree_as_cwd(self, mock_run):
        # Route only git subprocess calls to the real binary; assert the
        # final (test) subprocess.run call uses the worktree as cwd and
        # the configured interpreter, without invoking any real command.
        real_run = _ORIGINAL_SUBPROCESS_RUN

        def fake_run(args, **kwargs):
            if args and args[0] == "git":
                return real_run(args, **kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="OK", stderr="")

        mock_run.side_effect = fake_run
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        test_calls = [
            c for c in mock_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] != "git"
        ]
        self.assertEqual(len(test_calls), 1)
        _, kwargs = test_calls[0]
        self.assertEqual(kwargs["cwd"], result.worktree_path)

    @patch("subprocess.run")
    def test_test_command_uses_isolated_env_not_full_parent_env(self, mock_run):
        """Regression test for the leaked-production-secret bug: the test
        subprocess must receive the isolated verification environment
        (AGENT_API_KEY=test-key), never a copy of the parent process
        environment, even when the parent has a fake production key and
        other secrets set."""
        real_run = _ORIGINAL_SUBPROCESS_RUN

        def fake_run(args, **kwargs):
            if args and args[0] == "git":
                return real_run(args, **kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="OK", stderr="")

        mock_run.side_effect = fake_run

        with patch.dict(
            os.environ,
            {
                "AGENT_API_KEY": "fake-production-secret-should-not-leak",
                "AZURE_OPENAI_API_KEY": "azure-secret-should-not-leak",
            },
        ):
            experiment = self.service.create_experiment(goal="g")
            self.service.run_experiment(experiment.id)

        test_calls = [
            c for c in mock_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] != "git"
        ]
        self.assertEqual(len(test_calls), 1)
        _, kwargs = test_calls[0]
        env = kwargs.get("env")
        self.assertIsNotNone(env, "test subprocess must be given an explicit isolated env")
        self.assertEqual(env["AGENT_API_KEY"], "test-key")
        self.assertNotIn("AZURE_OPENAI_API_KEY", env)
        self.assertNotIn("fake-production-secret-should-not-leak", env.values())

    def test_no_venv_copied_or_installed_into_worktree(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertFalse((Path(result.worktree_path) / ".venv").exists())

    def test_git_status_diff_and_test_output_persisted(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertIsNotNone(result.git_status)
        self.assertIsNotNone(result.diff_stat)
        self.assertIsNotNone(result.test_command)
        self.assertIsNotNone(result.test_output)

    def test_untracked_file_is_in_combined_diff_without_changing_real_index(self):
        untracked_path = "new_experiment_file.txt"
        untracked_contents = "new untracked contents\nsecond line\n"
        index_before: bytes | None = None
        index_path: Path | None = None

        def factory(path):
            nonlocal index_before, index_path
            git_index = subprocess.run(
                ["git", "-C", path, "rev-parse", "--git-path", "index"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            index_path = Path(git_index)
            if not index_path.is_absolute():
                index_path = Path(path) / index_path
            index_before = index_path.read_bytes()
            Path(path, untracked_path).write_text(untracked_contents)
            return self.implementation_worker

        self.service.worker_factory = factory
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertIn(f"?? {untracked_path}", result.git_status or "")
        self.assertIn(untracked_path, result.diff_stat or "")
        self.assertIn(f"+++ b/{untracked_path}", result.diff_text or "")
        self.assertIn("+new untracked contents", result.diff_text or "")
        self.assertEqual(index_path.read_bytes(), index_before)

        reloaded = self.service.get_experiment(experiment.id)
        self.assertEqual(reloaded.diff_stat, result.diff_stat)
        self.assertEqual(reloaded.diff_text, result.diff_text)
        serialized = reloaded.to_dict()
        self.assertIn(untracked_path, serialized["diff_stat"])
        self.assertIn("+new untracked contents", serialized["diff_text"])

    def test_usage_of_implementation_phase_persisted(self):
        # Single-call pipeline: only implementation usage is recorded.
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.implementation_usage, {"input_tokens": 100, "output_tokens": 20})
        self.assertIsNotNone(result.implementation_runtime_ms)
        self.assertIsNone(result.selection_usage)
        self.assertIsNone(result.research_usage)

    # ------------------------------------------------------------------
    # Rejection handling
    # ------------------------------------------------------------------

    @unittest.skip("plan phase removed")
    def test_none_plan_is_rejected(self):
        self.plan_worker.run.return_value = _make_result(
            final_message=json.dumps({"title": "NONE", "motivation": "nothing small enough"})
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.policy_rule, "unparseable_plan")
        self.implementation_worker.run.assert_not_called()

    @unittest.skip("plan phase removed")
    def test_unparseable_plan_is_rejected(self):
        self.plan_worker.run.return_value = _make_result(final_message="not structured at all")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.policy_rule, "unparseable_plan")
        self.implementation_worker.run.assert_not_called()

    def test_policy_violation_persists_matched_policy_rule(self):
        # Goal policy check: commit keyword → rejected with disallowed_keyword rule.
        experiment = self.service.create_experiment(
            goal="Implement the feature then run git commit and push."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.policy_rule, "disallowed_keyword")
        self.assertIsNotNone(result.error)
        self.implementation_worker.run.assert_not_called()

    def test_resolve_experiment_accepts_unambiguous_prefix(self):
        experiment = self.service.create_experiment(goal="g")
        prefix = experiment.id[:8]
        resolved = self.service.resolve_experiment(prefix)
        self.assertEqual(resolved.id, experiment.id)

    def test_resolve_experiment_rejects_short_prefix(self):
        experiment = self.service.create_experiment(goal="g")
        with self.assertRaises(ExperimentNotFoundError):
            self.service.resolve_experiment(experiment.id[:7])

    def test_resolve_experiment_rejects_ambiguous_prefix(self):
        # Force two experiment ids sharing a common 8-char prefix by
        # inserting directly (uuid4 ids won't collide naturally).
        shared_prefix = "abcdef01"
        with self.service._lock, self.service._conn:
            for suffix in ("11111111111111111111111111", "22222222222222222222222222"):
                self.service._conn.execute(
                    "INSERT INTO experiments (id, goal, status, created_at) VALUES (?, ?, ?, ?)",
                    (shared_prefix + suffix, "g", STATUS_QUEUED, "2026-08-13T00:00:00Z"),
                )
        with self.assertRaises(ExperimentNotFoundError):
            self.service.resolve_experiment(shared_prefix)

    def test_goal_mentioning_commit_is_rejected(self):
        # Security check on goal text: "commit" triggers the denylist.
        experiment = self.service.create_experiment(
            goal="Implement the feature then run git commit and push."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_mentioning_package_install_is_rejected(self):
        # Security check on goal text: "pip install" triggers the denylist.
        experiment = self.service.create_experiment(
            goal="Run pip install requests then use it in the code."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_with_harmless_vm_mention_is_not_rejected(self):
        # A bare VM mention without a paired action verb must not block the goal.
        experiment = self.service.create_experiment(
            goal=(
                "Fix a typo in README.md. The current VM is Standard_B1s "
                "per OPERATIONS.md. No infrastructure action is needed."
            )
        )
        result = self.service.run_experiment(experiment.id)
        self.assertNotEqual(result.status, STATUS_REJECTED)

    def test_goal_creating_vm_is_rejected(self):
        experiment = self.service.create_experiment(
            goal="Create a new Azure VM for the worker pool."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_deleting_vm_is_rejected(self):
        experiment = self.service.create_experiment(
            goal="Delete the Azure VM that is no longer needed."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_deploying_to_production_is_rejected(self):
        experiment = self.service.create_experiment(
            goal="Deploy the application to the production environment."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_with_cost_action_is_rejected(self):
        # "enable billing" is in the hard keyword denylist.
        experiment = self.service.create_experiment(
            goal="Enable billing to provision new paid resources."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_plan_with_local_only_code_change_is_not_rejected(self):
        # A plan with a purely local code/test change and no infra/deploy
        # action must not be blocked.
        plan = json.loads(VALID_PLAN_JSON)
        self.plan_worker.run.return_value = _make_result(final_message=json.dumps(plan))
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertNotEqual(result.status, STATUS_REJECTED)

    def test_bare_secret_mention_with_negation_is_accepted(self):
        # Negated mentions in the goal should not trigger the secret-action check.
        experiment = self.service.create_experiment(
            goal="Improve redaction logic to ensure secrets are not logged."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertNotEqual(result.status, STATUS_REJECTED)

    def test_goal_reading_secret_from_env_is_rejected(self):
        experiment = self.service.create_experiment(
            goal="Read the API key from the environment and use it in requests."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    @unittest.skip("plan-specific check removed")
    def test_plan_touching_too_many_files_is_rejected(self):
        plan = json.loads(VALID_PLAN_JSON)
        plan["affected_files"] = [f"modules/f{i}.py" for i in range(6)]
        self.plan_worker.run.return_value = _make_result(final_message=json.dumps(plan))
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_rejected_experiment_never_creates_worktree(self):
        # Goal policy rejection must happen before any worktree is created.
        experiment = self.service.create_experiment(
            goal="Run git commit and push after implementing the change."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIsNone(result.worktree_path)
        self.assertFalse(self.worktrees_root.exists() and any(self.worktrees_root.iterdir()))

    def test_second_start_is_rejected(self):
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)
        with self.assertRaises(ExperimentNotRunnableError):
            self.service.run_experiment(experiment.id)
        # Implementation worker still only called once despite second attempt.
        self.assertEqual(self.implementation_worker.run.call_count, 1)

    def test_run_unknown_experiment_raises(self):
        with self.assertRaises(ExperimentNotFoundError):
            self.service.run_experiment("nope")

    # ------------------------------------------------------------------
    # Timeout / exception handling never leaves an active status
    # ------------------------------------------------------------------

    @unittest.skip("research phase removed")
    def test_research_timeout_yields_timed_out_not_active(self):
        self.selection_worker.run.return_value = _make_result(
            timed_out=True, success=False, error="Codex process timed out after 120s"
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_TIMED_OUT)
        self.plan_worker.run.assert_not_called()
        self.implementation_worker.run.assert_not_called()

    @unittest.skip("research phase removed")
    def test_research_exception_yields_failed_not_active(self):
        self.selection_worker.run.side_effect = RuntimeError("boom")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("boom", result.error)
        self.plan_worker.run.assert_not_called()

    @unittest.skip("plan phase removed")
    def test_plan_timeout_yields_timed_out_not_active(self):
        self.plan_worker.run.return_value = _make_result(
            timed_out=True, success=False, error="Codex process timed out after 90s"
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_TIMED_OUT)
        self.implementation_worker.run.assert_not_called()

    @unittest.skip("plan phase removed")
    def test_plan_exception_yields_failed_not_active(self):
        self.plan_worker.run.side_effect = RuntimeError("plan-boom")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("plan-boom", result.error)

    def test_implementation_exception_yields_failed_not_active(self):
        self.implementation_worker.run.side_effect = RuntimeError("kaboom")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("kaboom", result.error)

    def test_implementation_timeout_yields_timed_out(self):
        self.implementation_worker.run.side_effect = None
        self.implementation_worker.run.return_value = _make_result(
            timed_out=True, success=False, sandbox="workspace-write",
            error="Codex process timed out after 1200s",
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_TIMED_OUT)

    # ------------------------------------------------------------------
    # needs_review when a safe test command cannot be determined
    # ------------------------------------------------------------------

    def test_missing_tests_dir_yields_needs_review(self):
        # This scenario is about the test-runner being unable to find a
        # tests/ dir at all — it must not be masked by the implementation
        # side effect recreating tests/test_foo.py, so revert to a plain
        # (no filesystem writes) mock for this test only.
        self.implementation_worker.run.side_effect = None
        self.implementation_worker.run.return_value = _make_result(
            final_message="implemented", sandbox="workspace-write"
        )
        # Remove the tests/ dir the worktree would have inherited from HEAD.
        import shutil as _shutil
        _shutil.rmtree(self.repo_root / "tests")
        subprocess.run(["git", "-C", str(self.repo_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo_root), "commit", "-q", "-m", "remove tests"], check=True
        )

        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)

    def test_failing_tests_yield_failed_status(self):
        (self.repo_root / "tests" / "test_placeholder.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_fail(self):\n"
            "        self.assertTrue(False)\n"
        )
        subprocess.run(["git", "-C", str(self.repo_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo_root), "commit", "-q", "-m", "failing test"], check=True
        )

        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotEqual(result.test_exit_code, 0)

    # ------------------------------------------------------------------
    # Size limits
    # ------------------------------------------------------------------

    def test_diff_size_limit_is_applied(self):
        svc = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=lambda p: self.plan_worker,
            worker_factory=lambda wt: self.implementation_worker,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            max_diff_bytes=10,
        )
        self.addCleanup(svc.close)

        self.worker_factory_calls_large: list[str] = []

        def factory2(path):
            self.worker_factory_calls_large.append(path)
            # Exercise the cap on newly included untracked-file content.
            Path(path, "large_untracked.txt").write_text(("x" * 5000) + "\n")
            return self.implementation_worker

        svc.worker_factory = factory2

        experiment = svc.create_experiment(goal="g")
        result = svc.run_experiment(experiment.id)
        self.assertIn("truncated", result.diff_text or "")

    # ------------------------------------------------------------------
    # No secrets in stored output
    # ------------------------------------------------------------------

    def test_no_secrets_in_stored_test_output_or_error(self):
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        blob = json.dumps(result.to_dict())
        self.assertNotIn("AGENT_API_KEY", blob)
        self.assertNotIn("AZURE_OPENAI_API_KEY", blob)


class ParsePlanTestCase(unittest.TestCase):
    def test_parses_valid_json(self):
        plan = parse_plan(VALID_PLAN_JSON)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.title, "Add a docstring test")
        self.assertEqual(plan.affected_files, ["modules/foo.py", "tests/test_foo.py"])

    def test_parses_json_in_fenced_block(self):
        text = f"Here is my plan:\n```json\n{VALID_PLAN_JSON}\n```\nDone."
        plan = parse_plan(text)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.title, "Add a docstring test")

    def test_returns_none_for_garbage(self):
        self.assertIsNone(parse_plan("I refuse to answer in structured form."))

    def test_returns_none_for_empty(self):
        self.assertIsNone(parse_plan(""))
        self.assertIsNone(parse_plan(None))

    def test_returns_none_when_missing_required_field(self):
        data = json.loads(VALID_PLAN_JSON)
        del data["risks"]
        self.assertIsNone(parse_plan(json.dumps(data)))

    # ------------------------------------------------------------------
    # Regression tests using the real sanitized Codex output that
    # originally triggered the naive-brace-slicing bug (task
    # 19afaac6981f421ebe4d5b4f22d74688).
    # ------------------------------------------------------------------

    def test_parses_real_sanitized_pure_json(self):
        plan = parse_plan(REAL_SANITIZED_PLAN_JSON)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan.title, "Add unit tests for MCP filesystem client construction"
        )

    def test_parses_real_sanitized_json_in_code_block(self):
        text = f"```json\n{REAL_SANITIZED_PLAN_JSON}\n```"
        plan = parse_plan(text)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan.title, "Add unit tests for MCP filesystem client construction"
        )

    def test_parses_real_sanitized_json_with_surrounding_prose(self):
        text = (
            "Sure, here is my selected plan for this run:\n\n"
            f"{REAL_SANITIZED_PLAN_JSON}\n\n"
            "Let me know if you need anything else."
        )
        plan = parse_plan(text)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan.title, "Add unit tests for MCP filesystem client construction"
        )

    def test_braces_inside_json_string_values_do_not_break_extraction(self):
        # The "risks" field of the real sanitized fixture intentionally
        # contains a brace-laden example ("calls like {inner: {a:1}} in
        # examples"). The naive first/last brace slicing bug would have
        # been confused here: braces inside a JSON *string value* must not
        # be treated as new top-level object boundaries.
        text = (
            f"{REAL_SANITIZED_PLAN_JSON}\n\n"
            "Note: some unrelated mention of braces, e.g. { incomplete "
            "fragment without a closing brace, is not part of the plan."
        )
        plan = parse_plan(text)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan.title, "Add unit tests for MCP filesystem client construction"
        )
        self.assertIn("{inner: {a:1}}", plan.risks)

    def test_multiple_json_objects_are_rejected(self):
        text = f"{REAL_SANITIZED_PLAN_JSON}\n\n{VALID_PLAN_JSON}"
        self.assertIsNone(extract_single_json_object(text))
        self.assertIsNone(parse_plan(text))

    def test_invalid_json_is_rejected(self):
        self.assertIsNone(extract_single_json_object("{not: valid, json}"))
        self.assertIsNone(parse_plan("{not: valid, json}"))


class PlanPolicyTestCase(unittest.TestCase):
    def test_clean_plan_passes(self):
        plan = parse_plan(VALID_PLAN_JSON)
        self.assertIsNone(plan_violates_policy(plan, VALID_PLAN_JSON))

    def test_shell_true_keyword_rejected(self):
        plan = parse_plan(VALID_PLAN_JSON)
        self.assertIsNotNone(plan_violates_policy(plan, VALID_PLAN_JSON + " shell=True"))

    def test_path_outside_repo_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["affected_files"] = ["/etc/passwd"]
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    # ------------------------------------------------------------------
    # Action-based infrastructure/deployment policy (replaces the old
    # bare-keyword denylist for VM/Azure/deploy/infrastructure).
    # ------------------------------------------------------------------

    def test_bare_vm_mention_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["motivation"] = (
            "README.md still says the VM runs Standard_B1s on Azure, but "
            "IDEAS.md records that it was already resized to Standard_B2s."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_bare_deploy_and_infrastructure_mention_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["risks"] = (
            "None; this change does not touch deploy scripts or "
            "infrastructure, it only fixes a docstring."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_creating_vm_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Create a new Azure VM for the worker pool."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        reason = plan_violates_policy(plan, json.dumps(data))
        self.assertIsNotNone(reason)
        self.assertIn("infrastructure/deployment", reason)

    def test_deleting_vm_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Delete the old VM once migration finishes."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_deploying_to_production_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Deploy the new build to the production server."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_cloud_resource_change_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Resize the resource group's storage account."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_cost_action_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["risks"] = "Requires purchasing additional compute credits."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_push_and_merge_are_still_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Implement the change, then push and merge it."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_local_code_change_is_accepted(self):
        plan = parse_plan(VALID_PLAN_JSON)
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, VALID_PLAN_JSON))


class PolicyActionSecretTestCase(unittest.TestCase):
    """Action-based secret/credential policy (replaces the old bare-keyword
    denylist for secret/credential/token/api key/ssh key)."""

    def test_without_secrets_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["success_criteria"] = (
            "The new tests pass without network access, MCP startup, "
            "secrets, or production code changes."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_no_api_key_required_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["risks"] = "No API key required for this test."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_does_not_access_credentials_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["motivation"] = "This change does not access credentials at any point."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_redaction_test_mentioning_token_is_accepted(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "Add unit tests asserting that the redaction helper replaces "
            "words like token and secret with a fixed [REDACTED] marker."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_reading_secret_from_env_var_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Reads the secret from an environment variable to authenticate."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_loading_and_using_api_key_from_file_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Loads the api key from a file and uses it to call the service."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_printing_or_logging_token_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Logs the token to the console for debugging."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_copying_or_transmitting_credentials_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Copies credentials to a new location and transmits them to a backup server."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_generating_or_changing_ssh_key_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Generates a new ssh key for the deploy user."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_deleting_ssh_key_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = "Deletes the old ssh key that is no longer used."
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(data)))

    def test_original_mcp_test_plan_is_now_accepted(self):
        # Regression fixture: this is the exact plan text that was
        # incorrectly rejected by the old bare "secret" keyword hit in
        # "...without network access, MCP startup, secrets, or production
        # code changes." It must now be accepted.
        data = {
            "title": "Add focused unit tests for MCP filesystem client construction",
            "motivation": (
                "This directly advances IDEAS.md's highest-priority MCP V1 work "
                "and its explicit request for MCP path success/failure tests, "
                "while adding measurable coverage without changing production "
                "behavior or requiring external services, credentials, or "
                "model quota."
            ),
            "affected_files": ["tests/test_agent_clients.py"],
            "expected_changes": (
                "Create unit tests that mock MCPServerStdio and "
                "create_static_tool_filter in modules.agent_clients, verifying "
                "the default filesystem-server command and arguments, "
                "environment-variable overrides with shlex parsing, and a "
                "strict tool allowlist containing only list_directory and "
                "read_file. Also verify malformed argument parsing and "
                "constructor failures propagate without launching MCP."
            ),
            "success_criteria": (
                "The new tests deterministically validate default "
                "construction, configuration overrides, read-only filtering, "
                "and failure propagation; they pass without network access, "
                "MCP startup, secrets, or production code changes."
            ),
            "planned_tests": "Run the targeted unittest command for tests/test_agent_clients.py.",
            "risks": "Low. Mock assertions may need minor adjustment if the SDK changes.",
            "estimated_scope": "One new test file, approximately 5-7 unit tests.",
        }
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_negation_does_not_cross_sentence_boundary_to_hide_real_action(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "This change does not touch infrastructure at all. "
            "Reads the secret from the vault and uses it to call the API."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        reason = plan_violates_policy(plan, json.dumps(data))
        self.assertIsNotNone(reason)

    def test_secrets_are_absent_from_output_is_not_rejected(self):
        # Regression: "absent"/"excluded"/"omitted" describe the lack of a
        # secret, not an action performed on one. The old matcher treated
        # the ambiguous verb "output" (a common noun in phrases like
        # "activity output") as a real disclosure action whenever a secret
        # noun appeared anywhere nearby in the same sentence.
        data = json.loads(VALID_PLAN_JSON)
        data["planned_tests"] = (
            "Add a failure-path assertion that raw commands, paths, stack "
            "traces, and secrets are absent from public activity output."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_credentials_excluded_from_response_is_not_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "Ensure credentials are excluded from the public response and "
            "tokens are omitted from any returned field."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_security_test_asserting_non_disclosure_is_not_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["planned_tests"] = (
            "Add a security test asserting the api key and other secrets "
            "never appear in the response, and that credentials are not "
            "present in logs."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))

    def test_real_secret_action_still_rejected_after_absent_fix(self):
        # Ensure the narrow negation-word addition did not weaken real
        # detection: a genuine action ("store the api key") elsewhere in
        # the same plan must still be rejected even though the plan also
        # contains defensive, non-disclosure language.
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "Secrets are absent from the public response, but internally "
            "the plan will store the api key in the database for reuse."
        )
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        reason = plan_violates_policy(plan, json.dumps(data))
        self.assertIsNotNone(reason)
        self.assertIn("api key", reason)

    def test_outbox_view_plan_is_now_accepted(self):
        # Regression fixture: this is the exact plan produced for the
        # "Add public sanitized Outbox view" goal that was previously
        # rejected with "plan describes an actual secret/credential
        # action: 'secrets are absent from public activity output'" due
        # to the ambiguous "output" verb false-positive. It must now pass.
        data = {
            "title": "Cover untracked files in experiment diff collection",
            "motivation": (
                "Improve experiment observability by ensuring newly "
                "created, untracked files are represented in collected "
                "diffs instead of being silently omitted. This is a "
                "distinct, narrowly scoped fix identified in IDEAS.md and "
                "does not alter authentication or public data exposure."
            ),
            "affected_files": ["api.py", "tests/test_activity_api.py"],
            "expected_changes": (
                "Adjust the existing experiment diff-collection path to "
                "include bounded diff information for untracked files "
                "using repository-native Git behavior, while preserving "
                "the current sanitized public activity response and "
                "avoiding raw command errors, filesystem details, or "
                "environment data."
            ),
            "success_criteria": (
                "Experiment results account for both tracked modifications "
                "and newly created untracked files; existing tracked-file "
                "behavior remains unchanged; collection failures remain "
                "sanitized through the current activity patterns; and no "
                "dependency, infrastructure, authentication, or deployment "
                "changes are introduced."
            ),
            "planned_tests": (
                "Add focused coverage with a temporary repository "
                "containing tracked edits and an untracked file, asserting "
                "both are reflected by diff collection. Add a failure-path "
                "assertion that raw commands, paths, stack traces, and "
                "secrets are absent from public activity output. Run the "
                "complete test suite."
            ),
            "risks": (
                "Untracked files may be binary, large, or sensitive. Keep "
                "collection bounded, exclude unsupported binary content, "
                "reuse existing sanitization behavior, and test that raw "
                "file contents and operational errors cannot leak through "
                "public responses."
            ),
            "estimated_scope": (
                "Small change limited to the existing diff-collection "
                "implementation and its closely related activity tests; no "
                "new dependencies and implementable within 20 minutes."
            ),
        }
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, json.dumps(data)))


@unittest.skip("reformat-retry phase removed")
class ReformatRetryTestCase(unittest.TestCase):
    """Covers the exactly-one-shot read-only reformat retry."""

    UNPARSEABLE_TEXT = "I looked around but I will just describe it in prose."

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary: looked at IDEAS.md and repo layout."
        )
        self.plan_worker = MagicMock()

        def plan_factory(scratch_dir: str):
            return self.plan_worker

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()

        def factory(worktree_path: str):
            return self.implementation_worker

        self.service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=plan_factory,
            worker_factory=factory,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
        )
        self.addCleanup(self.service.close)

    def test_retry_is_triggered_exactly_once_on_parse_failure(self):
        # First call (selection) returns unparseable prose; second call
        # (the reformat retry) returns valid JSON.
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=VALID_PLAN_JSON, runtime_ms=5),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 2)
        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertIsNotNone(result.reformat_retry_task_id)
        self.assertEqual(result.reformat_retry_runtime_ms, 5)

    def test_retry_prompt_does_not_ask_to_select_new_feature(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        retry_call = self.plan_worker.run.call_args_list[1]
        retry_prompt = retry_call.kwargs.get("task") or retry_call.args[0]
        self.assertIn("Do not select or modify the feature", retry_prompt)
        self.assertIn(self.UNPARSEABLE_TEXT, retry_prompt)
        self.assertEqual(retry_call.kwargs.get("sandbox"), "read-only")
        self.assertEqual(retry_call.kwargs.get("timeout_seconds"), 60)

    def test_failed_retry_ends_rejected_not_active(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message="still just prose, no json here"),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 2)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIsNotNone(result.reformat_retry_task_id)
        self.implementation_worker.run.assert_not_called()

    def test_no_second_retry_is_ever_issued(self):
        # Even though the retry also fails, the worker must only ever be
        # called twice in total (one selection + one retry), never three
        # or more times.
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=self.UNPARSEABLE_TEXT),
        ]
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)
        self.assertEqual(self.plan_worker.run.call_count, 2)

    def test_successful_retry_continues_normal_flow_and_creates_worktree(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertTrue(Path(result.worktree_path).exists())
        self.implementation_worker.run.assert_called_once()

    def test_retry_usage_and_task_id_are_persisted(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(
                final_message=VALID_PLAN_JSON,
                usage={"input_tokens": 42, "output_tokens": 7},
            ),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertIsNotNone(result.reformat_retry_task_id)
        self.assertEqual(result.reformat_retry_usage, {"input_tokens": 42, "output_tokens": 7})

        reloaded = self.service.get_experiment(experiment.id)
        self.assertEqual(reloaded.reformat_retry_task_id, result.reformat_retry_task_id)
        self.assertEqual(reloaded.reformat_retry_usage, {"input_tokens": 42, "output_tokens": 7})

    def test_no_retry_when_plan_valid_on_first_attempt(self):
        self.plan_worker.run.return_value = _make_result(final_message=VALID_PLAN_JSON)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 1)
        self.assertIsNone(result.reformat_retry_task_id)

    def test_policy_violation_still_rejected_after_successful_retry(self):
        bad_plan = json.loads(VALID_PLAN_JSON)
        bad_plan["affected_files"] = ["/etc/passwd"]
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=json.dumps(bad_plan)),
            _make_result(final_message=json.dumps(bad_plan)),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_no_implementation_before_successful_validation(self):
        # Both selection and retry fail to parse: implementation must
        # never be invoked.
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=self.UNPARSEABLE_TEXT),
        ]
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)
        self.implementation_worker.run.assert_not_called()

    def test_plan_phase_uses_plan_schema(self):
        from modules.experiment_service import PLAN_JSON_SCHEMA

        self.plan_worker.run.return_value = _make_result(final_message=VALID_PLAN_JSON)
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        call_kwargs = self.plan_worker.run.call_args.kwargs
        self.assertEqual(call_kwargs.get("output_schema"), PLAN_JSON_SCHEMA)

    def test_retry_uses_same_plan_schema(self):
        from modules.experiment_service import PLAN_JSON_SCHEMA

        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        retry_call_kwargs = self.plan_worker.run.call_args_list[1].kwargs
        self.assertEqual(retry_call_kwargs.get("output_schema"), PLAN_JSON_SCHEMA)

    def test_native_schema_output_still_validated_against_policy(self):
        # Even if the worker "natively" honors the schema, a policy
        # violation embedded in the resulting JSON must still be rejected;
        # the schema alone must never be trusted to enforce security
        # boundaries.
        bad_plan = json.loads(VALID_PLAN_JSON)
        bad_plan["affected_files"] = ["/etc/passwd"]
        self.plan_worker.run.return_value = _make_result(final_message=json.dumps(bad_plan))
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_native_schema_output_still_requires_all_fields(self):
        incomplete_plan = json.loads(VALID_PLAN_JSON)
        del incomplete_plan["risks"]
        self.plan_worker.run.return_value = _make_result(
            final_message=json.dumps(incomplete_plan)
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)

    def test_selection_usage_persisted_even_when_rejected(self):
        self.plan_worker.run.return_value = _make_result(
            final_message=self.UNPARSEABLE_TEXT,
            usage={"input_tokens": 11, "output_tokens": 22},
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.selection_usage, {"input_tokens": 11, "output_tokens": 22})

    # ------------------------------------------------------------------
    # Raw message diagnosability (selection_raw_message / reformat_raw_message)
    # ------------------------------------------------------------------

    def test_selection_raw_message_stored_on_success(self):
        self.plan_worker.run.return_value = _make_result(final_message=VALID_PLAN_JSON)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.selection_raw_message, VALID_PLAN_JSON)

    def test_selection_raw_message_stored_on_rejected(self):
        self.plan_worker.run.return_value = _make_result(final_message=self.UNPARSEABLE_TEXT)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.selection_raw_message, self.UNPARSEABLE_TEXT)

    def test_reformat_raw_message_stored_separately(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT),
            _make_result(final_message=VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.selection_raw_message, self.UNPARSEABLE_TEXT)
        self.assertEqual(result.reformat_raw_message, VALID_PLAN_JSON)

    def test_empty_final_message_is_diagnosable(self):
        self.plan_worker.run.return_value = _make_result(final_message="")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        # Empty string, not None: distinguishable from "never captured".
        self.assertEqual(result.selection_raw_message, "")

    def test_empty_final_message_is_diagnosable_none_value(self):
        self.plan_worker.run.return_value = _make_result(final_message=None)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.selection_raw_message, "")

    def test_raw_message_size_limit_and_truncation_marker(self):
        svc = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=lambda p: self.plan_worker,
            worker_factory=lambda p: self.implementation_worker,
            db_path=str(Path(self.tmp.name) / "agent2.db"),
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            max_raw_message_bytes=50,
        )
        self.addCleanup(svc.close)
        long_text = "x" * 5000
        self.plan_worker.run.return_value = _make_result(final_message=long_text)

        experiment = svc.create_experiment(goal="g")
        result = svc.run_experiment(experiment.id)

        self.assertLessEqual(len(result.selection_raw_message.encode("utf-8")), 50 + 64)
        self.assertIn("truncated", result.selection_raw_message)


@unittest.skip("policy-replan phase removed")
class PolicyReplanTestCase(unittest.TestCase):
    """Covers the exactly-one-shot policy replan (distinct from the parser
    reformat retry above): triggered when a syntactically valid,
    schema-conformant plan is rejected by plan_violates_policy()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary: looked at IDEAS.md and repo layout."
        )
        self.plan_worker = MagicMock()

        def plan_factory(scratch_dir: str):
            return self.plan_worker

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()

        def factory(worktree_path: str):
            return self.implementation_worker

        self.service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=plan_factory,
            worker_factory=factory,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
        )
        self.addCleanup(self.service.close)

    def test_exactly_one_replan_after_policy_rejection(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=SECOND_VALID_PLAN_JSON, runtime_ms=7),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 2)
        self.assertIsNotNone(result.policy_replan_task_id)
        self.assertEqual(result.policy_replan_runtime_ms, 7)
        self.assertEqual(result.status, STATUS_SUCCEEDED)

    def test_research_runs_only_once(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=SECOND_VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        self.service.run_experiment(experiment.id)

        self.selection_worker.run.assert_called_once()

    def test_no_replan_when_plan_immediately_acceptable(self):
        self.plan_worker.run.return_value = _make_result(final_message=VALID_PLAN_JSON)
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 1)
        self.assertIsNone(result.policy_replan_task_id)
        self.assertEqual(result.status, STATUS_SUCCEEDED)

    def test_no_policy_replan_on_parser_failure(self):
        # Unparseable output triggers the reformat retry, not the policy
        # replan; if the reformat retry also fails to parse, the
        # experiment is rejected without ever invoking a policy replan.
        self.plan_worker.run.side_effect = [
            _make_result(final_message="not json at all, just prose"),
            _make_result(final_message="still not json, more prose"),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 2)
        self.assertIsNone(result.policy_replan_task_id)
        self.assertEqual(result.status, STATUS_REJECTED)

    def test_second_acceptable_plan_is_passed_through_to_implementation(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=SECOND_VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.plan["title"], "Add a second docstring test")
        self.implementation_worker.run.assert_called_once()

    def test_second_unacceptable_plan_ends_experiment_rejected(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(self.plan_worker.run.call_count, 2)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIsNotNone(result.policy_replan_task_id)
        self.implementation_worker.run.assert_not_called()

    def test_separate_usage_per_phase(self):
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary text.",
            usage={"input_tokens": 111, "output_tokens": 11},
        )
        self.plan_worker.run.side_effect = [
            _make_result(
                final_message=POLICY_VIOLATING_PLAN_JSON,
                usage={"input_tokens": 222, "output_tokens": 22},
            ),
            _make_result(
                final_message=SECOND_VALID_PLAN_JSON,
                usage={"input_tokens": 333, "output_tokens": 33},
            ),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.research_usage, {"input_tokens": 111, "output_tokens": 11})
        self.assertEqual(result.selection_usage, {"input_tokens": 222, "output_tokens": 22})
        self.assertEqual(result.policy_replan_usage, {"input_tokens": 333, "output_tokens": 33})
        # All three must be distinct from one another.
        usages = [result.research_usage, result.selection_usage, result.policy_replan_usage]
        self.assertEqual(len({json.dumps(u, sort_keys=True) for u in usages}), 3)

    def test_policy_replan_prompt_contains_goal_research_plan_and_reason(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=SECOND_VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="my special goal")
        self.service.run_experiment(experiment.id)

        replan_call = self.plan_worker.run.call_args_list[1]
        replan_prompt = replan_call.kwargs.get("task") or replan_call.args[0]
        self.assertIn("my special goal", replan_prompt)
        self.assertIn("Research summary: looked at IDEAS.md and repo layout.", replan_prompt)
        self.assertIn("Create a new Azure VM", replan_prompt)
        self.assertIn("infrastructure/deployment", replan_prompt)
        self.assertEqual(replan_call.kwargs.get("sandbox"), "read-only")
        self.assertIn("output_schema", replan_call.kwargs)

    def test_policy_replan_raw_message_and_diagnostic_log_are_size_limited_and_redacted(self):
        secret_text = "api_key=abcdef0123456789 sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567890123456789"
        long_text = secret_text + ("x" * 5000)
        self.plan_worker.run.side_effect = [
            _make_result(final_message=POLICY_VIOLATING_PLAN_JSON),
            _make_result(final_message=long_text),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertIsNotNone(result.policy_replan_raw_message)
        self.assertNotIn("abcdef0123456789", result.policy_replan_raw_message)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567890123456789", result.policy_replan_raw_message)
        self.assertLessEqual(len(result.policy_replan_raw_message.encode("utf-8")), 5000)

    def test_build_policy_replan_prompt_helper(self):
        prompt = _build_policy_replan_prompt(
            goal="goal-x",
            research_summary="summary-y",
            rejected_plan_json='{"title": "bad"}',
            rejection_reason="reason-z",
        )
        self.assertIn("goal-x", prompt)
        self.assertIn("summary-y", prompt)
        self.assertIn('{"title": "bad"}', prompt)
        self.assertIn("reason-z", prompt)
        self.assertIn("code, test, or", prompt)


class ExtractDiagnosticLogTestCase(unittest.TestCase):
    """Unit tests for the standalone _extract_diagnostic_log() filter."""

    def test_returns_none_for_empty_stdout(self):
        self.assertIsNone(_extract_diagnostic_log("", 1024))
        self.assertIsNone(_extract_diagnostic_log(None, 1024))

    def test_keeps_lifecycle_and_agent_message_events(self):
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "tid-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "hello"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}),
        ])
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertIn("thread.started", log)
        self.assertIn("turn.started", log)
        self.assertIn("agent_message", log)
        self.assertIn("hello", log)
        self.assertIn("turn.completed", log)

    def test_drops_reasoning_items(self):
        stdout = "\n".join([
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "secret chain of thought"}}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "final"}}),
        ])
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertNotIn("reasoning", log)
        self.assertNotIn("secret chain of thought", log)
        self.assertIn("final", log)

    def test_command_execution_kept_metadata_only_not_full_output(self):
        stdout = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "i0",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "this is a huge dump of file contents that must not be stored",
                "exit_code": 0,
                "status": "completed",
            },
        })
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertIn("command_execution", log)
        self.assertIn("ls -la", log)
        self.assertIn("exit_code", log)
        self.assertNotIn("huge dump of file contents", log)

    def test_ignores_malformed_json_lines(self):
        stdout = "\n".join([
            "not json at all",
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "ok"}}),
        ])
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertIn("ok", log)

    def test_secrets_are_redacted(self):
        stdout = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "i0",
                "type": "agent_message",
                "text": "here is my api_key=sk-ABCDEFGHIJ1234567890 for reference",
            },
        })
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertNotIn("sk-ABCDEFGHIJ1234567890", log)
        self.assertIn("REDACTED", log)

    def test_bearer_token_is_redacted(self):
        stdout = json.dumps({
            "type": "item.completed",
            "item": {"id": "i0", "type": "agent_message", "text": "Authorization: Bearer abcdef0123456789"},
        })
        log = _extract_diagnostic_log(stdout, 1024)
        self.assertNotIn("abcdef0123456789", log)

    def test_size_limit_and_truncation_marker_with_original_size(self):
        big_text = "y" * 5000
        stdout = json.dumps({
            "type": "item.completed",
            "item": {"id": "i0", "type": "agent_message", "text": big_text},
        })
        log = _extract_diagnostic_log(stdout, 200)
        self.assertLessEqual(len(log.encode("utf-8")), 200 + 100)
        self.assertIn("truncated", log)
        self.assertIn("original size", log)

    def test_no_diagnostic_events_yields_none(self):
        # Only a reasoning item and nothing else safe to keep.
        stdout = json.dumps({
            "type": "item.completed",
            "item": {"id": "i0", "type": "reasoning", "text": "thinking..."},
        })
        self.assertIsNone(_extract_diagnostic_log(stdout, 1024))


class DiagnosticLogPipelineTestCase(unittest.TestCase):
    """Verifies selection_diagnostic_log / reformat_diagnostic_log are
    stored (separately) by the actual experiment pipeline."""

    UNPARSEABLE_TEXT = "I looked around but I will just describe it in prose."

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary: looked at IDEAS.md and repo layout."
        )
        self.plan_worker = MagicMock()

        def plan_factory(scratch_dir: str):
            return self.plan_worker

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()

        def factory(worktree_path: str):
            return self.implementation_worker

        self.service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=plan_factory,
            worker_factory=factory,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
        )
        self.addCleanup(self.service.close)

    def _stdout_for(self, message: str) -> str:
        return json.dumps({
            "type": "item.completed",
            "item": {"id": "i0", "type": "agent_message", "text": message},
        })

    @unittest.skip("selection phase removed")
    def test_selection_diagnostic_log_stored_on_rejection(self):
        self.plan_worker.run.return_value = _make_result(
            final_message=self.UNPARSEABLE_TEXT,
            stdout=self._stdout_for(self.UNPARSEABLE_TEXT),
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIsNotNone(result.selection_diagnostic_log)
        self.assertIn(self.UNPARSEABLE_TEXT, result.selection_diagnostic_log)

    @unittest.skip("reformat-retry phase removed")
    def test_reformat_diagnostic_log_stored_separately(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message=self.UNPARSEABLE_TEXT, stdout=self._stdout_for(self.UNPARSEABLE_TEXT)),
            _make_result(final_message=VALID_PLAN_JSON, stdout=self._stdout_for(VALID_PLAN_JSON)),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertIn(self.UNPARSEABLE_TEXT, result.selection_diagnostic_log)
        self.assertIn("title", result.reformat_diagnostic_log)
        self.assertNotEqual(result.selection_diagnostic_log, result.reformat_diagnostic_log)

    def test_no_secrets_in_stored_diagnostic_logs(self):
        leaky_message = "plan info AZURE_OPENAI_API_KEY=sk-verysecretvalue1234"
        self.plan_worker.run.return_value = _make_result(
            final_message=leaky_message,
            stdout=self._stdout_for(leaky_message),
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)

        self.assertNotIn("sk-verysecretvalue1234", result.selection_diagnostic_log or "")


class NexusIdentityPromptTestCase(unittest.TestCase):
    def test_all_llm_prompts_identify_nexus(self):
        plan = parse_plan(VALID_PLAN_JSON)
        prompts = [
            _build_direct_implementation_prompt("some goal"),
            _build_implementation_prompt(plan),
        ]
        # Old prompts still exist as helpers but are no longer called in the
        # live pipeline; verify the new direct-implementation prompt and the
        # existing plan-based prompt both carry the Nexus identity header.
        for prompt in prompts:
            with self.subTest(prompt=prompt[:80]):
                self.assertTrue(prompt.startswith("You are operating"))
                self.assertIn("worker for Nexus", prompt)
                self.assertIn('not as "the user"', prompt)
                self.assertIn("not independently Nexus", prompt)

class ResearchPromptRequirementsTestCase(unittest.TestCase):
    """The research prompt must instruct Codex to investigate read-only and
    produce a free-form (non-JSON) summary, without forcing a final plan."""

    def test_research_prompt_is_read_only_and_investigative(self):
        prompt = " ".join(_build_research_prompt("some goal").split())
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("Do not modify any files", prompt)
        self.assertIn("IDEAS.md", prompt)

    def test_research_prompt_forbids_final_json_or_plan(self):
        prompt = " ".join(_build_research_prompt("some goal").split())
        self.assertIn("Do NOT emit a final structured plan or any JSON object", prompt)
        self.assertIn("prose, not JSON", prompt)


@unittest.skip("plan phase removed")
class PlanPromptRequirementsTestCase(unittest.TestCase):
    """The plan prompt must instruct Codex it has no tools, embed the
    research summary, forbid placeholder/intention-only answers, and
    require at least one affected file. The reformat retry prompt must
    remain reformat-only (no new research)."""

    def test_plan_prompt_declares_no_tools(self):
        prompt = " ".join(_build_plan_prompt("some goal", "some research").split())
        self.assertIn("You have NO tools available", prompt)
        self.assertIn("cannot run any command or", prompt)

    def test_plan_prompt_embeds_research_summary(self):
        prompt = _build_plan_prompt("some goal", "UNIQUE_RESEARCH_MARKER_XYZ")
        self.assertIn("UNIQUE_RESEARCH_MARKER_XYZ", prompt)

    def test_plan_prompt_requires_concrete_feature_and_nonempty_affected_files(self):
        prompt = " ".join(_build_plan_prompt("some goal", "research").split())
        self.assertIn("one concrete", prompt)
        self.assertIn("affected_files", prompt)
        self.assertIn("must contain at least one existing or planned repository file", prompt)

    def test_plan_prompt_forbids_progress_or_placeholder_content(self):
        prompt = " ".join(_build_plan_prompt("some goal", "research").split())
        self.assertIn("Do not return progress, placeholder, or intention-only content", prompt)

    def test_reformat_retry_prompt_still_forbids_new_selection(self):
        prompt = _build_reformat_retry_prompt("some previous output")
        self.assertIn("Do not select or modify the feature", prompt)
        self.assertIn("Output JSON only", prompt)
        # The retry prompt must not itself instruct further investigation;
        # it must only ask for reformatting of the already-produced text.
        self.assertNotIn("Investigate the repository", prompt)


class PlaceholderPlanPolicyTestCase(unittest.TestCase):
    """Placeholder/intention-only plans must still be rejected even when
    technically schema-valid (non-empty affected_files, all fields
    present)."""

    def _plan_with_title(self, title: str):
        data = json.loads(VALID_PLAN_JSON)
        data["title"] = title
        return parse_plan(json.dumps(data))

    def test_inspection_started_title_is_rejected(self):
        plan = self._plan_with_title("Repository inspection started")
        self.assertIsNotNone(plan)
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_analysis_only_title_is_rejected(self):
        plan = self._plan_with_title("Read-only analysis only")
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_investigating_title_is_rejected(self):
        plan = self._plan_with_title("Still investigating the repository")
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_empty_affected_files_still_rejected_by_parser(self):
        data = json.loads(VALID_PLAN_JSON)
        data["affected_files"] = []
        self.assertIsNone(parse_plan(json.dumps(data)))

    def test_concrete_feature_title_still_passes(self):
        plan = parse_plan(VALID_PLAN_JSON)
        self.assertIsNone(plan_violates_policy(plan, VALID_PLAN_JSON))

    def test_repository_inspection_title_is_rejected(self):
        # The exact title from the real rejected/failed experiment run.
        plan = self._plan_with_title("Repository inspection")
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_ideas_only_affected_files_is_rejected(self):
        # Investigation/documentation-only plans that touch only IDEAS.md
        # (no code or test file) are not an implementable feature.
        data = json.loads(VALID_PLAN_JSON)
        data["title"] = "Document the next steps"
        data["affected_files"] = ["IDEAS.md"]
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan)
        reason = plan_violates_policy(plan, json.dumps(plan.to_dict()))
        self.assertIsNotNone(reason)
        self.assertIn("code or test file", reason)

    def test_readme_and_ideas_only_affected_files_is_rejected(self):
        data = json.loads(VALID_PLAN_JSON)
        data["title"] = "Update project notes"
        data["affected_files"] = ["IDEAS.md", "README.md"]
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_plan_with_ideas_plus_code_file_is_not_rejected_for_this_rule(self):
        # A legitimate feature may still touch IDEAS.md alongside a real
        # code/test file — only pure documentation/investigation plans are
        # rejected by this rule.
        data = json.loads(VALID_PLAN_JSON)
        data["title"] = "Add retry test and update IDEAS"
        data["affected_files"] = ["IDEAS.md", "tests/test_foo.py"]
        plan = parse_plan(json.dumps(data))
        self.assertIsNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))

    def test_legitimate_documentation_feature_with_doc_file_extension_only_is_rejected(self):
        # Documentation-only changes are not blanket-forbidden as a
        # category, but they must still name at least one concrete
        # code/test/config file to be implementable; a plan naming only a
        # .md file remains rejected.
        data = json.loads(VALID_PLAN_JSON)
        data["title"] = "Expand README usage section"
        data["affected_files"] = ["README.md"]
        plan = parse_plan(json.dumps(data))
        self.assertIsNotNone(plan_violates_policy(plan, json.dumps(plan.to_dict())))


class ExperimentLoggingIntegrationTests(unittest.TestCase):
    """End-to-end checks that the structured JSONL log written for one
    experiment actually tells the complete story of that run: success,
    test failure, an unhandled exception, a timeout, and a plan-reformat
    retry - and that a logging failure never changes the experiment's
    resulting status.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.log_dir = Path(self.tmp.name) / "experiment-logs"

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary: looked at IDEAS.md and repo layout."
        )
        self.plan_worker = MagicMock()
        self.plan_worker.run.return_value = _make_result()

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()

        self.service = ExperimentService(
            selection_worker=self.selection_worker,
            plan_worker_factory=lambda scratch_dir: self.plan_worker,
            worker_factory=lambda worktree_path: self.implementation_worker,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            experiment_log_dir=str(self.log_dir),
            test_python_executable=sys.executable,
        )
        self.addCleanup(self.service.close)

    def _read_log(self, experiment_id: str) -> list[dict]:
        path = self.service.experiment_log_path(experiment_id)
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_successful_run_is_fully_understandable_from_log_alone(self):
        experiment = self.service.create_experiment(goal="Add a small docstring test.")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_SUCCEEDED)

        entries = self._read_log(experiment.id)
        events = [e["event"] for e in entries]
        # The whole pipeline - creation, implementation, worktree creation,
        # and final success - must be traceable from this one file, in order.
        for expected in (
            "experiment_created", "run_experiment_started", "worktree_created",
            "implementation_started", "tests_passed",
        ):
            self.assertIn(expected, events, f"missing {expected!r} in {events!r}")
        self.assertLess(events.index("worktree_created"), events.index("implementation_started"))
        self.assertLess(events.index("implementation_started"), events.index("tests_passed"))
        for entry in entries:
            self.assertEqual(entry["experiment_id"], experiment.id)

    def test_test_failure_is_logged_as_error_with_context(self):
        (self.repo_root / "tests" / "test_placeholder.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_fail(self):\n"
            "        self.assertTrue(False)\n"
        )
        subprocess.run(["git", "-C", str(self.repo_root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo_root), "commit", "-q", "-m", "failing test"], check=True)

        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)

        entries = self._read_log(experiment.id)
        failed = [e for e in entries if e["event"] == "tests_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["level"], "ERROR")
        self.assertEqual(failed[0]["phase"], "verifying")
        self.assertIn("exit_code", failed[0])
        status_change = [e for e in entries if e["event"] == "status_forced" and e.get("to_status") == STATUS_FAILED]
        self.assertTrue(status_change, "expected a status_forced -> failed entry")

    def test_unhandled_exception_is_logged_with_stacktrace(self):
        self.implementation_worker.run.side_effect = RuntimeError("impl boom")
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_FAILED)

        entries = self._read_log(experiment.id)
        exceptions = [e for e in entries if e["event"] == "unhandled_exception"]
        self.assertEqual(len(exceptions), 1)
        self.assertEqual(exceptions[0]["level"], "ERROR")
        self.assertEqual(exceptions[0]["exception_type"], "RuntimeError")
        self.assertIn("impl boom", exceptions[0]["exception_message"])
        self.assertIn("Traceback", exceptions[0]["stacktrace"])

    def test_timeout_is_logged_distinctly_from_a_hard_failure(self):
        self.implementation_worker.run.side_effect = None
        self.implementation_worker.run.return_value = _make_result(
            timed_out=True, success=False, sandbox="workspace-write",
            error="Codex process timed out after 1200s",
        )
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_TIMED_OUT)

        entries = self._read_log(experiment.id)
        status_events = [e for e in entries if e["event"] == "status_forced"]
        self.assertTrue(any(e.get("to_status") == STATUS_TIMED_OUT for e in status_events))

    @unittest.skip("reformat-retry phase removed")
    def test_reformat_retry_is_logged_as_warning(self):
        self.plan_worker.run.side_effect = [
            _make_result(final_message="not valid json at all"),
            _make_result(final_message=VALID_PLAN_JSON),
        ]
        experiment = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_SUCCEEDED)

        entries = self._read_log(experiment.id)
        retries = [e for e in entries if e["event"] == "plan_reformat_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["level"], "WARNING")
        self.assertEqual(retries[0]["phase"], "selection")

    def test_logging_failure_never_changes_experiment_status(self):
        """A broken logger (disk full, permission error, ...) must never
        affect the actual pipeline outcome - only best-effort logging is
        skipped."""
        experiment = self.service.create_experiment(goal="g")

        with patch(
            "modules.experiment_logger.ExperimentLogger._append",
            side_effect=OSError("simulated disk failure"),
        ):
            result = self.service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        # The experiment record itself (in the real DB, not the log) must
        # still reflect the correct successful outcome despite every log
        # write having failed.
        reloaded = self.service.get_experiment(experiment.id)
        self.assertEqual(reloaded.status, STATUS_SUCCEEDED)


# ---------------------------------------------------------------------------
# Regression tests for the single-LLM-call pipeline
# ---------------------------------------------------------------------------


class SingleCallPipelineRegressionTests(unittest.TestCase):
    """Focused regression tests for the single-call Codex pipeline.

    These tests verify that:
    - only one Codex call is made per experiment (implementation only),
    - the safety check runs on the raw goal before any LLM call,
    - the quality gate still works without an LLM-generated plan,
    - DB fields for removed phases remain null (backward-compatible),
    - usage summary contains only the implementation call.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

        self.implementation_worker = MagicMock()
        self.implementation_worker.run.side_effect = _write_affected_files_side_effect()

        self.service = ExperimentService(
            worker_factory=lambda wt: self.implementation_worker,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
        )
        self.addCleanup(self.service.close)

    # ------------------------------------------------------------------
    # Single LLM call
    # ------------------------------------------------------------------

    def test_only_implementation_worker_is_called(self):
        experiment = self.service.create_experiment(goal="Add a small helper.")
        self.service.run_experiment(experiment.id)
        # The implementation worker must be called exactly once and no other
        # LLM call should be made (no selection_worker, no plan_worker).
        self.implementation_worker.run.assert_called_once()

    def test_implementation_prompt_contains_goal(self):
        goal = "Add a UNIQUE_GOAL_MARKER helper function."
        experiment = self.service.create_experiment(goal=goal)
        self.service.run_experiment(experiment.id)
        _, kwargs = self.implementation_worker.run.call_args
        self.assertIn("UNIQUE_GOAL_MARKER", kwargs.get("task", ""))

    def test_implementation_uses_workspace_write_sandbox(self):
        experiment = self.service.create_experiment(goal="Some task.")
        self.service.run_experiment(experiment.id)
        _, kwargs = self.implementation_worker.run.call_args
        self.assertEqual(kwargs["sandbox"], "workspace-write")

    def test_implementation_worker_scoped_to_worktree(self):
        experiment = self.service.create_experiment(goal="Some task.")
        result = self.service.run_experiment(experiment.id)
        _, kwargs = self.implementation_worker.run.call_args
        self.assertEqual(kwargs["working_dir"], result.worktree_path)

    # ------------------------------------------------------------------
    # Goal security check runs before any LLM call
    # ------------------------------------------------------------------

    def test_goal_with_commit_keyword_rejected_without_llm_call(self):
        experiment = self.service.create_experiment(
            goal="Implement and then run git commit and push."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_with_pip_install_rejected_without_llm_call(self):
        experiment = self.service.create_experiment(
            goal="Run pip install requests then use it."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_with_infra_action_rejected_without_llm_call(self):
        experiment = self.service.create_experiment(
            goal="Create a new Azure VM for the worker pool."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.implementation_worker.run.assert_not_called()

    def test_goal_policy_rule_classified_correctly(self):
        experiment = self.service.create_experiment(
            goal="Implement and then git commit and push the result."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.policy_rule, "disallowed_keyword")

    def test_benign_goal_passes_safety_check(self):
        experiment = self.service.create_experiment(
            goal="Add unit tests for the redaction module."
        )
        result = self.service.run_experiment(experiment.id)
        self.assertNotEqual(result.status, STATUS_REJECTED)

    # ------------------------------------------------------------------
    # DB backward compatibility: removed-phase fields are null
    # ------------------------------------------------------------------

    def test_removed_phase_db_fields_are_null(self):
        experiment = self.service.create_experiment(goal="Some task.")
        result = self.service.run_experiment(experiment.id)
        # Fields for removed phases must remain null for new experiments.
        self.assertIsNone(result.selection_usage)
        self.assertIsNone(result.selection_task_id)
        self.assertIsNone(result.reformat_retry_usage)
        self.assertIsNone(result.reformat_retry_task_id)
        self.assertIsNone(result.policy_replan_usage)
        self.assertIsNone(result.policy_replan_task_id)
        self.assertIsNone(result.research_usage)
        self.assertIsNone(result.research_task_id)
        self.assertIsNone(result.plan)

    # ------------------------------------------------------------------
    # Usage summary contains only the implementation call
    # ------------------------------------------------------------------

    def test_usage_summary_contains_only_implementation_call(self):
        experiment = self.service.create_experiment(goal="Some task.")
        result = self.service.run_experiment(experiment.id)
        # Implementation usage must be present.
        self.assertIsNotNone(result.implementation_usage)
        self.assertIsNotNone(result.implementation_runtime_ms)
        # All selection/research/retry usage fields must be absent.
        self.assertIsNone(result.selection_usage)
        self.assertIsNone(result.research_usage)
        self.assertIsNone(result.reformat_retry_usage)
        self.assertIsNone(result.policy_replan_usage)

    # ------------------------------------------------------------------
    # Worktree isolation and sandbox enforcement still apply
    # ------------------------------------------------------------------

    def test_main_repo_unchanged_after_run(self):
        experiment = self.service.create_experiment(goal="Some task.")
        self.service.run_experiment(experiment.id)
        status = subprocess.run(
            ["git", "-C", str(self.repo_root), "status", "--short"],
            capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "")

    def test_worktree_created_on_isolated_branch(self):
        experiment = self.service.create_experiment(goal="Some task.")
        result = self.service.run_experiment(experiment.id)
        self.assertIsNotNone(result.worktree_path)
        self.assertTrue(Path(result.worktree_path).is_dir())
        self.assertTrue(result.branch_name.startswith("experiment/"))

    # ------------------------------------------------------------------
    # Quality gate (plan=None) still catches tests-only diffs
    # ------------------------------------------------------------------

    def test_quality_gate_requires_non_test_file_change(self):
        # An implementation that only touches test files must be flagged.
        def tests_only_impl(*args, **kwargs):
            working_dir = kwargs.get("working_dir")
            if working_dir:
                target = Path(working_dir) / "tests" / "test_new.py"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("def test_pass(): assert True\n")
            return _make_result(final_message="done", sandbox="workspace-write")

        self.implementation_worker.run.side_effect = tests_only_impl
        experiment = self.service.create_experiment(goal="Add tests only.")
        result = self.service.run_experiment(experiment.id)
        # With no non-test changes and a goal that doesn't explicitly request
        # tests-only, the quality gate should flag this for review.
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)

    # ------------------------------------------------------------------
    # _build_direct_implementation_prompt helper
    # ------------------------------------------------------------------

    def test_direct_implementation_prompt_contains_goal(self):
        prompt = _build_direct_implementation_prompt("UNIQUE_GOAL_TEXT")
        self.assertIn("UNIQUE_GOAL_TEXT", prompt)

    def test_direct_implementation_prompt_carries_nexus_identity(self):
        prompt = _build_direct_implementation_prompt("some goal")
        self.assertTrue(prompt.startswith("You are operating"))
        self.assertIn("worker for Nexus", prompt)

    def test_direct_implementation_prompt_forbids_commit_push_merge(self):
        prompt = _build_direct_implementation_prompt("some goal")
        self.assertIn("commit", prompt.lower())
        self.assertIn("push", prompt.lower())
        # The no-commit instruction must be present.
        self.assertIn("Do NOT run", prompt)

    # ------------------------------------------------------------------
    # _check_goal_policy helper
    # ------------------------------------------------------------------

    def test_check_goal_policy_blocks_commit(self):
        self.assertIsNotNone(_check_goal_policy("implement and commit the change"))

    def test_check_goal_policy_blocks_infra_action(self):
        self.assertIsNotNone(_check_goal_policy("create a new Azure VM"))

    def test_check_goal_policy_blocks_secret_action(self):
        self.assertIsNotNone(_check_goal_policy("read the api key from environment"))

    def test_check_goal_policy_allows_benign_goal(self):
        self.assertIsNone(_check_goal_policy("add unit tests for the cost monitor"))

    def test_check_goal_policy_allows_negated_mention(self):
        # "no secrets" / "without logging tokens" must not block.
        self.assertIsNone(_check_goal_policy("Ensure no secrets are logged in the output."))


if __name__ == "__main__":
    unittest.main()
