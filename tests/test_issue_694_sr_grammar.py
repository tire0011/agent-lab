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


if __name__ == "__main__":
    unittest.main()
