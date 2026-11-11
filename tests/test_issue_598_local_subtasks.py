"""#598: the local coding worker splits a task into small subtasks and runs them in order."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from modules.local_coding_worker import (
    _PLAN_SYSTEM_PROMPT,
    MAX_PATCH_ROUNDS,
    LocalCodingWorker,
    PlanUnusable,
    free_tier_plan,
    parse_plan,
)
from modules import local_coding_worker
from modules.model_registry import ModelSpec

# Subtask 2 only applies on top of subtask 1: the order is checked by git apply.
_STEP1 = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+step1\n"
_STEP2 = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-step1\n+step2\n"
_MOD2 = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
_MOD3 = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 3\n"
_BROKEN_PY = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = (\n"
_WEAKEN_TEST = ("--- a/test_mod.py\n+++ b/test_mod.py\n@@ -4,2 +4,2 @@\n def test_value():\n"
                "-    assert mod.VALUE == 2\n+    assert True\n")
_TEST_EXPECTS_3 = ("--- a/test_mod.py\n+++ b/test_mod.py\n@@ -4,2 +4,2 @@\n def test_value():\n"
                   "-    assert mod.VALUE == 2\n+    assert mod.VALUE == 3\n")
_ONE_SHOT ="--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+one-shot\n"


def _plan(*subtasks: dict) -> str:
    return json.dumps({"subtasks": list(subtasks)})


_TWO_STEPS = _plan(
    {"goal": "Change original to step1 in value.txt", "files": ["value.txt"]},
    {"goal": "Change step1 to step2 in value.txt", "files": ["value.txt"]},
)


class _Server:
    """Fake llama-server: planning answers and patch answers are scripted separately."""

    def __init__(self, plan: str, patches: list[str], *, n_ctx: int = 8192):
        self.plan = plan
        self.patches = list(patches)
        self.n_ctx = n_ctx
        self.plan_calls: list[str] = []
        self.patch_calls: list[str] = []

    async def context_size(self) -> int:
        return self.n_ctx

    async def count_tokens(self, text: str) -> int:
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", **_kw) -> str:
        if system == _PLAN_SYSTEM_PROMPT:
            self.plan_calls.append(prompt)
            return self.plan
        self.patch_calls.append(prompt)
        return self.patches[min(len(self.patch_calls), len(self.patches)) - 1]


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def last(self, name: str) -> dict:
        return [fields for event, fields in self.events if event == name][-1]

    def all(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]


class _Transport:
    """Records every registry HTTP call; answers with *content* or *status*."""

    def __init__(self, content: str = "", status: int = 200):
        self.content = content
        self.status = status
        self.urls: list[str] = []

    def __call__(self, url, _headers, _body):
        self.urls.append(url)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": self.content}}]})


_FREE = ModelSpec(id="free-planner", base_url="https://free.example/v1", model="f", cost="free",
                  roles=("planning",))
_PAID = ModelSpec(id="paid-planner", base_url="https://paid.example/v1", model="p", cost="paid",
                  roles=("planning",))


class Issue598SubtaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
        (self.repo / "value.txt").write_text("original\n")
        (self.repo / "mod.py").write_text("VALUE = 1\n")
        (self.repo / "test_mod.py").write_text(
            "import mod\n\n\ndef test_value():\n    assert mod.VALUE == 2\n"
        )
        self._git("add", ".")
        self._git("commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def _status(self) -> str:
        return self._git("status", "--porcelain", "--untracked-files=all")

    def _run(self, server: _Server, *, planner=None, task: str = "Edit value.txt and mod.py"):
        log = _Logger()
        planner = planner or mock.Mock(side_effect=AssertionError("free-tier planner must not be called"))
        worker = LocalCodingWorker(client=server, max_tokens=1024, split_tasks=True, planner=planner)
        result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                            experiment_logger=log)
        return worker, result, log

    def test_two_subtasks_are_applied_in_order_and_each_sees_the_earlier_diff(self):
        server = _Server(_TWO_STEPS, [_STEP1, _STEP2])
        worker, result, log = self._run(server)

        self.assertTrue(result.success, result.error)
        self.assertEqual("step2\n", (self.repo / "value.txt").read_text())
        self.assertEqual(1, len(server.plan_calls))
        self.assertEqual(2, len(server.patch_calls))
        first, second = server.patch_calls
        self.assertIn("Subtask 1 of 2", first)
        self.assertIn("Change original to step1", first)
        self.assertIn("Subtask 2 of 2", second)
        self.assertIn("-original\n+step1", second)  # the diff of subtask 1
        self.assertIn("--- value.txt ---\nstep1", second)  # and the file as it is now
        split = log.last("local_task_split")
        self.assertEqual((True, "local", 2), (split["split"], split["planner"], split["subtasks"]))
        done = log.last("local_task_split_result")
        self.assertEqual((True, 2, 2, None, True),
                         (done["split"], done["subtasks"], done["subtasks_succeeded"],
                          done["failed_subtask"], done["success"]))
        self.assertEqual([1, 2], [r["subtask"] for r in log.all("local_patch_round")])
        self.assertEqual(2, worker.last_split.succeeded)

    def test_failing_subtask_2_is_reported_and_subtask_1_is_no_success(self):
        server = _Server(_TWO_STEPS, [_STEP1, "no diff here", _STEP1, "still nothing"])
        _worker, result, log = self._run(server)

        self.assertFalse(result.success)
        self.assertIn("subtask 2/2 failed", result.error)
        self.assertIn("Change step1 to step2", result.error)
        self.assertIn(f"after {MAX_PATCH_ROUNDS} rounds", result.error)
        self.assertIn("stay in the working tree", result.error)
        self.assertEqual(1 + MAX_PATCH_ROUNDS, len(server.patch_calls))  # stopped after subtask 2
        # Subtask 1's change stays for the checkpoint, subtask 2's rounds were rolled back.
        self.assertEqual("step1\n", (self.repo / "value.txt").read_text())
        done = log.last("local_task_split_result")
        self.assertEqual((2, 1, 2, False),
                         (done["subtasks"], done["subtasks_succeeded"], done["failed_subtask"], done["success"]))

    def test_failing_subtask_1_stops_before_subtask_2_and_leaves_tree_clean(self):
        plan = _plan({"goal": "Edit mod", "files": ["mod.py"]}, {"goal": "Edit value", "files": ["value.txt"]})
        server = _Server(plan, [_BROKEN_PY])
        _worker, result, log = self._run(server)

        self.assertFalse(result.success)
        self.assertIn("subtask 1/2 failed", result.error)
        self.assertEqual(MAX_PATCH_ROUNDS, len(server.patch_calls))
        self.assertEqual("", self._status())
        self.assertEqual(1, log.last("local_task_split_result")["failed_subtask"])

    def test_unusable_local_split_falls_back_to_one_free_tier_call_never_paid(self):
        free_plan = _plan({"goal": "first", "files": ["value.txt"]}, {"goal": "second", "files": ["value.txt"]})
        transport = _Transport(free_plan)
        planner = functools.partial(free_tier_plan, models=[_PAID, _FREE], transport=transport)
        server = _Server("Sure! First edit the file, then the other one.", [_STEP1, _STEP2])
        _worker, result, log = self._run(server, planner=planner)

        self.assertTrue(result.success, result.error)
        self.assertEqual(["https://free.example/v1/chat/completions"], transport.urls)
        split = log.last("local_task_split")
        self.assertEqual(("free_tier", "free-planner", 1),
                         (split["planner"], split["planner_model"], split["free_tier_calls"]))
        self.assertIn("local plan unusable", split["reason"])
        self.assertEqual("step2\n", (self.repo / "value.txt").read_text())

    def test_failed_free_tier_call_is_not_retried_and_the_task_runs_one_shot(self):
        second_free = ModelSpec(id="free-2", base_url="https://free2.example/v1", model="g", cost="free")
        transport = _Transport(status=429)
        planner = functools.partial(free_tier_plan, models=[_FREE, second_free, _PAID], transport=transport)
        server = _Server("{not json", [_ONE_SHOT])
        _worker, result, log = self._run(server, planner=planner)

        self.assertTrue(result.success, result.error)
        self.assertEqual(["https://free.example/v1/chat/completions"], transport.urls)  # one call only
        self.assertEqual("one-shot\n", (self.repo / "value.txt").read_text())
        split = log.last("local_task_split")
        self.assertFalse(split["split"])
        self.assertIn("rate_limited", split["reason"])
        done = log.last("local_task_split_result")
        self.assertEqual((False, 0, True), (done["split"], done["subtasks"], done["success"]))

    def test_only_paid_models_configured_means_no_call_at_all(self):
        transport = _Transport(_TWO_STEPS)
        self.assertIsNone(free_tier_plan("plan", models=[_PAID], transport=transport))
        self.assertEqual([], transport.urls)

    def test_unreachable_local_model_does_not_spend_a_free_tier_call(self):
        class _Down(_Server):
            async def complete(self, prompt, *, system="", **kw):
                raise httpx.ConnectError("llama-server down")

        planner = mock.Mock()
        _worker, result, log = self._run(_Down(_TWO_STEPS, []), planner=planner)
        self.assertFalse(result.success)
        planner.assert_not_called()
        self.assertIn("local planner call failed", log.last("local_task_split")["reason"])

    def test_single_local_subtask_falls_back_to_one_free_tier_call(self):
        # Review of 8d81f164: one subtask is no split, so it is unusable too.
        free_plan = _plan({"goal": "first", "files": ["value.txt"]}, {"goal": "second", "files": ["value.txt"]})
        transport = _Transport(free_plan)
        planner = functools.partial(free_tier_plan, models=[_PAID, _FREE], transport=transport)
        server = _Server(_plan({"goal": "all of it", "files": ["value.txt"]}), [_STEP1, _STEP2])
        _worker, result, log = self._run(server, planner=planner)
        self.assertTrue(result.success, result.error)
        self.assertEqual(["https://free.example/v1/chat/completions"], transport.urls)
        split = log.last("local_task_split")
        self.assertEqual(("free_tier", 1), (split["planner"], split["free_tier_calls"]))
        self.assertIn("single subtask", split["reason"])

    def test_single_subtask_from_both_planners_runs_one_shot(self):
        single = _plan({"goal": "all of it", "files": ["value.txt"]})
        transport = _Transport(single)
        planner = functools.partial(free_tier_plan, models=[_FREE], transport=transport)
        server = _Server(single, [_ONE_SHOT])
        _worker, result, log = self._run(server, planner=planner)
        self.assertTrue(result.success, result.error)
        self.assertEqual(1, len(transport.urls))
        self.assertFalse(log.last("local_task_split")["split"])

    def test_named_tests_must_pass_and_a_failing_round_is_rolled_back(self):
        plan = _plan(
            {"goal": "Set VALUE to 2", "files": ["mod.py"], "tests": ["test_mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD3, _MOD2, _STEP1])
        _worker, result, log = self._run(server)

        self.assertTrue(result.success, result.error)
        rounds = log.all("local_patch_round")
        self.assertEqual(["tests_failed", "applied", "applied"], [r["outcome"] for r in rounds])
        self.assertIn("assert", rounds[0]["reason"])
        self.assertIn("named tests fail", server.patch_calls[1])  # fed back to the model
        self.assertEqual("VALUE = 2\n", (self.repo / "mod.py").read_text())
        self.assertNotIn("__pycache__", self._git("status", "--porcelain", "--ignored"))

    def test_patch_outside_the_subtask_files_is_rejected(self):
        plan = _plan({"goal": "value only", "files": ["value.txt"]}, {"goal": "mod", "files": ["mod.py"]})
        server = _Server(plan, [_MOD2, _STEP1, _MOD2])
        _worker, result, log = self._run(server)
        self.assertTrue(result.success, result.error)
        self.assertEqual(["outside_subtask", "applied", "applied"],
                         [r["outcome"] for r in log.all("local_patch_round")])

    def test_named_test_not_declared_in_files_cannot_be_edited(self):
        # codex finding on 1830c2db: a model must not weaken its own validation tests.
        plan = _plan(
            {"goal": "Set VALUE to 3", "files": ["mod.py"], "tests": ["test_mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD3 + _WEAKEN_TEST, _WEAKEN_TEST, _MOD3])
        _worker, result, log = self._run(server)

        self.assertFalse(result.success)
        self.assertIn("subtask 1/2 failed", result.error)
        rounds = log.all("local_patch_round")
        self.assertEqual(["outside_subtask", "outside_subtask", "tests_failed"],
                         [r["outcome"] for r in rounds])
        self.assertIn("test_mod.py", rounds[0]["reason"])
        self.assertIn("assert mod.VALUE == 2", (self.repo / "test_mod.py").read_text())
        self.assertEqual("", self._status())
        self.assertEqual(1, log.last("local_task_split_result")["failed_subtask"])

    def test_named_test_declared_in_files_may_be_edited(self):
        plan = _plan(
            {"goal": "Set VALUE to 3 and update its test", "files": ["mod.py", "test_mod.py"],
             "tests": ["test_mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD3 + _TEST_EXPECTS_3, _STEP1])
        _worker, result, _log = self._run(server)

        self.assertTrue(result.success, result.error)
        self.assertIn("assert mod.VALUE == 3", (self.repo / "test_mod.py").read_text())

    def test_subtask_prompt_says_named_tests_are_run_only(self):
        plan = _plan(
            {"goal": "Set VALUE to 2", "files": ["mod.py"], "tests": ["test_mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD2, _STEP1])
        _worker, result, _log = self._run(server)
        self.assertTrue(result.success, result.error)
        self.assertIn("Files to change (only these): mod.py\n", server.patch_calls[0])
        self.assertIn("run only; do not edit them", server.patch_calls[0])
        self.assertIn("Tests are only run, never edited", server.plan_calls[0])

    def test_split_off_runs_one_shot_and_still_logs_the_result(self):
        server = _Server(_TWO_STEPS, [_ONE_SHOT])
        log = _Logger()
        result = LocalCodingWorker(client=server, max_tokens=1024).run(
            task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write",
            experiment_logger=log,
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual([], server.plan_calls)
        done = log.last("local_task_split_result")
        self.assertEqual((False, True), (done["split"], done["success"]))

    def test_parse_plan_rejects_unusable_plans(self):
        root = str(self.repo)
        bad = [
            "no json",
            _plan(),
            json.dumps({"subtasks": [{"goal": "x", "files": ["a.py", "b.py", "c.py"]}]}),
            json.dumps({"subtasks": [{"goal": "", "files": ["a.py"]}]}),
            json.dumps({"subtasks": [{"goal": "x", "files": ["../outside.py"]}]}),
            json.dumps({"subtasks": [{"goal": "x", "files": ["/etc/passwd"]}]}),
            json.dumps({"subtasks": [{"goal": "x", "files": ["a.py"], "tests": ["notes.txt"]}]}),
            json.dumps({"subtasks": [{"goal": "x", "files": ["a.py"]}] * 7}),
        ]
        for text in bad:
            with self.subTest(text=text[:60]):
                with self.assertRaises(PlanUnusable):
                    parse_plan(text, root)
        fenced = "```json\n" + _plan({"goal": "x", "files": "./a.py", "tests": ["t/test_a.py::T::t"]}) + "\n```"
        (sub,) = parse_plan(fenced, root)
        self.assertEqual((("a.py",), ("t/test_a.py::T::t",)), (sub.files, sub.tests))

    def test_production_factory_turns_splitting_on_unless_disabled(self):
        from modules.coding_provider import build_worker

        with mock.patch.dict(os.environ, {"LOCAL_TASK_SPLIT": ""}, clear=False):
            os.environ.pop("LOCAL_TASK_SPLIT")
            self.assertTrue(build_worker("local").split_tasks)
        with mock.patch.dict(os.environ, {"LOCAL_TASK_SPLIT": "0"}):
            self.assertFalse(build_worker("local").split_tasks)
        with mock.patch.dict(os.environ, {"LOCAL_TEST_FIRST": "1"}):
            self.assertTrue(build_worker("local").test_first)
        with mock.patch.dict(os.environ, {"LOCAL_TEST_FIRST": "0"}):
            self.assertFalse(build_worker("local").test_first)

    def test_generated_tests_are_added_to_every_subtask_and_gate_the_attempt(self):
        generated = ["tests.test_generated.ValueTests.test_value"]
        content = ("import unittest\nimport mod\n\nclass ValueTests(unittest.TestCase):\n"
                   "    def test_value(self):\n        self.assertEqual(mod.VALUE, 2)\n")
        plan = _plan(
            {"goal": "Set VALUE to 2", "files": ["mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD2, _STEP1])
        seen: list[tuple[str, ...]] = []
        real_run = local_coding_worker.run_named_tests

        def recording_run(working_dir, tests, timeout):
            seen.append(tuple(tests))
            return real_run(working_dir, tests, timeout)

        with mock.patch.object(local_coding_worker, "generate_failing_tests",
                               return_value=(generated, content)), \
                mock.patch.object(local_coding_worker, "run_named_tests", side_effect=recording_run):
            worker = LocalCodingWorker(client=server, max_tokens=1024, split_tasks=True,
                                       test_first=True,
                                       planner=mock.Mock(side_effect=AssertionError("no fallback")))
            log = _Logger()
            result = worker.run(task="## Change\n`mod.py`\n## Test\nVALUE is 2",
                                working_dir=str(self.repo), sandbox="workspace-write",
                                experiment_logger=log)

        self.assertTrue(result.success, result.error)
        self.assertEqual([tuple(generated), tuple(generated)], seen)
        self.assertTrue(worker.local_attempt_passed_generated_tests)
        event = log.last("local_test_first_generated")
        self.assertEqual((1, 1, False),
                         (event["tests_generated"], event["tests_failing_on_base"],
                          event["paid_fallback_used"]))

    def test_generated_test_failure_exhausts_local_rounds(self):
        generated = ["tests.test_generated.ValueTests.test_value"]
        content = ("import unittest\nimport mod\n\nclass ValueTests(unittest.TestCase):\n"
                   "    def test_value(self):\n        self.assertEqual(mod.VALUE, 2)\n")
        plan = _plan(
            {"goal": "Set VALUE to 2", "files": ["mod.py"]},
            {"goal": "Change original to step1", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD3])
        with mock.patch.object(local_coding_worker, "generate_failing_tests",
                               return_value=(generated, content)):
            worker = LocalCodingWorker(client=server, max_tokens=1024, split_tasks=True,
                                       test_first=True)
            result = worker.run(task="## Change\n`mod.py`\n## Test\nVALUE is 2",
                                working_dir=str(self.repo), sandbox="workspace-write")
        self.assertFalse(result.success)
        self.assertFalse(worker.local_attempt_passed_generated_tests)

    def test_later_unrelated_failure_rechecks_generated_tests_on_current_tree(self):
        generated = ["tests.test_generated.ValueTests.test_value"]
        content = ("import unittest\nimport mod\n\nclass ValueTests(unittest.TestCase):\n"
                   "    def test_value(self):\n        self.assertEqual(mod.VALUE, 2)\n")
        plan = _plan(
            {"goal": "Set VALUE to 2", "files": ["mod.py"]},
            {"goal": "Make an unrelated change", "files": ["value.txt"]},
        )
        server = _Server(plan, [_MOD2, "no diff"])
        seen: list[tuple[str, ...]] = []
        real_run = local_coding_worker.run_named_tests

        def recording_run(working_dir, tests, timeout):
            seen.append(tuple(tests))
            return real_run(working_dir, tests, timeout)

        with mock.patch.object(local_coding_worker, "generate_failing_tests",
                               return_value=(generated, content)), \
                mock.patch.object(local_coding_worker, "run_named_tests", side_effect=recording_run):
            worker = LocalCodingWorker(client=server, max_tokens=1024, split_tasks=True,
                                       test_first=True)
            result = worker.run(task="## Change\n`mod.py`\n## Test\nVALUE is 2",
                                working_dir=str(self.repo), sandbox="workspace-write")

        self.assertFalse(result.success)
        self.assertEqual("VALUE = 2\n", (self.repo / "mod.py").read_text())
        self.assertTrue(worker.local_attempt_passed_generated_tests)
        self.assertEqual([tuple(generated), tuple(generated)], seen)

    def test_exhaustion_runner_crash_does_not_reuse_stale_pass_state(self):
        worker = LocalCodingWorker(client=_Server(_TWO_STEPS, ["no diff"]), test_first=True)
        worker.generated_tests = ("tests.test_generated.T.test_value",)
        worker.local_attempt_passed_generated_tests = True
        plan = mock.Mock(prompt="prompt", prompt_tokens=1)

        with mock.patch.object(local_coding_worker, "MAX_PATCH_ROUNDS", 1), \
                mock.patch.object(local_coding_worker, "run_named_tests",
                                  side_effect=RuntimeError("runner crashed")):
            with self.assertRaisesRegex(RuntimeError, "runner crashed"):
                worker._patch_rounds(plan, str(self.repo), 10, None, "implementation",
                                     tests=worker.generated_tests)

        self.assertIsNone(worker.local_attempt_passed_generated_tests)

    def test_exhaustion_runner_interrupt_propagates_without_stale_pass_state(self):
        worker = LocalCodingWorker(client=_Server(_TWO_STEPS, ["no diff"]), test_first=True)
        worker.generated_tests = ("tests.test_generated.T.test_value",)
        worker.local_attempt_passed_generated_tests = True
        plan = mock.Mock(prompt="prompt", prompt_tokens=1)

        with mock.patch.object(local_coding_worker, "MAX_PATCH_ROUNDS", 1), \
                mock.patch.object(local_coding_worker, "run_named_tests",
                                  side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker._patch_rounds(plan, str(self.repo), 10, None, "implementation",
                                     tests=worker.generated_tests)

        self.assertIsNone(worker.local_attempt_passed_generated_tests)

    def test_reusing_worker_resets_generated_test_acceptance_state(self):
        generated = ["tests.test_generated.ValueTests.test_value"]
        content = ("import unittest\nimport mod\n\nclass ValueTests(unittest.TestCase):\n"
                   "    def test_value(self):\n        self.assertEqual(mod.VALUE, 2)\n")
        worker = LocalCodingWorker(client=_Server(_TWO_STEPS, [_MOD2, _MOD2]), test_first=True)

        with mock.patch.object(local_coding_worker, "generate_failing_tests",
                               side_effect=[(generated, content), ([], "")]):
            first = worker.run(task="## Change\n`mod.py`\n## Test\nVALUE is 2",
                               working_dir=str(self.repo), sandbox="workspace-write")
            self.assertTrue(first.success)
            self.assertTrue(worker.local_attempt_passed_generated_tests)
            self._git("checkout", "--", "mod.py")
            second = worker.run(task="Edit mod.py", working_dir=str(self.repo),
                                sandbox="workspace-write")

        self.assertTrue(second.success)
        self.assertEqual((), worker.generated_tests)
        self.assertIsNone(worker.local_attempt_passed_generated_tests)

    def test_test_first_off_or_empty_generation_preserves_current_behavior(self):
        with mock.patch.object(local_coding_worker, "generate_failing_tests") as generate:
            result = LocalCodingWorker(client=_Server(_TWO_STEPS, [_ONE_SHOT])).run(
                task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write")
        self.assertTrue(result.success, result.error)
        generate.assert_not_called()

        self._git("checkout", "--", "value.txt")
        with mock.patch.object(local_coding_worker, "generate_failing_tests", return_value=([], "")):
            worker = LocalCodingWorker(client=_Server(_TWO_STEPS, [_ONE_SHOT]), test_first=True)
            result = worker.run(task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write")
        self.assertTrue(result.success, result.error)
        self.assertEqual((), worker.generated_tests)
        self.assertIsNone(worker.local_attempt_passed_generated_tests)


if __name__ == "__main__":
    unittest.main()
