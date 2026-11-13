import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.local_coding_worker import LocalCodingWorker, _response_patch, apply_and_check
from modules.local_edit_format import EditFormatError, edits_to_diff


class LocalEditFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)

    @staticmethod
    def block(path, search, replacement):
        return f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"

    def test_one_block_edits_function_and_diff_applies_unchanged(self):
        self.commit({"app.py": "def value():\n    return 1\n"})
        answer = self.block("app.py", "def value():\n    return 1\n", "def value():\n    return 2\n")
        diff = edits_to_diff(answer, str(self.repo))
        self.assertEqual((self.repo / "app.py").read_text(), "def value():\n    return 1\n")
        apply_and_check(diff, str(self.repo), 10)
        self.assertEqual((self.repo / "app.py").read_text(), "def value():\n    return 2\n")

    def test_two_blocks_in_two_files(self):
        self.commit({"a.txt": "one\n", "b.txt": "two\n"})
        answer = self.block("a.txt", "one\n", "ONE\n") + self.block("b.txt", "two\n", "TWO\n")
        apply_and_check(edits_to_diff(answer, str(self.repo)), str(self.repo), 10)
        self.assertEqual((self.repo / "a.txt").read_text(), "ONE\n")
        self.assertEqual((self.repo / "b.txt").read_text(), "TWO\n")

    def test_whitespace_normalised_match(self):
        self.commit({"app.py": "def f():\n\treturn   1\n"})
        answer = self.block("app.py", "def f():\n    return 1\n", "def f():\n    return 2\n")
        apply_and_check(edits_to_diff(answer, str(self.repo)), str(self.repo), 10)
        self.assertIn("return 2", (self.repo / "app.py").read_text())

    def test_ambiguous_and_missing_include_closest_lines(self):
        self.commit({"a.txt": "same\nsame\nnearby value\n"})
        for search in ("same\n", "nearby other\n"):
            with self.subTest(search=search), self.assertRaises(EditFormatError) as caught:
                edits_to_diff(self.block("a.txt", search, "new\n"), str(self.repo))
            self.assertIn("edit block 1 (a.txt)", str(caught.exception))
            self.assertIn("closest file lines", str(caught.exception))

    def test_empty_search_creates_new_file(self):
        self.commit({"a.txt": "old\n"})
        diff = edits_to_diff(self.block("new.py", "", "VALUE = 3\n"), str(self.repo))
        apply_and_check(diff, str(self.repo), 10)
        self.assertEqual((self.repo / "new.py").read_text(), "VALUE = 3\n")

    def test_outside_subtask_is_rejected_before_diff(self):
        self.commit({"a.txt": "old\n", "b.txt": "old\n"})
        with self.assertRaises(EditFormatError) as caught:
            edits_to_diff(self.block("b.txt", "old\n", "new\n"), str(self.repo),
                          allowed_paths={"a.txt"})
        self.assertIn("outside this subtask", str(caught.exception))

    def test_unified_diff_with_wrong_hunk_counts_uses_recount(self):
        self.commit({"a.txt": "old\n"})
        corrupt = "--- a/a.txt\n+++ b/a.txt\n@@ -1,99 +1,99 @@\n-old\n+new\n"
        apply_and_check(corrupt, str(self.repo), 10)
        self.assertEqual((self.repo / "a.txt").read_text(), "new\n")

    def test_diff_answer_is_fallback_in_search_replace_mode(self):
        self.commit({"a.txt": "old\n"})
        diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace"}):
            self.assertEqual(_response_patch(diff, str(self.repo), 10, None), diff)

    def test_fake_client_search_replace_answer_runs_through_worker(self):
        self.commit({"app.py": "VALUE = 1\n"})
        answer = self.block("app.py", "VALUE = 1\n", "VALUE = 2\n")

        class Client:
            model = "fake"

            async def context_size(self):
                return 4096

            async def count_tokens(self, text):
                return max(1, len(text) // 4)

            async def complete(self, *args, **kwargs):
                return answer

        with patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": "search_replace"}):
            result = LocalCodingWorker(client=Client(), max_tokens=512).run(
                task="Change app.py VALUE to 2", working_dir=str(self.repo),
                sandbox="workspace-write", timeout_seconds=30,
            )
        self.assertTrue(result.success, result.error)
        self.assertEqual((self.repo / "app.py").read_text(), "VALUE = 2\n")


if __name__ == "__main__":
    unittest.main()
