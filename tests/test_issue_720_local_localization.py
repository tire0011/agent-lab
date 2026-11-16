"""#720: NEXUS_LOCAL_LOCALIZE in the local coding worker (localization part 2)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from modules.coding_worker import WorkerResult
from modules.experiment_service import ExperimentService
from modules.experiment_service.prompts import _build_direct_implementation_prompt
from modules.local_coding_worker import (
    _HANDOVER_END_MARKERS,
    _MAX_FILES,
    CONTEXT_TOO_SMALL,
    LocalCodingWorker,
    build_prompt,
)
from modules.local_file_localizer import (
    LIKELY_FILES_HEADING,
    hint_line,
    parse_likely_files,
    unaffordable_reason,
    with_hint,
)

_PATCH = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n"
_FAST = (5000.0, 500.0)  # rates that easily afford the 45 s localization call
_SLOW = (7.0, 2.6)  # the fallback rates: no room for any repository map
_NAMED = [f"mod_{n}.py" for n in range(6)]


class _FakeLlama:
    """llama-server stand-in: /props, /tokenize, the localization and patch calls."""

    def __init__(self, *, paths: list[str] | None = None, delay: float = 0.0):
        self.paths = paths or []
        self.delay = delay
        self.localize_calls: list[dict] = []
        self.patch_calls: list[dict] = []

    async def context_size(self) -> int:
        return 32768

    async def count_tokens(self, text: str) -> int:
        return self.tokens(text)

    @staticmethod
    def tokens(text: str) -> int:
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 0, **kw) -> str:
        if kw.get("response_format"):
            self.localize_calls.append({"prompt": prompt, "max_tokens": max_tokens})
            if self.delay:
                time.sleep(self.delay)
            return '{"paths": [' + ", ".join(f'"{p}"' for p in self.paths) + "]}"
        self.patch_calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        return _PATCH


class _Log:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def info(self, phase, event, message, **fields):
        self.events.append((event, message, fields))

    warning = info

    def named(self, event: str) -> list[dict]:
        return [fields for name, _message, fields in self.events if name == event]


def _result(success: bool, task: str, working_dir: str, sandbox: str, provider: str,
            error: str | None = None) -> WorkerResult:
    return WorkerResult(success=success, exit_code=0 if success else 1, stdout="", stderr="",
                        runtime_ms=1, timed_out=False, stdout_truncated=False,
                        stderr_truncated=False, thread_id=None, final_message=None, usage=None,
                        task=task, working_dir=working_dir, sandbox=sandbox, error=error,
                        provider=provider)


def _task(goal: str) -> str:
    return _build_direct_implementation_prompt(goal)


_GOAL = "Change value.txt to say patched; touches " + ", ".join(_NAMED) + "."


class Issue720LocalLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("original\n")
        for name in _NAMED:
            (self.repo / name).write_text("".join(f"line_{i} = {i}\n" for i in range(60)))
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("NEXUS_LOCAL_LOCALIZE", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _worker(self, client: _FakeLlama, rates=_FAST) -> LocalCodingWorker:
        worker = LocalCodingWorker(client=client, rates_path=self.root / "rates.json")
        worker.rates.medians = lambda: rates  # type: ignore[method-assign]
        return worker

    def _run(self, worker: LocalCodingWorker, task: str, log: _Log, timeout: float = 300):
        return worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                          timeout_seconds=timeout, experiment_logger=log)

    def test_flag_off_makes_no_call_and_leaves_the_prompt_unchanged(self):
        client, log = _FakeLlama(paths=["mod_1.py"]), _Log()
        task = _task(_GOAL)
        expected = asyncio.run(build_prompt(task, str(self.repo), _FakeLlama(),
                                            max_output_tokens=4096, rates=_FAST)).prompt
        result = self._run(self._worker(client), task, log)
        self.assertTrue(result.success, result.error)
        self.assertEqual([], client.localize_calls)
        self.assertEqual([], log.named("local_localization"))
        self.assertEqual(expected, client.patch_calls[0]["prompt"])
        self.assertNotIn(LIKELY_FILES_HEADING, client.patch_calls[0]["prompt"])

    def test_hint_narrows_round_one_files_on_six_named_files(self):
        task = _task(_GOAL)
        hinted = with_hint(task, hint_line(["mod_4.py", "mod_1.py"]))
        plain = asyncio.run(build_prompt(task, str(self.repo), _FakeLlama(),
                                         max_output_tokens=4096, rates=_FAST))
        narrowed = asyncio.run(build_prompt(hinted, str(self.repo), _FakeLlama(),
                                            max_output_tokens=4096, rates=_FAST,
                                            likely_files=("mod_4.py", "mod_1.py")))
        self.assertEqual(6, sum(name in plain.named_files for name in _NAMED))
        self.assertLessEqual(narrowed.prompt_tokens, plain.prompt_tokens)
        # Named and predicted come first (in the task's order), the rest is dropped.
        self.assertEqual(["mod_1.py", "mod_4.py"], narrowed.named_files)
        self.assertLessEqual(len(narrowed.named_files), _MAX_FILES)
        self.assertIn("Likely files: mod_4.py, mod_1.py", narrowed.prompt)

    def test_predicted_but_unnamed_file_follows_the_named_ones(self):
        task = _task("Change mod_0.py and mod_2.py")
        plan = asyncio.run(build_prompt(task, str(self.repo), _FakeLlama(), max_output_tokens=4096,
                                        rates=_FAST, likely_files=("mod_5.py", "mod_2.py")))
        self.assertEqual(["mod_2.py", "mod_5.py"], plan.named_files)

    def test_localization_time_reduces_the_remaining_budget(self):
        os.environ["NEXUS_LOCAL_LOCALIZE"] = "1"
        client, log = _FakeLlama(paths=["mod_2.py", "value.txt"], delay=0.3), _Log()
        worker = self._worker(client)
        seen: list[float] = []
        original = worker._correction_loop

        def capture(plan, task, working_dir, timeout, *args, **kwargs):
            seen.append(timeout)
            return original(plan, task, working_dir, timeout, *args, **kwargs)

        with mock.patch.object(worker, "_correction_loop", side_effect=capture):
            result = self._run(worker, _task(_GOAL), log)
        self.assertTrue(result.success, result.error)
        self.assertEqual(1, len(client.localize_calls))
        [event] = log.named("local_localization")
        self.assertEqual(["mod_2.py", "value.txt"], event["paths"])
        self.assertGreaterEqual(event["seconds"], 0.3)
        self.assertFalse(event["skipped"])
        self.assertAlmostEqual(300 - event["seconds"], seen[0], places=3)
        self.assertLess(seen[0], 300 - 0.3 + 1e-6)
        prompt = client.patch_calls[0]["prompt"]
        self.assertIn("Likely files: mod_2.py, value.txt", prompt)
        self.assertIn("--- mod_2.py ---", prompt)
        self.assertNotIn("--- mod_3.py ---", prompt)
        self.assertEqual("Likely files: mod_2.py, value.txt", worker.likely_files_hint)

    def test_unaffordable_rates_log_a_skip_and_make_no_call(self):
        os.environ["NEXUS_LOCAL_LOCALIZE"] = "1"
        client, log = _FakeLlama(paths=["mod_1.py"]), _Log()
        task = _task(_GOAL)
        self.assertIsNotNone(unaffordable_reason(task, _SLOW))
        result = self._run(self._worker(client, rates=_SLOW), task, log)
        self.assertEqual([], client.localize_calls)
        [event] = log.named("local_localization")
        self.assertTrue(event["skipped"])
        self.assertTrue(event["reason"])
        self.assertEqual([], event["paths"])
        if client.patch_calls:  # the patch call is still governed by its own admission
            self.assertNotIn(LIKELY_FILES_HEADING, client.patch_calls[0]["prompt"])
        self.assertIsNotNone(result)

    def test_too_short_remaining_run_budget_skips(self):
        os.environ["NEXUS_LOCAL_LOCALIZE"] = "1"
        client, log = _FakeLlama(paths=["mod_1.py"]), _Log()
        self._run(self._worker(client), _task(_GOAL), log, timeout=30)
        self.assertEqual([], client.localize_calls)
        self.assertIn("remaining run budget", log.named("local_localization")[0]["reason"])

    def test_existing_hint_is_reused_without_a_second_call(self):
        os.environ["NEXUS_LOCAL_LOCALIZE"] = "1"
        client, log = _FakeLlama(paths=["mod_5.py"]), _Log()
        task = with_hint(_task(_GOAL), hint_line(["value.txt", "mod_3.py"]))
        worker = self._worker(client)
        result = self._run(worker, task, log)
        self.assertTrue(result.success, result.error)
        self.assertEqual([], client.localize_calls)
        [event] = log.named("local_localization")
        self.assertTrue(event["reused"])
        self.assertEqual(["value.txt", "mod_3.py"], event["paths"])
        prompt = client.patch_calls[0]["prompt"]
        self.assertEqual(1, prompt.count(LIKELY_FILES_HEADING))
        self.assertNotIn("--- mod_0.py ---", prompt)

    def test_hint_is_not_a_handover_end_marker(self):
        self.assertFalse(any(LIKELY_FILES_HEADING in marker for marker in _HANDOVER_END_MARKERS))

    def test_hint_helpers(self):
        line = hint_line(["a.py", "b/c.py"])
        self.assertEqual("Likely files: a.py, b/c.py", line)
        self.assertEqual(["a.py", "b/c.py"], parse_likely_files("x\n\n" + line + "\n"))
        self.assertEqual("task", with_hint("task", ""))
        once = with_hint("task", line)
        self.assertEqual(once, with_hint(once, hint_line(["other.py"])))

    def test_paid_coder_handover_gets_the_same_hint_line(self):
        paid_tasks: list[str] = []
        hint = hint_line(["value.txt", "mod_1.py"])

        class _Local:
            provider = "local"
            likely_files_hint = hint
            generated_tests = ()
            local_attempt_passed_generated_tests = None

            def run(self, *, task, working_dir, sandbox, **_kw):
                return _result(False, task, working_dir, sandbox, "local",
                               error=f"{CONTEXT_TOO_SMALL}: too big")

        class _Paid:
            provider = "codex"

            def run(self, *, task, working_dir, sandbox, **_kw):
                paid_tasks.append(task)
                Path(working_dir, "value.txt").write_text("changed\n")
                return _result(True, task, working_dir, sandbox, "codex")

        service = ExperimentService(
            db_path=str(self.root / "agent.db"), repo_root=str(self.repo),
            worktrees_root=str(self.root / "worktrees"),
            local_worker_factory=lambda _p: _Local(),
            worker_factory=lambda _p: _Paid(),
            test_command=["/bin/true"], max_repair_iterations=0,
            experiment_log_dir=str(self.root / "logs"),
        )
        try:
            exp = service.create_experiment("Change value.txt", complexity="small",
                                            source_type="issue", source_id="720-handover")
            service.run_experiment(exp.id)
        finally:
            service.close()
        self.assertEqual(1, len(paid_tasks))
        self.assertIn("\n" + hint, paid_tasks[0])
        self.assertEqual(1, paid_tasks[0].count(LIKELY_FILES_HEADING))


if __name__ == "__main__":
    unittest.main()
