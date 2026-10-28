"""#416: only the Change section of an issue names required artifacts."""

import unittest

from modules.experiment_evaluation import extract_explicit_goal_artifacts


class ChangeSectionArtifactsTest(unittest.TestCase):
    def test_bold_headings_take_paths_from_change_only(self):
        goal = (
            "GitHub Issue #416: gate\n\n**Problem**\nFlagged git/hooks/commit-msg.sample "
            "and modules/x.py as missing.\n\n**Change**\n- Edit `modules/experiment_evaluation.py` "
            "and add modules/new_thing.py.\n\n**Test** (`tests/test_experiment_evaluation.py`)\n"
            "- Fixture modules/y.py is missing.\n"
        )
        self.assertEqual(
            extract_explicit_goal_artifacts(goal),
            ["modules/experiment_evaluation.py", "modules/new_thing.py"],
        )

    def test_colon_headings(self):
        goal = (
            "Problem: git/outbox_publisher.lock was required.\n\n"
            "Change: in `modules/experiment_evaluation.py`, filter paths.\n\n"
            "Test: modules/x.py exists in a temp worktree.\n"
        )
        self.assertEqual(extract_explicit_goal_artifacts(goal), ["modules/experiment_evaluation.py"])

    def test_without_sections_keeps_every_path(self):
        goal = "Fix modules/a.py and update modules/b.py."
        self.assertEqual(extract_explicit_goal_artifacts(goal), ["modules/a.py", "modules/b.py"])


if __name__ == "__main__":
    unittest.main()
