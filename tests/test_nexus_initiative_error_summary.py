"""Planner history summarizes quality-gate failures instead of cutting them (#444)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from modules.experiment_evaluation import summarize_error
from modules.nexus_initiative import NexusInitiativeService


def missing(*paths: str) -> str:
    return "; ".join(f"erforderliches Artefakt {path} fehlt" for path in paths)


def quality_error(*paths: str) -> str:
    return f"quality evaluation: {missing(*paths)}"


GIT_PATHS = [f"git/hooks/hook-{i}.sample" for i in range(37)]
MANY_PATHS = [*GIT_PATHS, "modules/x.py", "modules/y.py", "README.md"]


class SummarizeErrorTests(unittest.TestCase):
    def test_forty_items_become_a_count_with_examples(self):
        summary = summarize_error(quality_error(*MANY_PATHS))
        self.assertTrue(summary.startswith("quality evaluation: 40 required artifacts missing"))
        self.assertIn("(+37 more)", summary)
        examples = summary.split(", e.g. ")[1].split(" (+")[0].split(", ")
        self.assertLessEqual(len(examples), 3)
        self.assertLessEqual(len(summary), 200)

    def test_repeated_prefix_is_grouped_with_a_count(self):
        summary = summarize_error(quality_error(*MANY_PATHS))
        self.assertIn("(git/: 37, modules/: 2)", summary)

    def test_a_real_gap_is_not_hidden_behind_repeated_false_positives(self):
        summary = summarize_error(quality_error(*MANY_PATHS))
        self.assertEqual(
            summary.split(", e.g. ")[1],
            "git/hooks/hook-0.sample, modules/x.py, README.md (+37 more)",
        )

    def test_other_reasons_survive_the_summary(self):
        error = f"quality evaluation: nur Tests geändert; {missing(*MANY_PATHS)}"
        summary = summarize_error(error)
        self.assertIn("40 required artifacts missing", summary)
        self.assertTrue(summary.endswith("; nur Tests geändert"))
        self.assertLessEqual(len(summary), 200)

    def test_long_paths_shrink_the_examples_but_keep_the_count(self):
        paths = [f"modules/{'very_long_directory_name/' * 4}file_{i}.py" for i in range(40)]
        summary = summarize_error(quality_error(*paths))
        self.assertIn("40 required artifacts missing (modules/: 40)", summary)
        self.assertLessEqual(len(summary), 200)

    def test_paths_too_long_for_any_example_leave_the_count_alone(self):
        paths = [f"{'d' * 250}/file_{i}.py" for i in range(40)]
        summary = summarize_error(quality_error(*paths), limit=60)
        self.assertLessEqual(len(summary), 60)
        self.assertIn("40 required artifacts missing", summary)

    def test_a_single_artifact_item_is_nothing_to_count(self):
        error = f"quality evaluation: {missing('modules/x.py')}; {'word ' * 60}"
        summary = summarize_error(error)
        self.assertNotIn("required artifacts", summary)
        self.assertTrue(summary.endswith("…"))

    def test_plain_long_error_is_cut_at_a_word_boundary(self):
        error = "Traceback  failed\nbecause " + "something " * 40
        summary = summarize_error(error)
        self.assertLessEqual(len(summary), 200)
        self.assertTrue(summary.endswith("something…"))
        self.assertTrue(" ".join(error.split()).startswith(summary[:-1]))

    def test_word_longer_than_the_limit_is_cut_hard(self):
        summary = summarize_error("x" * 300)
        self.assertEqual(summary, "x" * 199 + "…")

    def test_short_error_is_unchanged(self):
        self.assertEqual(summarize_error("tests failed"), "tests failed")

    def test_short_quality_error_stays_complete(self):
        error = quality_error("modules/x.py", "modules/y.py")
        self.assertEqual(summarize_error(error), error)

    def test_no_error_is_empty(self):
        self.assertEqual(summarize_error(None), "")


class RecentExperimentErrorTests(unittest.TestCase):
    def test_recent_experiments_returns_the_summary(self):
        svc = NexusInitiativeService(db_path=":memory:", experiment_service=SimpleNamespace())
        self.addCleanup(svc.close)
        svc._conn.execute(
            "CREATE TABLE experiments (id TEXT, goal TEXT, status TEXT, ended_at TEXT, error TEXT)"
        )
        svc._conn.execute(
            "INSERT INTO experiments VALUES ('e1', 'Goal', 'needs_review', '2026-10-30', ?)",
            (quality_error(*MANY_PATHS),),
        )
        error = svc._recent_experiments()[0]["error"]
        self.assertEqual(error, summarize_error(quality_error(*MANY_PATHS)))
        self.assertIn("40 required artifacts missing", error)


if __name__ == "__main__":
    unittest.main()
