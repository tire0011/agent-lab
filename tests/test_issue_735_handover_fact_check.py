"""#735: handover fact check of the paths, modules and line refs an issue names."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modules.coding_worker import WorkerResult
from modules.experiment_service import ExperimentService
from modules.experiment_service.prompts import _build_direct_implementation_prompt
from modules.handover_fact_check import (
    ENV_HANDOVER_FACT_CHECK,
    EVENT,
    REPO_FACTS_HEADING,
    check_references,
    facts_block,
    run_check,
    with_facts,
)
from modules.local_coding_worker import CONTEXT_TOO_SMALL, LocalCodingWorker

_PATCH = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n"
_FAST = (5000.0, 500.0)


def _timing_check_source() -> str:
    lines = [f"filler_{i} = {i}\n" for i in range(169)]
    lines.append("def check_timing():\n")  # line 170
    lines.append("    return True\n")
    return "".join(lines)


class _FakeLlama:
    """llama-server stand-in: /props, /tokenize, JSON-schema calls and the patch call."""

    def __init__(self, *, choice: dict | None = None, fail: bool = False):
        self.choice = choice or {}
        self.fail = fail
        self.schema_calls: list[dict] = []
        self.patch_calls: list[dict] = []

    async def context_size(self) -> int:
        return 32768

    async def count_tokens(self, text: str) -> int:
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 0, **kw) -> str:
        if kw.get("response_format"):
            self.schema_calls.append({"prompt": prompt, "max_tokens": max_tokens,
                                      "response_format": kw["response_format"]})
            if self.fail:
                raise ConnectionError("llama-server is down")
            if "file_localization" in json.dumps(kw["response_format"]):
                return '{"paths": []}'
            return json.dumps(self.choice)
        self.patch_calls.append({"prompt": prompt, "system": system})
        return _PATCH


class _Log:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def info(self, phase, event, message, **fields):
        self.events.append((event, message, fields))

    warning = error = debug = info

    def named(self, event: str) -> list[dict]:
        return [fields for name, _message, fields in self.events if name == event]


def _result(success: bool, task: str, working_dir: str, sandbox: str, provider: str,
            error: str | None = None) -> WorkerResult:
    return WorkerResult(success=success, exit_code=0 if success else 1, stdout="", stderr="",
                        runtime_ms=1, timed_out=False, stdout_truncated=False,
                        stderr_truncated=False, thread_id=None, final_message=None, usage=None,
                        task=task, working_dir=working_dir, sandbox=sandbox, error=error,
                        provider=provider)


class Issue735HandoverFactCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        files = {
            "value.txt": "original\n",
            "modules/__init__.py": "",
            "modules/run_store/__init__.py": "def get_setting():\n    return 1\n",
            "modules/timing_check.py": _timing_check_source(),
            "modules/existing.py": "def present():\n    return 1\n",
            "modules/a/helpers.py": "def a_helper():\n    pass\n",
            "modules/b/helpers.py": "def b_helper():\n    pass\n",
            "scripts/real_script.py": "print('ok')\n",
            "tests/test_existing.py": "def test_ok():\n    pass\n",
        }
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in (ENV_HANDOVER_FACT_CHECK, "NEXUS_LOCAL_LOCALIZE"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _one(self, text: str, **kw):
        refs = check_references(text, self.repo, **kw)
        self.assertEqual(1, len(refs), refs)
        return refs[0]

    # Deterministic resolution

    def test_file_that_became_a_package_is_moved(self):
        ref = self._one("The coder ran `sed -n 1,80p modules/run_store.py` and failed.")
        self.assertEqual("moved", ref.status)
        self.assertEqual("modules/run_store/__init__.py", ref.path)
        self.assertIn("`modules/run_store.py`: moved; it is modules/run_store/__init__.py",
                      facts_block([ref]))

    def test_dotted_module_of_a_package_exists(self):
        self.assertEqual("exists", self._one("see `modules.run_store`").status)

    def test_non_existent_script_is_missing(self):
        ref = self._one("First run `python scripts/print_repo_tree.py`.")
        self.assertEqual("missing", ref.status)
        self.assertIn("`scripts/print_repo_tree.py`: missing", facts_block([ref]))

    def test_line_ref_with_moved_symbol_is_line_shifted(self):
        ref = self._one("`check_timing()` at `timing_check.py:161` is too strict.")
        self.assertEqual("line_shifted", ref.status)
        self.assertEqual(161, ref.line)
        self.assertEqual(170, ref.new_line)
        self.assertEqual("modules/timing_check.py", ref.path)
        self.assertIn("modules/timing_check.py:170", facts_block([ref]))

    def test_line_ref_still_matching_exists(self):
        self.assertEqual("exists", self._one("`check_timing()` at `modules/timing_check.py:170`").status)

    def test_existing_paths_add_no_noise(self):
        text = ("Edit `modules/existing.py` (see `present()`), `scripts/real_script.py`, "
                "`modules.existing` and `tests/test_*.py`. Also value.txt.")
        refs = check_references(text, self.repo)
        statuses = {ref.token: ref.status for ref in refs}
        self.assertTrue(refs)
        self.assertTrue(all(status == "exists" for status in statuses.values()), statuses)
        self.assertEqual("", facts_block(refs))
        task = _build_direct_implementation_prompt(text)
        self.assertEqual(task, with_facts(task, facts_block(refs)))

    def test_function_moved_to_another_file(self):
        refs = check_references("`get_setting()` in `modules/existing.py`", self.repo)
        moved = [r for r in refs if r.kind == "symbol"]
        self.assertEqual(["moved"], [r.status for r in moved])
        self.assertEqual("modules/run_store/__init__.py", moved[0].path)

    # The one bounded llama call for ambiguous matches

    def test_ambiguous_basename_calls_llama_once(self):
        client = _FakeLlama(choice={"r0": "modules/b/helpers.py"})
        stats: dict = {}
        ref = self._one("Fix `modules/helpers.py` first.", client=client, stats=stats)
        self.assertEqual(1, len(client.schema_calls))
        self.assertTrue(stats["llama_called"])
        call = client.schema_calls[0]
        self.assertEqual("json_schema", call["response_format"]["type"])
        schema = call["response_format"]["json_schema"]["schema"]
        self.assertEqual(["modules/a/helpers.py", "modules/b/helpers.py"],
                         sorted(schema["properties"]["r0"]["enum"]))
        self.assertEqual(("moved", "modules/b/helpers.py", "llama"),
                         (ref.status, ref.path, ref.chosen_by))
        self.assertIn("it is modules/b/helpers.py", facts_block([ref]))

    def test_missing_llama_falls_back_to_listing_candidates(self):
        for client in (None, _FakeLlama(fail=True), _FakeLlama(choice={"r0": "not/a/candidate.py"})):
            with self.subTest(client=client):
                ref = self._one("Fix `modules/helpers.py` first.", client=client)
                self.assertEqual("moved", ref.status)
                self.assertIsNone(ref.path)
                block = facts_block([ref])
                self.assertIn("candidates: ", block)
                self.assertIn("modules/a/helpers.py", block)
                self.assertIn("modules/b/helpers.py", block)

    def test_unambiguous_names_never_call_llama(self):
        client = _FakeLlama()
        check_references("`modules/run_store.py`, `scripts/nope.py`, `modules/existing.py`",
                         self.repo, client=client)
        self.assertEqual([], client.schema_calls)

    # Flag and event

    def test_run_check_logs_one_event_with_counts(self):
        log = _Log()
        block = run_check("`modules/run_store.py`, `scripts/nope.py`, `modules/existing.py`",
                          self.repo, experiment_logger=log)
        self.assertTrue(block.startswith(REPO_FACTS_HEADING))
        events = log.named(EVENT)
        self.assertEqual(1, len(events))
        self.assertEqual((1, 1, 0, 1, 3), (events[0]["exists"], events[0]["moved"],
                                           events[0]["line_shifted"], events[0]["missing"],
                                           events[0]["references"]))

    def test_flag_off_means_no_block_no_call_no_event(self):
        os.environ[ENV_HANDOVER_FACT_CHECK] = "0"
        client, log = _FakeLlama(choice={"r0": "modules/a/helpers.py"}), _Log()
        self.assertIsNone(run_check("Fix `modules/helpers.py`.", self.repo, client=client,
                                    experiment_logger=log))
        self.assertEqual([], client.schema_calls)
        self.assertEqual([], log.named(EVENT))
        worker = LocalCodingWorker(client=client, rates_path=self.root / "rates.json")
        worker.rates.medians = lambda: _FAST  # type: ignore[method-assign]
        result = worker.run(task=_build_direct_implementation_prompt("Fix `modules/helpers.py`."),
                            working_dir=str(self.repo), sandbox="workspace-write",
                            timeout_seconds=300, experiment_logger=log)
        self.assertTrue(result.success, result.error)
        self.assertNotIn(REPO_FACTS_HEADING, client.patch_calls[0]["prompt"])
        self.assertEqual([], client.schema_calls)
        self.assertEqual([], log.named(EVENT))

    # Both prompts

    def test_local_worker_prompt_gets_the_block(self):
        client, log = _FakeLlama(choice={"r0": "modules/a/helpers.py"}), _Log()
        worker = LocalCodingWorker(client=client, rates_path=self.root / "rates.json")
        worker.rates.medians = lambda: _FAST  # type: ignore[method-assign]
        goal = "Change value.txt; the old code is in `modules/run_store.py` and `modules/helpers.py`."
        result = worker.run(task=_build_direct_implementation_prompt(goal),
                            working_dir=str(self.repo), sandbox="workspace-write",
                            timeout_seconds=300, experiment_logger=log)
        self.assertTrue(result.success, result.error)
        prompt = client.patch_calls[0]["prompt"]
        self.assertEqual(1, prompt.count(REPO_FACTS_HEADING))
        self.assertIn("it is modules/run_store/__init__.py", prompt)
        self.assertIn("it is modules/a/helpers.py", prompt)
        facts_calls = [c for c in client.schema_calls
                       if "repo_fact_choice" in json.dumps(c["response_format"])]
        self.assertEqual(1, len(facts_calls))
        self.assertEqual(1, len(log.named(EVENT)))
        self.assertTrue(worker.repo_facts_checked)
        self.assertIn("modules/run_store/__init__.py", worker.repo_facts_block)

    def _service(self, local_factory, paid_factory) -> ExperimentService:
        return ExperimentService(
            db_path=str(self.root / "agent.db"), repo_root=str(self.repo),
            worktrees_root=str(self.root / "worktrees"),
            local_worker_factory=local_factory, worker_factory=paid_factory,
            test_command=["/bin/true"], max_repair_iterations=0,
            experiment_log_dir=str(self.root / "logs"),
        )

    def _paid(self, tasks: list[str]):
        class _Paid:
            provider = "codex"

            def run(self, *, task, working_dir, sandbox, **_kw):
                tasks.append(task)
                Path(working_dir, "value.txt").write_text("changed\n")
                return _result(True, task, working_dir, sandbox, "codex")

        return _Paid()

    def test_paid_handover_gets_the_block(self):
        paid_tasks: list[str] = []
        service = self._service(lambda _p: None, lambda _p: self._paid(paid_tasks))
        try:
            exp = service.create_experiment(
                "Change value.txt; see `modules/run_store.py` and `scripts/print_repo_tree.py`.",
                complexity="large", source_type="issue", source_id="735-paid")
            service.run_experiment(exp.id)
        finally:
            service.close()
        self.assertEqual(1, len(paid_tasks))
        self.assertEqual(1, paid_tasks[0].count(REPO_FACTS_HEADING))
        self.assertIn("it is modules/run_store/__init__.py", paid_tasks[0])
        self.assertIn("`scripts/print_repo_tree.py`: missing", paid_tasks[0])

    def test_paid_fallback_reuses_the_local_block(self):
        paid_tasks: list[str] = []
        block = f"{REPO_FACTS_HEADING}\n- `x.py`: missing; from the local run"

        class _Local:
            provider = "local"
            likely_files_hint = ""
            repo_facts_checked = True
            repo_facts_block = block
            generated_tests = ()
            local_attempt_passed_generated_tests = None

            def run(self, *, task, working_dir, sandbox, **_kw):
                return _result(False, task, working_dir, sandbox, "local",
                               error=f"{CONTEXT_TOO_SMALL}: too big")

        service = self._service(lambda _p: _Local(), lambda _p: self._paid(paid_tasks))
        try:
            exp = service.create_experiment("Change value.txt; see `modules/run_store.py`.",
                                            complexity="small", source_type="issue",
                                            source_id="735-fallback")
            service.run_experiment(exp.id)
        finally:
            service.close()
        self.assertEqual(1, len(paid_tasks))
        self.assertEqual(1, paid_tasks[0].count(REPO_FACTS_HEADING))
        self.assertIn(block, paid_tasks[0])


if __name__ == "__main__":
    unittest.main()
