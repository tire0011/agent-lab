"""An exhausted budget still lets the tick adopt verified work (#538).

The capability-resume check ran first and returned on ``budget_exhausted``, so
service.tick() - which adopts fresh verified work without budget - never ran:
experiment 21883e58 (#597) waited overnight.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import nexus_tick


class _Service:
    controller = object()

    def __init__(self, resume_decision: str):
        self.resume_decision = resume_decision
        self.ticked = False

    def resume_blocked_capability(self):
        return SimpleNamespace(decision=self.resume_decision, detail="", candidate=None)

    def abandoned_work(self):
        return []

    def tick(self):
        self.ticked = True
        return SimpleNamespace(acted=False, candidate=None, decision="idle", detail="")

    def close(self):
        pass


def _run(service: _Service) -> int:
    with patch.object(nexus_tick, "_build_service", return_value=service), \
            patch.object(nexus_tick, "_reconcile_stale_claims"), \
            patch.object(nexus_tick, "_sweep_stalled_experiments"):
        return nexus_tick.main(["--db-path", ":memory:"])


class BudgetAdoptionTests(unittest.TestCase):
    def test_budget_exhausted_resume_still_runs_the_tick(self):
        service = _Service("budget_exhausted")
        self.assertEqual(_run(service), 0)
        self.assertTrue(service.ticked)

    def test_busy_still_stops_the_tick(self):
        service = _Service("busy")
        _run(service)
        self.assertFalse(service.ticked)


if __name__ == "__main__":
    unittest.main()
