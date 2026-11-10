"""#634: local_test_first keeps only generated tests that fail on the base tree."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from modules import local_test_first
from modules.llama_cpp_client import LlamaCppClient
from modules.local_test_first import _prune, _sections, generate_failing_tests

ISSUE = """## Change
`value.py`

## Test
Add a regression test for value().
"""

# The format of real Nexus issues: bold headings, inline text after the heading.
BOLD_ISSUE = """GitHub Issue #999: value() returns the wrong number

**Problem**
value() returns 1, callers expect 2.

**Change** `value.py`: value() returns 2.

**Test** (`tests/test_value.py`, no network)
value() returns 2.

---
Part 1 of 2 of #998.
"""

COLON_ISSUE = "Problem: wrong number.\nChange: `value.py` returns 2.\nTest: value() returns 2.\n"

PASSES = " def test_value(self): self.assertEqual(value.value(), 1)\n"
FAILS = " def test_value(self): self.assertEqual(value.value(), 2)\n"


def _source(body, name="ValueTests"):
    return f"import unittest\nimport value\nclass {name}(unittest.TestCase):\n" + body


class FakeClient:
    """Fake local model: scripted answers (file content, raw string or exception)."""

    def __init__(self, *answers, path="tests/test_value.py"):
        self.answers = list(answers)
        self.path = path
        self.calls = []

    def _next(self, prompt, kwargs):
        self.calls.append(prompt)
        self.kwargs = kwargs
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):  # (path, content)
            return json.dumps({"path": answer[0], "content": answer[1]})
        return json.dumps({"path": self.path, "content": answer})

    async def complete(self, prompt, **kwargs):
        return self._next(prompt, kwargs)


class SyncClient(FakeClient):
    def complete(self, prompt, **kwargs):  # type: ignore[override]
        return self._next(prompt, kwargs)


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "tests").mkdir()
        (self.root / "value.py").write_text("def value(): return 1\n", encoding="utf-8")
        (self.root / "tests" / "test_existing.py").write_text("# keep me\n", encoding="utf-8")
        self.before = self._snapshot()

    def tearDown(self):
        self.temp.cleanup()

    def _snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assertTreeUnchanged(self):
        self.assertEqual(self._snapshot(), self.before)
        self.assertFalse((self.root / "tests" / "__pycache__").exists())

    def run_generate(self, client, issue=ISSUE):
        result = generate_failing_tests(issue, str(self.root), client)
        self.assertTreeUnchanged()
        return result

    def assertValidFile(self, ids, content, filename="test_value.py"):
        """The returned content, written as the chosen file, collects exactly *ids*, all failing."""
        target = self.root / "tests" / filename
        target.write_text(content, encoding="utf-8")
        try:
            compile(content, filename, "exec")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", *ids], cwd=self.root,
                                  capture_output=True, text=True, timeout=60, env=env)
            self.assertIn(f"Ran {len(ids)} test", done.stderr)
            self.assertIn(f"FAILED (failures={len(ids)})", done.stderr)
            module = "tests." + filename[:-3]
            listed = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import sys,unittest\n"
                 "def flat(s):\n"
                 " for x in s:\n"
                 "  yield from flat(x) if isinstance(x, unittest.TestSuite) else [x]\n"
                 f"print(sorted(t.id() for t in flat(unittest.defaultTestLoader.loadTestsFromName({module!r}))))"],
                cwd=self.root, capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(listed.stdout.strip(), str(sorted(ids)))
        finally:
            target.unlink()


class ContractTests(_Base):
    def test_passing_test_is_dropped(self):
        client = FakeClient(_source(PASSES), _source(PASSES), _source(PASSES))
        self.assertEqual(self.run_generate(client), ([], ""))
        self.assertEqual(len(client.calls), 3)
        self.assertIn("passes on the base tree", client.calls[1])

    def test_passing_test_is_dropped_and_failing_correction_kept(self):
        client = FakeClient(_source(PASSES), _source(FAILS))
        ids, content = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertEqual(content, _source(FAILS))
        self.assertIn("Correction (round 2 of 3)", client.calls[1])
        self.assertIn(_source(PASSES), client.calls[1])  # previous file fed back

    def test_syntax_error_is_retried_then_dropped(self):
        client = FakeClient("not python !!!", "still ???", "class broken(")
        self.assertEqual(self.run_generate(client), ([], ""))
        self.assertEqual(len(client.calls), 3)
        self.assertIn("does not compile", client.calls[1])
        self.assertIn("Correction (round 3 of 3)", client.calls[2])
        self.assertNotIn("not python !!!", client.calls[2])  # only the last round's feedback

    def test_syntax_error_then_failing_test_is_kept(self):
        client = FakeClient("class broken(", _source(FAILS))
        ids, content = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertEqual(len(client.calls), 2)

    def test_failing_test_is_kept(self):
        client = FakeClient(_source(FAILS))
        ids, content = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertEqual(content, _source(FAILS))
        self.assertEqual(len(client.calls), 1)
        self.assertValidFile(ids, content)

    def test_only_failing_methods_are_returned(self):
        source = _source(" def test_passes(self): self.assertEqual(value.value(), 1)\n"
                         " def test_fails(self): self.assertEqual(value.value(), 2)\n")
        ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_fails"])
        self.assertNotIn("test_passes", content)
        self.assertValidFile(ids, content)

    def test_missing_test_section_returns_empty_without_calling(self):
        client = FakeClient(AssertionError("must not call"))
        for issue in ("## Change\n`value.py`", "", "Testing the value is hard.\nThe Test section is missing.\n"):
            self.assertEqual(self.run_generate(client, issue), ([], ""))
        self.assertEqual(client.calls, [])

    def test_unreachable_server_returns_empty_without_retrying(self):
        down = FakeClient(ConnectionError("down"), _source(FAILS))
        self.assertEqual(self.run_generate(down), ([], ""))
        self.assertEqual(len(down.calls), 1)

    def test_real_client_against_closed_port_returns_empty(self):
        client = LlamaCppClient(base_url="http://127.0.0.1:9/v1", timeout=5)
        self.assertEqual(self.run_generate(client), ([], ""))

    def test_sync_client_and_call_from_running_loop(self):
        ids, _ = self.run_generate(SyncClient(_source(FAILS)))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])

        async def inside_loop():
            return generate_failing_tests(ISSUE, str(self.root), FakeClient(_source(FAILS)))

        self.assertEqual(asyncio.run(inside_loop())[0], ["tests.test_value.ValueTests.test_value"])
        self.assertTreeUnchanged()


class IssueSectionTests(_Base):
    """Blocker 1 of d70791cf: real issues use bold headings with inline text."""

    def test_bold_headings_with_inline_text(self):
        sections = _sections(BOLD_ISSUE)
        self.assertEqual(sections["problem"], "value() returns 1, callers expect 2.")
        self.assertEqual(sections["change"], "`value.py`: value() returns 2.")
        self.assertEqual(sections["test"], "(`tests/test_value.py`, no network)\nvalue() returns 2.")

    def test_markdown_colon_and_bold_colon_headings(self):
        self.assertEqual(_sections(ISSUE)["test"], "Add a regression test for value().")
        self.assertEqual(_sections(COLON_ISSUE)["change"], "`value.py` returns 2.")
        self.assertEqual(_sections(COLON_ISSUE)["test"], "value() returns 2.")
        self.assertEqual(_sections("**Test:** inline only\n")["test"], "inline only")
        self.assertEqual(_sections("### Tests\n- one\n")["test"], "- one")

    def test_handover_is_not_part_of_the_issue(self):
        text = "## Problem\nx\n\nWhat Nexus already found (handover):\n**Test** from the handover\n"
        self.assertNotIn("test", _sections(text))

    def test_bold_issue_generates_with_inline_test_text_and_change_file(self):
        client = FakeClient(_source(FAILS), path="tests/test_value_634.py")
        ids, content = self.run_generate(client, BOLD_ISSUE)
        self.assertEqual(ids, ["tests.test_value_634.ValueTests.test_value"])
        prompt = client.calls[0]
        self.assertIn("(`tests/test_value.py`, no network)", prompt)
        self.assertIn("value() returns 2.", prompt)
        self.assertIn("--- value.py ---\ndef value(): return 1", prompt)
        self.assertNotIn("Part 1 of 2", prompt)

    def test_colon_issue_generates(self):
        ids, _ = self.run_generate(FakeClient(_source(FAILS)), COLON_ISSUE)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])


class PruneTests(_Base):
    """Blocker 2 of d70791cf: pruning must never leave an empty class body."""

    def test_class_with_only_passing_tests_is_removed(self):
        source = (_source(" def test_passes(self): self.assertEqual(value.value(), 1)\n", "A")
                  + "class B(unittest.TestCase):\n"
                    "    def test_fails(self):\n        self.assertEqual(value.value(), 2)\n")
        ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.B.test_fails"])
        self.assertNotIn("class A", content)
        compile(content, "test_value.py", "exec")
        self.assertValidFile(ids, content)

    def test_emptied_base_class_keeps_pass(self):
        source = ("import unittest\nimport value\n"
                  "class Base(unittest.TestCase):\n"
                  "    @unittest.skipIf(False, 'never')\n"
                  "    def test_passes(self):\n        self.assertEqual(value.value(), 1)\n"
                  "class Child(Base):\n"
                  "    def test_fails(self):\n        self.assertEqual(value.value(), 2)\n")
        ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.Child.test_fails"])
        self.assertIn("class Base(unittest.TestCase):\n    pass\n", content)
        self.assertNotIn("skipIf", content)
        self.assertValidFile(ids, content)

    def test_prune_keeps_helpers_docstrings_and_mixins(self):
        source = ("import unittest\n"
                  "class Mixin:\n    def helper(self):\n        return 1\n"
                  "class A(Mixin, unittest.TestCase):\n    '''Doc.'''\n    def test_x(self):\n        pass\n"
                  "class B(unittest.TestCase):\n    def setUp(self):\n        pass\n"
                  "    def test_y(self):\n        pass\n    def test_z(self):\n        pass\n")
        pruned = _prune(source, {("B", "test_z")})
        compile(pruned, "x.py", "exec")
        self.assertIn("class Mixin", pruned)
        self.assertNotIn("class A", pruned)
        self.assertIn("def setUp", pruned)
        self.assertNotIn("test_y", pruned)
        self.assertIn("test_z", pruned)

    def test_pruned_file_that_does_not_validate_is_rejected(self):
        # The pruned file is checked again; here pruning is forced to yield a file whose
        # only test passes on base, so it must be rejected and the next round used.
        order_dependent = _source(
            " state = []\n"
            " def test_a_passes(self): ValueTests.state.append(1)\n"
            " def test_b_fails(self): self.assertEqual(ValueTests.state, [1])\n")
        client = FakeClient(order_dependent, _source(FAILS))
        with mock.patch.object(local_test_first, "_check", wraps=local_test_first._check) as check, \
                mock.patch.object(local_test_first, "_prune", return_value=_source(PASSES)):
            ids, content = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertEqual(content, _source(FAILS))
        self.assertEqual(check.call_count, 3)  # candidate, pruned re-check, round 2
        self.assertIn("after removing the tests", client.calls[1])


class RobustnessTests(_Base):
    def test_error_instead_of_assertion_is_dropped(self):
        source = _source(" def test_error(self): value.missing()\n"
                         " def test_fails(self): self.assertEqual(value.value(), 2)\n")
        ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_fails"])
        self.assertNotIn("test_error", content)

    def test_crashing_test_process_is_dropped(self):
        source = "import os\n" + _source(" def test_crash(self): os._exit(3)\n" + FAILS)
        ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertNotIn("test_crash", content)

    def test_hanging_test_times_out_and_is_dropped(self):
        source = _source(" def test_hangs(self):\n  while True: pass\n" + FAILS)
        with mock.patch.object(local_test_first, "_TIMEOUT", 2.0):
            ids, content = self.run_generate(FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertNotIn("test_hangs", content)

    def test_printed_unittest_summary_cannot_spoof_a_failure(self):
        source = _source(" def test_spoof(self): print('FAILED (failures=1)')\n")
        client = FakeClient(source, source, source)
        self.assertEqual(self.run_generate(client), ([], ""))

    def test_import_error_and_foreign_testcases_are_rejected(self):
        broken = "import does_not_exist\n" + _source(FAILS)
        foreign = "from tests.test_other import OtherTests\n" + _source(FAILS)
        (self.root / "tests" / "test_other.py").write_text(
            "import unittest\nclass OtherTests(unittest.TestCase):\n def test_o(self): self.fail()\n")
        self.before = self._snapshot()
        client = FakeClient(broken, foreign, _source(FAILS))
        ids, _ = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertIn("cannot be imported", client.calls[1])
        self.assertIn("other modules", client.calls[2])

    def test_existing_file_and_bad_paths_are_never_used(self):
        client = FakeClient(("tests/test_existing.py", _source(FAILS)), ("../evil.py", _source(FAILS)),
                            ("tests/test_new.py", _source(FAILS)))
        ids, _ = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_new.ValueTests.test_value"])
        self.assertIn("already exists", client.calls[1])
        self.assertIn("must be a new tests/test_*.py", client.calls[2])
        self.assertEqual((self.root / "tests" / "test_existing.py").read_text(), "# keep me\n")

    def test_non_json_answer_is_retried(self):
        class Raw(FakeClient):
            async def complete(self, prompt, **kwargs):
                self.calls.append(prompt)
                return self.answers.pop(0)

        client = Raw("sorry, no", "```python\n" + _source(FAILS) + "```")
        ids, _ = self.run_generate(client)
        self.assertEqual(ids, ["tests.test_generated.ValueTests.test_value"])
        self.assertIn("not a JSON object", client.calls[1])

    def test_interrupt_propagates_and_cleans_up(self):
        client = FakeClient("class broken(", KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            generate_failing_tests(ISSUE, str(self.root), client)
        self.assertTreeUnchanged()

    def test_server_lost_halfway_returns_empty_and_cleans_up(self):
        client = FakeClient("class broken(", ConnectionError("gone"), _source(FAILS))
        self.assertEqual(self.run_generate(client), ([], ""))
        self.assertEqual(len(client.calls), 2)

    def test_unexpected_crash_inside_a_round_returns_empty(self):
        with mock.patch.object(local_test_first, "_collect", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_generate(FakeClient(_source(FAILS))), ([], ""))

    def test_running_twice_gives_the_same_result_and_leaves_nothing(self):
        first = self.run_generate(FakeClient(_source(FAILS)))
        second = self.run_generate(FakeClient(_source(FAILS)))
        self.assertEqual(first, second)
        self.assertEqual(first[0], ["tests.test_value.ValueTests.test_value"])

    def test_stale_candidates_are_removed_but_fresh_ones_kept(self):
        stale = self.root / "tests" / "_nexus_test_first_old.py"
        fresh = self.root / "tests" / "_nexus_test_first_running.py"
        stale.write_text("x = 1\n")
        fresh.write_text("x = 2\n")
        old = time.time() - 2 * local_test_first._STALE_SECONDS
        os.utime(stale, (old, old))
        generate_failing_tests(ISSUE, str(self.root), FakeClient(_source(FAILS)))
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_missing_tests_dir_is_created_and_removed(self):
        for item in (self.root / "tests").iterdir():
            item.unlink()
        (self.root / "tests").rmdir()
        ids, _ = generate_failing_tests(ISSUE, str(self.root), FakeClient(_source(FAILS)))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertFalse((self.root / "tests").exists())

    def test_missing_working_dir_returns_empty(self):
        missing = str(self.root / "nope")
        self.assertEqual(generate_failing_tests(ISSUE, missing, FakeClient(_source(FAILS))), ([], ""))

    def test_large_change_file_is_cut_in_prompt(self):
        (self.root / "value.py").write_text("def value(): return 1\n" + "#" * 50000 + "\n")
        self.before = self._snapshot()
        client = FakeClient(_source(FAILS))
        self.run_generate(client)
        self.assertIn("...[file cut]", client.calls[0])
        self.assertLess(len(client.calls[0]), 20000)


if __name__ == "__main__":
    unittest.main()
