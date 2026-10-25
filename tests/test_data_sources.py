"""Focused unit tests for the IDEAS.md read/write helpers in data_sources.py.

API-level behavior (auth, request validation, end-to-end reflection in
GET /ideas) is covered in tests/test_api.py; these tests target the
lower-level parsing/sanitization/insertion helpers directly.
"""

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

import data_sources


class SanitizeIdeaTextTests(unittest.TestCase):
    def test_rejects_empty_string(self):
        with self.assertRaises(data_sources.InvalidIdeaTextError):
            data_sources._sanitize_idea_text("")

    def test_rejects_whitespace_only(self):
        with self.assertRaises(data_sources.InvalidIdeaTextError):
            data_sources._sanitize_idea_text("   \n\t  ")

    def test_rejects_text_over_max_length(self):
        with self.assertRaises(data_sources.InvalidIdeaTextError):
            data_sources._sanitize_idea_text("x" * (data_sources.MAX_IDEA_TEXT_LENGTH + 1))

    def test_accepts_text_at_max_length(self):
        text = "x" * data_sources.MAX_IDEA_TEXT_LENGTH
        self.assertEqual(data_sources._sanitize_idea_text(text), text)

    def test_collapses_internal_whitespace_and_newlines(self):
        result = data_sources._sanitize_idea_text("  multi\nline   \n idea  ")
        self.assertEqual(result, "multi line idea")

    def test_strips_backticks_to_prevent_forged_state_marker(self):
        result = data_sources._sanitize_idea_text("looks `adopted` already")
        self.assertNotIn("`", result)

    def test_strips_leading_heading_and_list_markers(self):
        self.assertEqual(data_sources._sanitize_idea_text("## heading text"), "heading text")
        self.assertEqual(data_sources._sanitize_idea_text("- bullet text"), "bullet text")
        self.assertEqual(data_sources._sanitize_idea_text("> quote text"), "quote text")

    def test_rejects_text_that_is_only_markdown_markers(self):
        with self.assertRaises(data_sources.InvalidIdeaTextError):
            data_sources._sanitize_idea_text("### ---")


class InsertIdeaIntoInboxTests(unittest.TestCase):
    def test_creates_ideas_section_when_missing(self):
        result = data_sources._insert_idea_into_inbox("# IDEAS.md\n\nIntro.\n", "first idea")
        self.assertIn("## Ideas", result)
        self.assertIn("### Inbox", result)
        self.assertIn("- first idea `new`", result)

    def test_creates_inbox_subsection_at_end_of_ideas_section(self):
        original = (
            "## Ideas\n\n### Section A\n\n- old idea\n\n## Open unknowns\n\n- unknown\n"
        )
        result = data_sources._insert_idea_into_inbox(original, "new idea")
        # The new bullet must land inside "## Ideas", before "## Open unknowns".
        ideas_index = result.index("## Ideas")
        unknowns_index = result.index("## Open unknowns")
        bullet_index = result.index("- new idea `new`")
        self.assertTrue(ideas_index < bullet_index < unknowns_index)
        # Existing content is preserved untouched.
        self.assertIn("- old idea", result)

    def test_appends_to_existing_inbox_subsection(self):
        original = (
            "## Ideas\n\n### Section A\n\n- old idea\n\n"
            "### Inbox\n\n- first inbox idea `new`\n\n## Open unknowns\n\n- unknown\n"
        )
        result = data_sources._insert_idea_into_inbox(original, "second inbox idea")
        # Only one "### Inbox" heading, both bullets present under it.
        self.assertEqual(result.count("### Inbox"), 1)
        self.assertIn("- first inbox idea `new`", result)
        self.assertIn("- second inbox idea `new`", result)
        inbox_index = result.index("### Inbox")
        first_index = result.index("- first inbox idea")
        second_index = result.index("- second inbox idea")
        unknowns_index = result.index("## Open unknowns")
        self.assertTrue(inbox_index < first_index < second_index < unknowns_index)

    def test_appends_at_end_of_file_when_no_trailing_section(self):
        original = "## Ideas\n\n### Section A\n\n- old idea\n"
        result = data_sources._insert_idea_into_inbox(original, "trailing idea")
        self.assertIn("### Inbox", result)
        self.assertIn("- trailing idea `new`", result)
        self.assertTrue(result.endswith("\n"))


class AddIdeaIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ideas_path = Path(self.temp_dir.name) / "IDEAS.md"
        self.ideas_path.write_text(
            "## Ideas\n\n### Section\n\n- existing idea\n\n"
            "## Open unknowns\n\n- unknown\n",
            encoding="utf-8",
        )
        self._orig_ideas_path = data_sources.IDEAS_PATH
        data_sources.IDEAS_PATH = self.ideas_path
        self.addCleanup(self._restore_path)

    def _restore_path(self) -> None:
        data_sources.IDEAS_PATH = self._orig_ideas_path

    def test_add_idea_returns_new_idea_with_state_new(self):
        idea = data_sources.add_idea("a brand new idea")
        self.assertEqual(idea.text, "a brand new idea")
        self.assertEqual(idea.state, "new")
        self.assertEqual(idea.section, "Inbox")

    def test_add_idea_is_visible_in_list_ideas(self):
        data_sources.add_idea("visible idea")
        ideas = data_sources.list_ideas()
        texts = {i.text for i in ideas}
        self.assertIn("existing idea", texts)
        self.assertIn("visible idea", texts)

    def test_add_idea_rejects_invalid_text(self):
        with self.assertRaises(data_sources.InvalidIdeaTextError):
            data_sources.add_idea("   ")

    def test_concurrent_add_idea_calls_do_not_corrupt_file_or_lose_ideas(self):
        submitted = [f"concurrent idea {i}" for i in range(8)]
        errors: list[Exception] = []

        def _worker(text: str) -> None:
            try:
                data_sources.add_idea(text)
            except Exception as exc:  # pragma: no cover - failure surfaced via assert
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(text,)) for text in submitted]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        ideas = data_sources.list_ideas()
        texts = {i.text for i in ideas}
        self.assertIn("existing idea", texts)
        for text in submitted:
            self.assertIn(text, texts)
        # No duplicate/garbled bullets: exactly one idea per submitted text.
        self.assertEqual(
            sum(1 for i in ideas if i.text in submitted), len(submitted)
        )


class RepositoryIdeasTests(unittest.TestCase):
    def test_repository_has_no_ideas_left_after_moving_them_to_issues(self):
        # #340: the ideas moved to docs/ideas-to-issues.md and GitHub issues.
        ideas_path = Path(__file__).resolve().parents[1] / "IDEAS.md"
        with patch.object(data_sources, "IDEAS_PATH", ideas_path):
            ideas = data_sources.list_ideas()

        self.assertEqual(ideas, [])


if __name__ == "__main__":
    unittest.main()
