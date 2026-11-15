import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from modules.llama_cpp_client import LlamaCppClient
from modules.local_coding_worker import PromptPlan, patch_grammar, sr_grammar_paths
from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.local_shadow_replay_hunks import UnitLogger
from modules.run_store.schema import init_db
from modules.sr_grammar import ENV_SR_GRAMMAR, build_sr_grammar, sr_grammar_enabled


class GrammarTests(unittest.TestCase):
    def test_builds_path_constrained_edit_block_grammar(self):
        grammar = build_sr_grammar(["z.py", "pkg/a.py", "pkg/a.py", "../bad.py"])
        self.assertIn('path ::= "pkg/a.py" | "z.py"', grammar)
        self.assertIn('"\\n<<<<<<< SEARCH\\n"', grammar)
        self.assertIn('">>>>>>> REPLACE\\n"', grammar)
        self.assertNotIn("../bad.py", grammar)

    def test_requires_a_usable_path(self):
        with self.assertRaises(ValueError):
            build_sr_grammar(["/absolute.py", "../outside.py", "bad\\path.py"])

    def test_flag_defaults_on_and_zero_disables_it(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_SR_GRAMMAR, None)
            self.assertTrue(sr_grammar_enabled())
        with patch.dict(os.environ, {ENV_SR_GRAMMAR: "0"}):
            self.assertFalse(sr_grammar_enabled())


class ClientTests(unittest.TestCase):
    def test_complete_sends_grammar_only_when_provided(self):
        payloads = []

        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        client = LlamaCppClient(base_url="http://fake/v1", stream=False,
                                transport=httpx.MockTransport(handler))
        asyncio.run(client.complete("first", grammar='root ::= "x"'))
        asyncio.run(client.complete("second"))
        self.assertEqual(payloads[0]["grammar"], 'root ::= "x"')
        self.assertNotIn("grammar", payloads[1])


class WorkerGrammarSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.plan = PromptPlan(prompt="", n_ctx=4096, n_ctx_source="test", budget_tokens=1000,
                               prompt_tokens=0, token_source="test", named_files=["pkg/value.py"],
                               essential="Change pkg/value.py and add tests/test_value.py")

    def test_paths_include_named_and_new_files_but_not_directories(self):
        paths = sr_grammar_paths(self.plan, str(self.root))
        self.assertIn("pkg/value.py", paths)
        self.assertIn("tests/test_value.py", paths)
        self.assertNotIn("pkg", paths)

    def test_grammar_is_only_for_enabled_sr_mode(self):
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "1"}):
            self.assertIsNotNone(patch_grammar(self.plan, str(self.root)))
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "0"}):
            self.assertIsNone(patch_grammar(self.plan, str(self.root)))
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "diff", ENV_SR_GRAMMAR: "1"}):
            self.assertIsNone(patch_grammar(self.plan, str(self.root)))


class ReplayMetricsTests(unittest.TestCase):
    def test_unit_logger_records_requested_fields(self):
        logger = UnitLogger()
        logger.info("phase", "local_patch_round", outcome="edit_format", sr_grammar=True)
        logger.info("phase", "local_patch_round", outcome="applied", sr_grammar=True)
        logger.info("phase", "local_model_call", call_type="patch",
                    timings={"predicted_n": 30, "predicted_ms": 10000.0})
        self.assertTrue(logger.sr_grammar)
        self.assertEqual(logger.format_rounds, 1)
        self.assertEqual(logger.generation_tokens_per_second, 3.0)

    def test_patch_model_call_alone_records_the_grammar_flag(self):
        # #706 review: a patch call that raises leaves only local_model_call.
        logger = UnitLogger()
        logger.info("phase", "local_model_call", call_type="patch", outcome="error", sr_grammar=True)
        self.assertTrue(logger.sr_grammar)
        self.assertEqual(logger.rounds, 0)

    def test_non_patch_calls_and_missing_flags_do_not_set_it(self):
        logger = UnitLogger()
        logger.info("phase", "local_model_call", call_type="test_first", sr_grammar=True)
        logger.info("phase", "local_model_call", call_type="patch")
        logger.info("phase", "local_model_call", call_type="patch", sr_grammar="yes")
        self.assertIsNone(logger.sr_grammar)
        logger.info("phase", "local_model_call", call_type="patch", sr_grammar=False)
        self.assertIs(logger.sr_grammar, False)

    def test_repeated_events_never_turn_a_sent_grammar_back_off(self):
        logger = UnitLogger()
        for value in (True, False, True, False):
            logger.warning("phase", "local_model_call", call_type="patch", sr_grammar=value)
            logger.warning("phase", "local_patch_round", outcome="model_error", sr_grammar=value)
        self.assertTrue(logger.sr_grammar)

    def test_schema_has_replay_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "test.db"))
            try:
                replay = {row[1] for row in conn.execute("PRAGMA table_info(local_replays)")}
                units = {row[1] for row in conn.execute("PRAGMA table_info(local_replay_units)")}
            finally:
                conn.close()
        self.assertIn("sr_grammar", replay)
        self.assertTrue({"sr_grammar", "format_rounds", "generation_tokens_per_second"} <= units)


class _FailingClient:
    """A llama-server stand-in whose completion fails with a non-timeout error."""

    model = "fake-local-model"

    def __init__(self, error):
        self.error = error
        self.grammars = []

    async def is_available(self):
        return True

    async def busy_slots(self):
        return 0

    async def complete(self, prompt, *, system=None, temperature=0.0, max_tokens=1024,
                       seed=None, grammar=None):
        self.grammars.append(grammar)
        raise self.error


class _RaisingWorker:
    """The real worker, then a crash out of ``run`` (the ``_score_unit`` except path)."""

    def __init__(self, client):
        self.inner = lsh.hunk_worker_factory(client)

    def run(self, **kwargs):
        self.inner.run(**kwargs)
        raise RuntimeError("worker crashed after the patch call")


class HunkReplayGrammarTests(unittest.TestCase):
    """#706 review: unit rows keep sr_grammar when the patch call raises."""

    def setUp(self):
        from tests.test_issue_649_hunk_replay import HunkFixture

        self.fx = HunkFixture(self)
        env = patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "1"})
        env.start()
        self.addCleanup(env.stop)

    def replay(self, client, factory=lsh.hunk_worker_factory, variant="v1"):
        return lsr.replay("exp-1", repo_root=self.fx.repo, db_path=self.fx.db_path, variant=variant,
                          client=client, worker_factory=factory, unit="hunk")

    def test_model_error_before_any_patch_round_still_stores_the_flag(self):
        client = _FailingClient(RuntimeError("server said 500"))
        self.replay(client)
        self.assertTrue(client.grammars and all(g for g in client.grammars))
        for key, row in self.fx.units().items():
            self.assertEqual(row["rounds"], 0, key)  # no local_patch_round was logged
            self.assertEqual(row["sr_grammar"], 1, key)
            self.assertIn("server said 500", row["error"])

    def test_worker_exception_path_stores_the_flag(self):
        client = _FailingClient(RuntimeError("server said 500"))
        summary = self.replay(client, factory=_RaisingWorker)
        rows = self.fx.units()
        self.assertEqual(summary["status"], lsh.RUN_DONE)
        for key, row in rows.items():
            self.assertEqual(row["status"], "error", key)
            self.assertIn("worker crashed", row["error"])
            self.assertEqual(row["sr_grammar"], 1, key)
        self.assertEqual(lsh.unit_variant_summary(db_path=self.fx.db_path)[0]["sr_grammar_units"], 2)

    def test_disabled_flag_stores_false_and_sends_no_grammar(self):
        client = _FailingClient(RuntimeError("server said 500"))
        with patch.dict(os.environ, {ENV_SR_GRAMMAR: "0"}):
            self.replay(client)
        self.assertTrue(client.grammars)
        self.assertEqual(set(client.grammars), {None})
        self.assertEqual({row["sr_grammar"] for row in self.fx.units().values()}, {0})

    def test_interrupted_unit_rerun_stores_only_the_rerun_flag(self):
        with self.assertRaises(KeyboardInterrupt):
            self.replay(_FailingClient(KeyboardInterrupt()))
        area = self.fx.units()[("pkg/shapes.py", "area")]
        self.assertEqual(area["status"], "running")
        self.assertIsNone(area["sr_grammar"])  # nothing half-written on interruption

        # The rerun has grammar off: the interrupted attempt must not leak True in.
        with patch.dict(os.environ, {ENV_SR_GRAMMAR: "0"}):
            self.replay(_FailingClient(RuntimeError("server said 500")))
        rows = self.fx.units()
        self.assertNotIn("running", {row["status"] for row in rows.values()})
        self.assertEqual({row["sr_grammar"] for row in rows.values()}, {0})

    def test_second_replay_is_a_no_op_and_keeps_the_stored_flag(self):
        self.replay(_FailingClient(RuntimeError("server said 500")))
        again = _FailingClient(RuntimeError("server said 500"))
        with patch.dict(os.environ, {ENV_SR_GRAMMAR: "0"}):
            summary = self.replay(again)
        self.assertEqual(again.grammars, [])
        self.assertEqual(summary["ran_units"], 0)
        self.assertEqual({row["sr_grammar"] for row in self.fx.units().values()}, {1})


if __name__ == "__main__":
    unittest.main()
