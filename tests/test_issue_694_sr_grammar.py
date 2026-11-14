"""#694: a GBNF grammar constrains the local model's SEARCH/REPLACE answer.

Covers the grammar text (paths, markers), what it accepts and rejects (with a
small GBNF-to-regex check of the real grammar text, plus llama.cpp's
``gbnf-validator`` when installed), the client's ``grammar`` field, when the
worker sends it (SR mode with the flag on; never with ``=0`` or in diff
mode), a fake llama-server whose grammar-constrained answer applies, and the
``sr_grammar`` record in the worker log and the replay records.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.llama_cpp_client import LlamaCppClient
from modules.local_coding_worker import LocalCodingWorker, PromptPlan, patch_grammar, sr_grammar_paths
from modules.local_edit_format import parse_edit_blocks
from modules.sr_grammar import ENV_SR_GRAMMAR, build_sr_grammar, sr_grammar_enabled
from tests.test_issue_637_local_shadow_replay import FakeWorker as ReplayWorker
from tests.test_issue_637_local_shadow_replay import ReplayFixture
from tests.test_issue_649_hunk_replay import MERGED_SHAPES, SHAPES, HunkFixture
from tests.test_issue_649_hunk_replay import FakeWorker as HunkWorker

SR_ENV = {"LOCAL_EDIT_FORMAT": "search_replace", "LOCAL_MIN_USEFUL_PATCH_TOKENS": "0"}
ALLOWED = ["pkg/value.py", "tests/test_value.py"]


def block(path, search, replacement):
    return f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"


VALID_TWO_BLOCKS = (block("pkg/value.py", "VALUE = 1\n", "VALUE = 2\n") + "\n"
                    + block("tests/test_value.py", "", "from pkg.value import VALUE\n\n"
                            "def test_value():\n    assert VALUE == 2\n"))


# ---- a pure-Python check of the grammar text --------------------------------

_TOKEN = re.compile(r'\s*(?:(?P<lit>"(?:\\.|[^"\\])*")|(?P<cls>\[(?:\\.|[^\]\\])*\])'
                    r'|(?P<name>[A-Za-z][A-Za-z0-9-]*)|(?P<op>[()|*+?]))')


def _unescape(body):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), body)


def gbnf_to_regex(grammar):
    """A Python regex for a *non-recursive* GBNF grammar (the subset build_sr_grammar uses)."""
    rules = {}
    for line in grammar.strip().splitlines():
        name, expr = line.split("::=", 1)
        rules[name.strip()] = expr.strip()

    def compile_rule(name, stack=()):
        assert name not in stack, f"recursive rule {name}"
        tokens = []
        text = rules[name]
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            assert m, f"cannot parse {text[pos:]!r}"
            tokens.append(m)
            pos = m.end()
        out = []
        for m in tokens:
            if m.group("lit"):
                out.append("(?:" + re.escape(_unescape(m.group("lit")[1:-1])) + ")")
            elif m.group("cls"):
                body = m.group("cls")[1:-1]
                body = re.sub(r"\\(.)", lambda e: {"n": "\\n", "t": "\\t", "r": "\\r"}.get(
                    e.group(1), re.escape(e.group(1))), body)
                out.append("[" + body + "]")
            elif m.group("name"):
                out.append("(?:" + compile_rule(m.group("name"), stack + (name,)) + ")")
            else:
                out.append({"(": "(?:"}.get(m.group("op"), m.group("op")))
        return "".join(out)

    return re.compile(compile_rule("root"), re.DOTALL)


def accepts(text, paths=ALLOWED):
    return gbnf_to_regex(build_sr_grammar(paths)).fullmatch(text) is not None


class GrammarTextTests(unittest.TestCase):
    def test_every_allowed_path_and_no_other(self):
        grammar = build_sr_grammar(ALLOWED + ["pkg/value.py"])
        path_rule = next(line for line in grammar.splitlines() if line.startswith("path ::="))
        self.assertEqual(sorted(re.findall(r'"([^"]*)"', path_rule)), sorted(ALLOWED))
        for path in ALLOWED:
            self.assertIn(f'"{path}"', grammar)
        self.assertNotIn("modules/", grammar)

    def test_unusable_paths_are_dropped_and_none_left_is_an_error(self):
        grammar = build_sr_grammar(["../etc/passwd", "/abs.py", "a\nb.py", "ok.py"])
        self.assertIn('"ok.py"', grammar)
        self.assertNotIn("passwd", grammar)
        self.assertNotIn("abs.py", grammar)
        with self.assertRaises(ValueError):
            build_sr_grammar(["../x.py"])
        with self.assertRaises(ValueError):
            build_sr_grammar([])

    def test_quotes_and_backslashes_in_paths_are_escaped(self):
        grammar = build_sr_grammar(['we"ird.py'])
        self.assertIn('"we\\"ird.py"', grammar)
        self.assertTrue(accepts(block('we"ird.py', "", "X = 1\n"), ['we"ird.py']))

    def test_flag_is_on_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_SR_GRAMMAR, None)
            self.assertTrue(sr_grammar_enabled())
        for value in ("0", "false", "off", " NO "):
            with patch.dict(os.environ, {ENV_SR_GRAMMAR: value}):
                self.assertFalse(sr_grammar_enabled())


class GrammarAcceptanceTests(unittest.TestCase):
    def test_accepts_a_valid_two_block_answer_the_parser_also_accepts(self):
        self.assertTrue(accepts(VALID_TWO_BLOCKS))
        self.assertEqual([b.path for b in parse_edit_blocks(VALID_TWO_BLOCKS)], ALLOWED)

    def test_accepts_blocks_without_a_blank_line_and_short_marker_runs_in_bodies(self):
        body = "x = 1\n\n<< not a marker\n== also fine\n>>>>>>x\n"
        answer = block("pkg/value.py", body, "x = 2\n") + block("pkg/value.py", "", "")
        self.assertTrue(accepts(answer))

    def test_rejects_prose_before_the_first_block(self):
        self.assertFalse(accepts("Here is the change:\n" + VALID_TWO_BLOCKS))

    def test_rejects_prose_after_the_last_block(self):
        self.assertFalse(accepts(VALID_TWO_BLOCKS + "Done.\n"))

    def test_rejects_a_code_fence(self):
        self.assertFalse(accepts("```\n" + VALID_TWO_BLOCKS + "```\n"))
        self.assertFalse(accepts(block("pkg/value.py", "```python\n", "x\n").replace(
            "pkg/value.py\n", "```text\npkg/value.py\n", 1)))

    def test_rejects_a_path_that_is_not_allowed(self):
        self.assertFalse(accepts(block("modules/other.py", "", "X = 1\n")))

    def test_rejects_a_missing_replace_marker(self):
        broken = VALID_TWO_BLOCKS.rsplit(">>>>>>> REPLACE\n", 1)[0]
        self.assertFalse(accepts(broken))

    def test_rejects_a_marker_line_inside_a_body(self):
        self.assertFalse(accepts(block("pkg/value.py", "a\n=======\nb\n", "c\n")))
        self.assertFalse(accepts(block("pkg/value.py", "<<<<<<< SEARCH\n", "c\n")))
        self.assertFalse(accepts(block("pkg/value.py", "a\n", ">>>>>>>>\n")))

    def test_rejects_an_empty_answer(self):
        self.assertFalse(accepts(""))


def _find_validator():
    """llama.cpp's grammar validator: ``NEXUS_GBNF_VALIDATOR``, on PATH, or a llama.cpp build."""
    configured = os.environ.get("NEXUS_GBNF_VALIDATOR")
    if configured and os.access(configured, os.X_OK):
        return configured
    for name in ("llama-gbnf-validator", "gbnf-validator", "test-gbnf-validator"):
        if shutil.which(name):
            return shutil.which(name)
    built = Path("/opt/llama.cpp/build/bin/test-gbnf-validator")
    return str(built) if os.access(built, os.X_OK) else None


GBNF_VALIDATOR = _find_validator()


@unittest.skipUnless(GBNF_VALIDATOR, "llama.cpp gbnf-validator is not installed")
class LlamaCppValidatorTests(unittest.TestCase):
    def validate(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            grammar, sample = Path(tmp, "sr.gbnf"), Path(tmp, "answer.txt")
            grammar.write_text(build_sr_grammar(ALLOWED))
            sample.write_text(text)
            run = subprocess.run([GBNF_VALIDATOR, str(grammar), str(sample)],
                                 capture_output=True, text=True, timeout=60)
        output = run.stdout + run.stderr
        self.assertNotIn("Failed to parse grammar", output)
        # The validator exits 0 either way; its verdict is in the output.
        self.assertIn("according to the grammar", output)
        return run.returncode == 0 and "is valid according to the grammar" in output

    def test_llama_cpp_agrees(self):
        self.assertTrue(self.validate(VALID_TWO_BLOCKS))
        self.assertFalse(self.validate("Here is the change:\n" + VALID_TWO_BLOCKS))
        self.assertFalse(self.validate("```\n" + VALID_TWO_BLOCKS + "```\n"))
        self.assertFalse(self.validate(block("modules/other.py", "", "X = 1\n")))
        self.assertFalse(self.validate(VALID_TWO_BLOCKS.rsplit(">>>>>>> REPLACE\n", 1)[0]))


# ---- the client --------------------------------------------------------------

def _completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class ClientTests(unittest.TestCase):
    def payloads(self, **complete_kwargs):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_completion("ok"))

        client = LlamaCppClient(base_url="http://fake/v1", stream=False,
                                transport=httpx.MockTransport(handler))
        self.assertEqual(asyncio.run(client.complete("hi", **complete_kwargs)), "ok")
        return seen

    def test_grammar_is_sent_as_the_grammar_field(self):
        [payload] = self.payloads(grammar="root ::= \"x\"")
        self.assertEqual(payload["grammar"], "root ::= \"x\"")

    def test_no_grammar_field_without_a_grammar(self):
        [payload] = self.payloads()
        self.assertNotIn("grammar", payload)

    def test_chat_completion_takes_grammar(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_completion("ok"))

        client = LlamaCppClient(base_url="http://fake/v1", stream=False,
                                transport=httpx.MockTransport(handler))
        asyncio.run(client.chat_completion([{"role": "user", "content": "x"}], grammar="g"))
        self.assertEqual(seen[0]["grammar"], "g")


# ---- the worker --------------------------------------------------------------

class RecordingLogger:
    def __init__(self):
        self.events = []

    def _record(self, _phase, event, _message="", **fields):
        self.events.append((event, fields))

    info = warning = error = _record

    def fields(self, event):
        return [fields for name, fields in self.events if name == event]


class RecordingClient:
    model = "fake"
    timeout = 300.0

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def context_size(self):
        return 8192

    async def count_tokens(self, text):
        return max(1, len(text) // 4)

    async def complete(self, prompt, *args, **kwargs):
        self.calls.append(kwargs)
        return self.answer


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, "repo")
        self.repo.mkdir()
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "value.py").write_text("VALUE = 1\n")
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True)
        self.rates = str(Path(self.temp.name, "rates.json"))

    def run_worker(self, client, env, task="Change pkg/value.py so that VALUE is 2."):
        log = RecordingLogger()
        worker = LocalCodingWorker(client=client, max_tokens=512, rates_path=self.rates)
        with patch.dict(os.environ, {**SR_ENV, **env}):
            result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                                timeout_seconds=60, experiment_logger=log)
        return worker, result, log


class WorkerGrammarTests(GitRepoCase):
    ANSWER = block("pkg/value.py", "VALUE = 1\n", "VALUE = 2\n")

    def test_sr_mode_with_flag_on_sends_the_grammar_and_records_it(self):
        client = RecordingClient(self.ANSWER)
        worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "1"})
        self.assertTrue(result.success, result.error)
        [call] = client.calls
        self.assertIn('"pkg/value.py"', call["grammar"])
        self.assertTrue(worker.sr_grammar)
        self.assertEqual([True], [f["sr_grammar"] for f in log.fields("local_patch_round")])
        self.assertEqual([True], [f["sr_grammar"] for f in log.fields("local_model_call")
                                  if f.get("call_type") == "patch"])
        self.assertEqual([True], [f["sr_grammar"] for f in log.fields("local_run_metrics")])

    def test_default_is_on(self):
        client = RecordingClient(self.ANSWER)
        with patch.dict(os.environ, {}):
            os.environ.pop(ENV_SR_GRAMMAR, None)
            self.run_worker(client, {})
        self.assertIn("grammar", client.calls[0])

    def test_flag_off_leaves_the_grammar_out(self):
        client = RecordingClient(self.ANSWER)
        worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "0"})
        self.assertTrue(result.success, result.error)
        self.assertNotIn("grammar", client.calls[0])
        self.assertFalse(worker.sr_grammar)
        self.assertEqual([False], [f["sr_grammar"] for f in log.fields("local_patch_round")])
        self.assertEqual([False], [f["sr_grammar"] for f in log.fields("local_run_metrics")])

    def test_diff_mode_never_sends_the_grammar(self):
        client = RecordingClient("not a diff")
        worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "1", "LOCAL_EDIT_FORMAT": "diff"})
        self.assertFalse(result.success)
        self.assertTrue(client.calls)
        self.assertTrue(all("grammar" not in call for call in client.calls))
        self.assertFalse(worker.sr_grammar)

    def test_client_without_a_grammar_option_is_not_sent_one(self):
        class OldClient(RecordingClient):
            async def complete(self, prompt, system="", temperature=0.0, max_tokens=1024, seed=None):
                self.calls.append({"seed": seed})
                return self.answer

        client = OldClient(self.ANSWER)
        worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "1"})
        self.assertTrue(result.success, result.error)
        self.assertFalse(worker.sr_grammar)
        self.assertEqual([False], [f["sr_grammar"] for f in log.fields("local_patch_round")])

    def test_a_grammar_that_cannot_be_built_does_not_fail_the_run(self):
        client = RecordingClient(self.ANSWER)
        with patch("modules.local_coding_worker.patch_grammar", side_effect=RuntimeError("boom")):
            worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "1"})
        self.assertTrue(result.success, result.error)
        self.assertNotIn("grammar", client.calls[0])
        self.assertFalse(worker.sr_grammar)

    def test_second_run_resets_the_flag(self):
        client = RecordingClient(self.ANSWER)
        worker = LocalCodingWorker(client=client, max_tokens=512, rates_path=self.rates)
        with patch.dict(os.environ, {**SR_ENV, ENV_SR_GRAMMAR: "1"}):
            worker.run(task="Change pkg/value.py so that VALUE is 2.", working_dir=str(self.repo),
                       sandbox="workspace-write", timeout_seconds=60)
        self.assertTrue(worker.sr_grammar)
        with patch.dict(os.environ, {**SR_ENV, ENV_SR_GRAMMAR: "0"}):
            worker.run(task="Change pkg/value.py so that VALUE is 3.", working_dir=str(self.repo),
                       sandbox="workspace-write", timeout_seconds=60)
        self.assertFalse(worker.sr_grammar)


class FakeServerTests(GitRepoCase):
    def test_grammar_constrained_answer_applies(self):
        """The fake server answers with a valid block only when it gets a grammar."""
        requests = []

        def handler(request):
            if not request.url.path.endswith("/chat/completions"):
                return httpx.Response(404)
            payload = json.loads(request.content)
            requests.append(payload)
            grammar = payload.get("grammar")
            content = (block("pkg/value.py", "VALUE = 1\n", "VALUE = 2\n") if grammar
                       else "Sure! Change VALUE to 2.")
            if grammar:
                # What the server sampled really matches the grammar it was given.
                assert gbnf_to_regex(grammar).fullmatch(content), content
            return httpx.Response(200, json=_completion(content))

        client = LlamaCppClient(base_url="http://fake/v1", stream=False,
                                transport=httpx.MockTransport(handler))
        worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "1"})
        self.assertTrue(result.success, result.error)
        self.assertEqual((self.repo / "pkg" / "value.py").read_text(), "VALUE = 2\n")
        self.assertEqual(len(requests), 1)
        self.assertIn('"pkg/value.py"', requests[0]["grammar"])
        self.assertEqual(["applied"], [f["outcome"] for f in log.fields("local_patch_round")])

    def test_without_the_grammar_the_same_server_wastes_the_rounds(self):
        def handler(request):
            if not request.url.path.endswith("/chat/completions"):
                return httpx.Response(404)
            payload = json.loads(request.content)
            content = (block("pkg/value.py", "VALUE = 1\n", "VALUE = 2\n") if payload.get("grammar")
                       else "Sure! Change VALUE to 2.")
            return httpx.Response(200, json=_completion(content))

        client = LlamaCppClient(base_url="http://fake/v1", stream=False,
                                transport=httpx.MockTransport(handler))
        _worker, result, log = self.run_worker(client, {ENV_SR_GRAMMAR: "0"})
        self.assertFalse(result.success)
        self.assertEqual({"no_diff"}, {f["outcome"] for f in log.fields("local_patch_round")})


class GrammarPathTests(GitRepoCase):
    def plan(self, essential, named):
        return PromptPlan(prompt="", n_ctx=4096, n_ctx_source="test", budget_tokens=3000,
                          prompt_tokens=0, token_source="estimate", named_files=named,
                          essential=essential)

    def test_one_shot_allows_prompt_files_and_files_the_goal_creates(self):
        plan = self.plan("Change pkg/value.py and add tests/test_value.py; see pkg too.\n"
                         "NEW FILE: docs/new.md (use an empty SEARCH)", ["pkg/value.py"])
        paths = sr_grammar_paths(plan, str(self.repo))
        self.assertEqual(paths[0], "pkg/value.py")
        self.assertIn("tests/test_value.py", paths)
        self.assertIn("docs/new.md", paths)
        self.assertNotIn("pkg", paths)  # an existing directory

    def test_subtask_uses_only_its_declared_files(self):
        plan = self.plan("Change pkg/value.py and other.py", ["pkg/value.py"])
        self.assertEqual(sr_grammar_paths(plan, str(self.repo), {"pkg/new.py"}), ["pkg/new.py"])

    def test_paths_outside_the_tree_are_dropped(self):
        plan = self.plan("x", ["../outside.py", "pkg/value.py"])
        self.assertEqual(sr_grammar_paths(plan, str(self.repo)), ["pkg/value.py"])

    def test_patch_grammar_is_none_in_diff_mode_or_with_flag_off_or_no_paths(self):
        plan = self.plan("Change pkg/value.py", ["pkg/value.py"])
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "1"}):
            self.assertIsNotNone(patch_grammar(plan, str(self.repo)))
            self.assertIsNone(patch_grammar(self.plan("no paths here", []), str(self.repo)))
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "0"}):
            self.assertIsNone(patch_grammar(plan, str(self.repo)))
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "diff", ENV_SR_GRAMMAR: "1"}):
            self.assertIsNone(patch_grammar(plan, str(self.repo)))


# ---- replay records ----------------------------------------------------------

class GrammarReplayWorker(ReplayWorker):
    def __init__(self, sr_grammar):
        super().__init__(None)
        self.sr_grammar = sr_grammar

    def run(self, *, experiment_logger=None, **kwargs):
        experiment_logger.info("local_shadow_replay", "local_run_metrics", "metrics",
                               sr_grammar=self.sr_grammar, samples_setting=1)
        return super().run(experiment_logger=experiment_logger, **kwargs)


class ShadowReplayRecordTests(unittest.TestCase):
    def test_sr_grammar_is_stored_on_the_replay_record(self):
        fx = ReplayFixture(self)
        row = fx.replay(GrammarReplayWorker(True))
        self.assertIs(row["sr_grammar"], True)
        self.assertEqual(fx.rows()[0]["sr_grammar"], 1)
        row = fx.replay(GrammarReplayWorker(False), variant="off")
        self.assertIs(row["sr_grammar"], False)

    def test_variant_names_the_grammar_arm(self):
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "1"}):
            os.environ.pop(lsr.ENV_VARIANT, None)
            self.assertEqual(lsr.prompt_variant(), "baseline+sr+gbnf")
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace", ENV_SR_GRAMMAR: "0"}):
            os.environ.pop(lsr.ENV_VARIANT, None)
            self.assertEqual(lsr.prompt_variant(), "baseline+sr")
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "diff", ENV_SR_GRAMMAR: "1"}):
            os.environ.pop(lsr.ENV_VARIANT, None)
            self.assertEqual(lsr.prompt_variant(), "baseline")


class GrammarHunkWorker(HunkWorker):
    def __init__(self, files, *, sr_grammar, outcomes):
        super().__init__(files)
        self.sr_grammar, self.outcomes = sr_grammar, outcomes

    def run(self, *, experiment_logger=None, **kwargs):
        for number, outcome in enumerate(self.outcomes, start=1):
            experiment_logger.info("p", "local_patch_round", outcome, round=number, outcome=outcome,
                                   sr_grammar=self.sr_grammar)
            experiment_logger.info("p", "local_model_call", "call", call_type="patch",
                                   timings={"predicted_n": 30, "predicted_ms": 10000.0})
        return super().run(experiment_logger=experiment_logger, **kwargs)


class HunkReplayRecordTests(unittest.TestCase):
    def test_unit_rows_record_grammar_format_rounds_and_tok_per_second(self):
        fx = HunkFixture(self)
        worker = GrammarHunkWorker({SHAPES: MERGED_SHAPES}, sr_grammar=True,
                                   outcomes=["edit_format", "no_diff", "applied"])
        fx.replay(worker, variant="g1")
        rows = fx.units()
        self.assertTrue(rows)
        for row in rows.values():
            self.assertEqual(row["sr_grammar"], 1)
            self.assertEqual(row["format_rounds"], 2)
            self.assertAlmostEqual(row["generation_tokens_per_second"], 3.0)
        fx.replay(GrammarHunkWorker({SHAPES: MERGED_SHAPES}, sr_grammar=False, outcomes=["applied"]),
                  variant="g0")
        summary = {s["prompt_variant"]: s for s in lsh.unit_variant_summary(db_path=fx.db_path)}
        self.assertEqual(summary["g1"]["format_rounds"], 2 * len(rows))
        self.assertEqual(summary["g1"]["sr_grammar_units"], len(rows))
        self.assertAlmostEqual(summary["g1"]["format_round_rate"], 0.5, places=3)
        self.assertEqual(summary["g0"]["format_rounds"], 0)
        self.assertEqual(summary["g0"]["sr_grammar_units"], 0)
        self.assertEqual(summary["g0"]["format_round_rate"], 0.0)

    def test_worker_without_the_field_stores_null(self):
        fx = HunkFixture(self)
        fx.replay(HunkWorker({SHAPES: MERGED_SHAPES}), variant="old")
        for row in fx.units().values():
            self.assertIsNone(row["sr_grammar"])
            self.assertEqual(row["format_rounds"], 0)
            self.assertIsNone(row["generation_tokens_per_second"])


if __name__ == "__main__":
    unittest.main()
