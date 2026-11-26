"""#727: local-model triage of no-drift verification failures by error signature."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import failure_triage as ft
from modules.failure_triage import group_failures, signature, triage
from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.llama_cpp_client import LlamaCppClientError
from modules.run_store import init_db

ROOT = Path(__file__).resolve().parents[1]

PLAIN = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        pass\n"
)

TAIL_401 = (
    "tests/test_api.py::TestApi::test_secure_health\n"
    "--------------------------------- Captured log ---------------------------------\n"
    '  File "/tmp/abc123/tests/test_api.py", line 42, in test_secure_health\n'
    "    assert response.status_code == 200\n"
    "E   AssertionError: assert 401 == 200\n"
    "E    +  where 401 = response.status_code\n"
)


class SignatureTests(unittest.TestCase):
    def test_same_exception_with_different_numbers_paths_and_hex_is_equal(self) -> None:
        a = (
            '  File "/tmp/abc123/tests/test_a.py", line 42, in test_x\n'
            "E   AssertionError: assert 401 == 200\n"
        )
        b = (
            '  File "/var/zzz9/tests/test_b.py", line 7, in test_y\n'
            "E   AssertionError: assert 404 == 500\n"
        )
        self.assertEqual(signature(a), signature(b))
        self.assertTrue(signature(a).startswith("AssertionError: "))
        self.assertNotIn("401", signature(a))
        self.assertNotIn("/tmp", signature(a))

    def test_hex_ids_and_paths_are_normalised(self) -> None:
        a = "RuntimeError: worker 0xdeadbeef died on /tmp/x1/run.py"
        b = "RuntimeError: worker 0x1234abcd died on /var/y2/run.py"
        self.assertEqual(signature(a), signature(b))
        self.assertIn("HEX", signature(a))
        self.assertIn("PATH", signature(a))

    def test_type_only_when_the_message_is_empty(self) -> None:
        self.assertEqual(signature("Traceback ...\nTimeoutError"), "TimeoutError")

    def test_different_exceptions_differ(self) -> None:
        self.assertNotEqual(signature(TAIL_401), signature("ValueError: boom"))

    def test_deterministic(self) -> None:
        self.assertEqual(signature(TAIL_401), signature(TAIL_401))


class GroupFailuresTests(unittest.TestCase):
    def test_forty_tails_with_one_shared_401_form_one_group(self) -> None:
        tails = {
            f"tests/test_api.py::TestApi::test_secure_{n}": (
                f'  File "/tmp/x{n}/tests/test_api.py", line {n}, in test_secure_{n}\n'
                f"E   AssertionError: assert 401 == 200\n"
            )
            for n in range(40)
        }
        groups = group_failures(tails)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 40)
        self.assertEqual(len(groups[0]["test_ids"]), 40)
        self.assertIn("AssertionError", groups[0]["signature"])

    def test_groups_are_sorted_by_count(self) -> None:
        tails = {
            "tests/a.py::test_1": "AssertionError: one",
            "tests/a.py::test_2": "AssertionError: one",
            "tests/b.py::test_1": "TimeoutError",
        }
        groups = group_failures(tails)
        self.assertEqual([g["count"] for g in groups], [2, 1])
        self.assertEqual(groups[0]["signature"], "AssertionError: one")
        self.assertEqual(groups[1]["signature"], "TimeoutError")


class ExplodingClient:
    """A client that must never be called (the rule fires first)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("the model was called")


class FakeClient:
    """A fake local model answering a fixed JSON string."""

    def __init__(self, answer: str, *, available: bool = True) -> None:
        self.answer = answer
        self.available = available
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[float] = []
        self.last_timings = None

    async def is_available(self) -> bool:
        return self.available

    async def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        self.timeouts.append(getattr(self, "timeout", 0.0))
        return self.answer


class TriageRuleTests(unittest.TestCase):
    def test_rule_gives_host_blocker_with_zero_model_calls(self) -> None:
        tails = {
            f"tests/test_task_orchestrator.py::test_{n}": "AssertionError: assert 401 == 200"
            for n in range(12)
        }
        groups = group_failures(tails)
        client = ExplodingClient()
        result = triage(groups, ["modules/some_module.py"], client=client)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "host_blocker")
        self.assertEqual(result[0]["source"], "rule")
        self.assertEqual(result[0]["count"], 12)
        self.assertEqual(client.calls, 0)

    def test_a_small_group_goes_to_the_model_even_without_changed_files(self) -> None:
        groups = group_failures({"tests/test_x.py::test_a": "AssertionError: boom 1"})
        client = FakeClient(json.dumps({"label": "unclear", "why": "only one test"}))
        result = triage(groups, [], client=client)
        self.assertEqual(result[0]["label"], "unclear")
        self.assertEqual(result[0]["source"], "model")
        self.assertEqual(len(client.calls), 1)


class TriageModelTests(unittest.TestCase):
    def _groups(self) -> list[dict]:
        return group_failures({
            "tests/test_changed.py::test_a": "AssertionError: boom 1",
            "tests/test_changed.py::test_b": "AssertionError: boom 2",
        })

    def test_group_in_a_changed_file_goes_to_the_model_and_the_answer_is_parsed(self) -> None:
        client = FakeClient(json.dumps({"label": "experiment_fault", "why": "the changed test file fails"}))
        result = triage(self._groups(), ["tests/test_changed.py"], client=client)
        self.assertEqual(result[0]["label"], "experiment_fault")
        self.assertEqual(result[0]["why"], "the changed test file fails")
        self.assertEqual(result[0]["source"], "model")
        self.assertEqual(len(client.calls), 1)
        prompt, kwargs = client.calls[0]
        self.assertIn("AssertionError", prompt)          # the signature is in the prompt
        self.assertIn("tests/test_changed.py", prompt)   # the changed files are in the prompt
        self.assertEqual(kwargs["max_tokens"], ft.MAX_OUTPUT_TOKENS)
        self.assertEqual(kwargs["max_tokens"], 120)
        self.assertIn("response_format", kwargs)
        self.assertIn("json_schema", str(kwargs["response_format"]))
        self.assertGreater(client.timeouts[0], 0)

    def test_a_test_importing_a_changed_module_goes_to_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tests").mkdir()
            (repo / "modules").mkdir()
            (repo / "modules" / "foo.py").write_text("X = 1\n", encoding="utf-8")
            (repo / "tests" / "test_imp.py").write_text(
                "import modules.foo\n", encoding="utf-8",
            )
            groups = group_failures({"tests/test_imp.py::test_a": "AssertionError: boom"})
            client = FakeClient(json.dumps({"label": "experiment_fault", "why": "imports modules.foo"}))
            result = triage(groups, ["modules/foo.py"], client=client, repo=repo)
            self.assertEqual(result[0]["label"], "experiment_fault")
            self.assertEqual(len(client.calls), 1)
            # Without the repo the import cannot be read, but the rule threshold
            # (10 tests) is not reached either, so the model is asked anyway.
            client2 = FakeClient(json.dumps({"label": "unclear", "why": "?"}))
            result2 = triage(groups, ["modules/foo.py"], client=client2)
            self.assertEqual(result2[0]["source"], "model")

    def test_malformed_model_json_gives_unclear(self) -> None:
        for answer in ("not json at all", '{"label": "bogus"}', '{"why": "no label"}',
                       "[1, 2, 3]"):
            with self.subTest(answer=answer):
                result = triage(self._groups(), ["tests/test_changed.py"],
                                client=FakeClient(answer))
                self.assertEqual(result[0]["label"], "unclear")
                self.assertEqual(result[0]["source"], "model_error")

    def test_a_timeout_gives_unclear(self) -> None:
        class TimeoutClient(FakeClient):
            async def complete(self, prompt, **kwargs):
                self.calls.append((prompt, kwargs))
                raise LlamaCppClientError("local model call timed out", reason="timeout")

        result = triage(self._groups(), ["tests/test_changed.py"], client=TimeoutClient("x"))
        self.assertEqual(result[0]["label"], "unclear")
        self.assertIn("timed out", result[0]["why"])

    def test_an_unavailable_model_gives_unclear(self) -> None:
        groups = group_failures({"tests/test_x.py::test_a": "AssertionError: boom"})
        with patch.object(ft, "_default_client") as factory:
            factory.return_value = FakeClient("{}", available=False)
            result = triage(groups, ["modules/foo.py"], client=None)
        self.assertEqual(result[0]["label"], "unclear")
        self.assertEqual(result[0]["source"], "unavailable")

    def test_two_sample_tails_are_in_the_prompt(self) -> None:
        tails = {
            "tests/test_changed.py::test_a": "AssertionError: boom 1",
            "tests/test_changed.py::test_b": "AssertionError: boom 2",
            "tests/test_changed.py::test_c": "AssertionError: boom 3",
        }
        groups = group_failures(tails)
        client = FakeClient(json.dumps({"label": "unclear", "why": "?"}))
        triage(groups, ["tests/test_changed.py"], client=client, tails=tails)
        prompt, _ = client.calls[0]
        self.assertIn("boom 1", prompt)
        self.assertIn("boom 2", prompt)
        self.assertNotIn("boom 3", prompt)


class RunStoreTests(unittest.TestCase):
    def test_record_and_list_triage_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "db.sqlite"))
            try:
                groups = [
                    {"signature": "AssertionError: assert N == N", "test_ids": ["a", "b"],
                     "count": 40, "label": "host_blocker", "why": "rule", "source": "rule"},
                    {"signature": "TimeoutError", "test_ids": ["c"], "count": 2,
                     "label": "unclear", "why": "malformed", "source": "model_error"},
                ]
                self.assertEqual(ft.record_triage(conn, "exp1", groups), 2)
                rows = ft.list_triage(conn, experiment_id="exp1")
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["label"], "unclear")  # newest first
                self.assertEqual(rows[1]["label"], "host_blocker")
                self.assertEqual(rows[1]["test_ids"], ["a", "b"])
                self.assertEqual(rows[1]["count"], 40)
                self.assertEqual(rows[1]["gate"], "timing_check")
                self.assertEqual(ft.list_triage(conn, label="host_blocker"), [rows[1]])
                self.assertEqual(ft.list_triage(conn, experiment_id="other"), [])
            finally:
                conn.close()

    def test_early_signal_drops_the_required_streak_to_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "db.sqlite"))
            try:
                self.assertEqual(ft.required_streak_length(conn), 3)
                group = {"signature": "AssertionError: assert N == N", "count": 40,
                         "label": "host_blocker"}
                ft.record_early_signal(conn, "exp1", group)
                self.assertEqual(ft.required_streak_length(conn), 2)
                signal = ft.early_signal(conn)
                self.assertEqual(signal["experiment_id"], "exp1")
                self.assertEqual(signal["signature"], group["signature"])
                self.assertEqual(ft.required_streak_length(conn, default=5), 2)
            finally:
                conn.close()


class McpToolTests(unittest.TestCase):
    def test_failure_triage_list_is_registered_and_read_only(self) -> None:
        from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS

        tree = ast.parse((ROOT / "nexus_mcp.py").read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    func = deco.func if isinstance(deco, ast.Call) else deco
                    if isinstance(func, ast.Attribute) and func.attr == "tool":
                        names.append(node.name)
        self.assertIn("failure_triage_list", names)
        self.assertIn("failure_triage_list", READ_ONLY_TOOLS)
        self.assertNotIn("failure_triage_list", WRITE_TOOLS)


class VerificationTriageTests(unittest.TestCase):
    """The timing check's failed-closed path carries the triage (#727)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self.worker_tasks: list[str] = []
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=1,
            worker_factory=lambda _: self._worker(), plan_worker_factory=lambda _: None,
        )
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _worker(self):
        worker = MagicMock()

        def run(*, task, **_kwargs):
            self.worker_tasks.append(task)
            return MagicMock(usage=None, timed_out=False, error=None)

        worker.run.side_effect = run
        return worker

    def _verify(self, timing_check: str = "1"):
        (self.repo / "tests" / "test_new.py").write_text(PLAIN, encoding="utf-8")
        experiment = self.service.create_experiment("Add a test")
        base = self._git("rev-parse", "HEAD").strip()
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": timing_check}):
            result = self.service._verify(experiment.id, str(self.repo))
        return experiment.id, result

    @staticmethod
    def _failing_run(tails: dict[str, str]):
        def run(repo, tests, *args, **kwargs):
            sink = kwargs.get("tails")
            if sink is not None:
                sink.update(tails)
            return "error"

        return run

    def test_needs_review_reason_contains_the_top_group_and_rows_are_stored(self) -> None:
        tails = {
            f"tests/test_other.py::test_{n}": (
                f'  File "/tmp/x{n}/tests/test_other.py", line {n}\n'
                "E   AssertionError: assert 401 == 200\n"
            )
            for n in range(12)
        }
        with patch("modules.timing_check.run_timing_check",
                   side_effect=self._failing_run(tails)), \
                patch.object(ft, "_default_client", side_effect=AssertionError("no model call")):
            experiment_id, result = self._verify("1")
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(result.error.startswith("timing check failed closed: error"))
        self.assertIn("failure triage", result.error)
        self.assertIn("host_blocker", result.error)
        self.assertIn("AssertionError", result.error)
        self.assertIn("12 tests", result.error)
        rows = ft.list_triage(self.service._conn, experiment_id=experiment_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "host_blocker")
        self.assertEqual(rows[0]["source"], "rule")
        self.assertEqual(rows[0]["count"], 12)
        # The host_blocker group is the early signal for #714: 2 experiments.
        self.assertEqual(ft.required_streak_length(self.service._conn), 2)

    def test_a_group_in_a_changed_file_goes_to_the_model(self) -> None:
        tails = {"tests/test_new.py::T::test_ok": "E   AssertionError: assert 401 == 200\n"}
        client = FakeClient(json.dumps({"label": "experiment_fault", "why": "changed test file"}))
        with patch("modules.timing_check.run_timing_check",
                   side_effect=self._failing_run(tails)), \
                patch.object(ft, "_default_client", return_value=client):
            _, result = self._verify("1")
        self.assertEqual(result.status, "needs_review")
        self.assertIn("experiment_fault", result.error)
        self.assertEqual(len(client.calls), 1)
        rows = ft.list_triage(self.service._conn)
        self.assertEqual(rows[0]["label"], "experiment_fault")
        self.assertEqual(rows[0]["source"], "model")

    def test_nothing_changes_when_the_timing_check_passes(self) -> None:
        with patch("modules.timing_check.run_timing_check", return_value="ok"):
            _, result = self._verify("1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertFalse(result.error)
        self.assertEqual(ft.list_triage(self.service._conn), [])
        self.assertEqual(ft.required_streak_length(self.service._conn), 3)


if __name__ == "__main__":
    unittest.main()
