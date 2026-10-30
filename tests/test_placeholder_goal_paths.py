"""#433/#455: example paths in an issue are not required artifacts."""

import unittest

from modules.experiment_evaluation import extract_explicit_goal_artifacts


class PlaceholderGoalPathsTest(unittest.TestCase):
    def test_placeholder_paths_are_skipped(self):
        goal = (
            "Change: in `modules/experiment_adoption_service.py`, name the conflicts "
            "(for example `Integrationskonflikt: modules/a.py`), like modules/new_thing.py "
            "or modules/foo.py would.\n"
        )
        self.assertEqual(
            extract_explicit_goal_artifacts(goal), ["modules/experiment_adoption_service.py"]
        )

    def test_real_short_names_still_count(self):
        self.assertEqual(extract_explicit_goal_artifacts("Change: add modules/io.py."), ["modules/io.py"])


if __name__ == "__main__":
    unittest.main()
