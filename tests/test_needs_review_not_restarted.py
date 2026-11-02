"""A needs_review adoption that cannot get further is not restarted every tick."""

import unittest
from types import SimpleNamespace

from tests.test_nexus_initiative import InitiativeFixture


class NeedsReviewNotRestartedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = InitiativeFixture()

    def _service(self, retryable):
        self.fx.adoption.assess_failure = lambda experiment_id: SimpleNamespace(retryable=retryable)
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service()
        self.addCleanup(svc.close)
        return svc

    def test_terminal_needs_review_is_skipped(self):
        svc = self._service(False)

        self.assertEqual(svc.tick().decision, "idle")
        self.assertEqual(self.fx.adoption.started, [])

    def test_resumable_needs_review_is_still_picked(self):
        svc = self._service(True)

        self.assertTrue(svc.tick().acted)
        self.assertEqual(self.fx.adoption.started, ["stuck"])


if __name__ == "__main__":
    unittest.main()
