"""#679: tolerate guessed SEARCH text only when the target has no content."""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.local_coding_worker import LocalCodingWorker, PromptPlan, Subtask, apply_and_check
from modules.local_edit_format import EditFormatError, edits_to_diff


class LenientCreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def block(path: str, search: str, replacement: str) -> str:
        return f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"

    def test_missing_path_with_nonempty_search_creates_new_file(self):
        result = edits_to_diff(self.block("new.py", "guessed old text\n", "VALUE = 1\n"), str(self.repo))
        self.assertEqual(1, result.lenient_creates)
        self.assertIn("new file mode", result)
        apply_and_check(result, str(self.repo), 10)
        self.assertEqual("VALUE = 1\n", (self.repo / "new.py").read_text())

    def test_existing_empty_file_with_nonempty_search_uses_replacement(self):
        (self.repo / "empty.py").touch()
        result = edits_to_diff(self.block("empty.py", "guessed\n", "VALUE = 2\n"), str(self.repo))
        self.assertEqual(1, result.lenient_creates)
        apply_and_check(result, str(self.repo), 10)
        self.assertEqual("VALUE = 2\n", (self.repo / "empty.py").read_text())

    def test_nonempty_file_with_missing_search_remains_strict(self):
        (self.repo / "value.txt").write_text("actual\n")
        with self.assertRaisesRegex(EditFormatError, "SEARCH was not found"):
            edits_to_diff(self.block("value.txt", "guessed\n", "new\n"), str(self.repo))

    def test_empty_search_on_existing_nonempty_file_remains_error(self):
        (self.repo / "value.txt").write_text("actual\n")
        with self.assertRaisesRegex(EditFormatError, "empty SEARCH but the path already exists"):
            edits_to_diff(self.block("value.txt", "", "new\n"), str(self.repo))

    def test_lenient_create_count_covers_multiple_blocks(self):
        answer = (self.block("one.py", "guess one\n", "ONE = 1\n")
                  + self.block("two.py", "guess two\n", "TWO = 2\n"))
        self.assertEqual(2, edits_to_diff(answer, str(self.repo)).lenient_creates)

    def test_missing_subtask_target_is_named_as_new_file(self):
        class Client:
            timeout = None

        worker = LocalCodingWorker(client=Client(), max_tokens=1024)
        parent = PromptPlan(
            prompt="", n_ctx=4096, n_ctx_source="test", budget_tokens=3000,
            prompt_tokens=0, token_source="estimate", essential="overall task",
        )
        prompt = asyncio.run(worker._subtask_plan(
            parent, Subtask("create it", ("modules/new_module.py",)), 1, 2, [], str(self.repo)
        )).prompt
        self.assertIn("NEW FILE: modules/new_module.py (use an empty SEARCH)", prompt)


if __name__ == "__main__":
    unittest.main()
