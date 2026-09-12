"""Every controller turn leaves a durable record (#158).

Before this existed, which model decided a turn was recoverable only from a log
line. Answering "how often does the local controller handle a turn, and does it
produce a usable decision" meant grepping the systemd journal - retention-bound,
unqueryable from the API, and impossible to separate from test runs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.nexus_task_store import NexusTaskStore


class ControllerTurnRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = NexusTaskStore(db_path=str(Path(self._tmp.name) / "agent.db"))
        self.addCleanup(self.store.close)
        self.session = self.store.get_or_create(source_type="task", source_id="s1", title="t")

    def _record(self, **kwargs) -> None:
        self.store.record_controller_turn(self.session.id, **kwargs)

    def test_local_turn_is_not_counted_as_escalated(self):
        self._record(provider_used="local", outcome="answer", prompt_chars=1200)

        stats = self.store.controller_turn_stats()

        self.assertEqual(stats["total_turns"], 1)
        self.assertEqual(stats["escalated_turns"], 0)
        self.assertEqual(stats["escalation_rate"], 0.0)

    def test_external_provider_counts_as_escalated(self):
        self._record(provider_used="codex_readonly", outcome="answer")

        stats = self.store.controller_turn_stats()

        self.assertEqual(stats["escalated_turns"], 1)
        self.assertEqual(stats["escalation_rate"], 1.0)

    def test_failed_turns_are_counted_too(self):
        # A statistic that only recorded successful turns would answer the
        # wrong question - that is the defect this table exists to remove.
        self._record(provider_used="local", outcome="answer")
        self._record(provider_used="none", outcome="budget_exceeded", detail="over budget")
        self._record(provider_used="local", outcome="invalid_json", detail="bad escape")

        stats = self.store.controller_turn_stats()

        self.assertEqual(stats["total_turns"], 3)
        self.assertEqual(stats["by_outcome"]["budget_exceeded"], 1)
        self.assertEqual(stats["by_outcome"]["invalid_json"], 1)
        self.assertEqual(stats["by_outcome"]["answer"], 1)

    def test_escalation_rate_matches_the_measured_shape(self):
        # The production journal for 30 days showed 15 escalations across 67
        # local turns (~22%). Pin the arithmetic that will report that.
        for _ in range(52):
            self._record(provider_used="local", outcome="answer")
        for _ in range(15):
            self._record(provider_used="codex_readonly", outcome="answer")

        stats = self.store.controller_turn_stats()

        self.assertEqual(stats["total_turns"], 67)
        self.assertEqual(stats["escalated_turns"], 15)
        self.assertAlmostEqual(stats["escalation_rate"], 0.2239, places=3)
        self.assertEqual(stats["by_provider"], {"local": 52, "codex_readonly": 15})

    def test_since_filter_separates_periods(self):
        self._record(provider_used="local", outcome="answer")

        self.assertEqual(self.store.controller_turn_stats(since="2099-01-01")["total_turns"], 0)
        self.assertEqual(self.store.controller_turn_stats(since="2000-01-01")["total_turns"], 1)

    def test_recording_never_breaks_a_turn(self):
        # Instrumentation must not be the reason a controller turn fails.
        self.store.close()

        self.store.record_controller_turn(self.session.id, provider_used="local", outcome="answer")


if __name__ == "__main__":
    unittest.main()
