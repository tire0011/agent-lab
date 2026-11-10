import tempfile
import unittest
from pathlib import Path

from modules.local_test_first import generate_failing_tests


ISSUE = """## Change
`value.py`

## Test
Add a regression test for value().
"""


class FakeClient:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = []

    async def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        content = self.contents.pop(0)
        if isinstance(content, Exception):
            raise content
        import json
        return json.dumps({"path": "tests/test_value.py", "content": content})


def test_source(body):
    return "import unittest\nimport value\nclass ValueTests(unittest.TestCase):\n" + body


class LocalTestFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "tests").mkdir()
        (self.root / "value.py").write_text("def value(): return 1\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_passing_test_is_dropped(self):
        client = FakeClient(test_source(" def test_value(self): self.assertEqual(value.value(), 1)\n"))
        ids, content = generate_failing_tests(ISSUE, str(self.root), client)
        self.assertEqual((ids, content), ([], ""))
        self.assertEqual(len(client.calls), 1)

    def test_syntax_error_is_retried_then_dropped(self):
        client = FakeClient("not python !!!", "still ???", "class broken(")
        self.assertEqual(generate_failing_tests(ISSUE, str(self.root), client), ([], ""))
        self.assertEqual(len(client.calls), 3)
        self.assertIn("does not compile", client.calls[1])

    def test_failing_test_is_kept(self):
        source = test_source(" def test_value(self): self.assertEqual(value.value(), 2)\n")
        ids, content = generate_failing_tests(ISSUE, str(self.root), FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_value"])
        self.assertEqual(content, source)

    def test_only_failing_methods_are_returned(self):
        source = test_source(
            " def test_passes(self): self.assertEqual(value.value(), 1)\n"
            " def test_fails(self): self.assertEqual(value.value(), 2)\n"
        )
        ids, content = generate_failing_tests(ISSUE, str(self.root), FakeClient(source))
        self.assertEqual(ids, ["tests.test_value.ValueTests.test_fails"])
        self.assertNotIn("test_passes", content)
        self.assertIn("test_fails", content)

    def test_missing_test_section_and_unreachable_server_are_empty(self):
        client = FakeClient(AssertionError("must not call"))
        self.assertEqual(generate_failing_tests("## Change\n`value.py`", str(self.root), client), ([], ""))
        self.assertEqual(client.calls, [])
        down = FakeClient(ConnectionError("down"))
        self.assertEqual(generate_failing_tests(ISSUE, str(self.root), down), ([], ""))


if __name__ == "__main__":
    unittest.main()
