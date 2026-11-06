"""#358: the initiative tick waits for the experiment it started.

In the first autonomous cycle the oneshot tick exited right after dispatch;
the experiment's daemon thread died with it and stayed ``queued``.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import nexus_tick
from modules.experiment_adoption_service import ADOPTION_THREAD_PREFIX
from modules.nexus_controller_service import EXPERIMENT_THREAD_PREFIX


class _Service:
    controller = None

    def __init__(self, prefix=EXPERIMENT_THREAD_PREFIX):
        self.prefix = prefix
        self.finished = threading.Event()
        self.closed = False

    def abandoned_work(self):
        return []

    def tick(self):
        def run():
            time.sleep(0.3)
            self.finished.set()

        threading.Thread(target=run, daemon=True, name=f"{self.prefix}abc").start()
        return SimpleNamespace(acted=False, candidate=None, decision="dispatched", detail="")

    def close(self):
        self.closed = True


class _ChainingService(_Service):
    """An experiment thread that starts a second one, as a resumed controller does."""

    def tick(self):
        def second():
            time.sleep(0.3)
            self.finished.set()

        def first():
            time.sleep(0.1)
            threading.Thread(target=second, daemon=True, name=f"{EXPERIMENT_THREAD_PREFIX}second").start()

        threading.Thread(target=first, daemon=True, name=f"{EXPERIMENT_THREAD_PREFIX}first").start()
        return SimpleNamespace(acted=False, candidate=None, decision="dispatched", detail="")


class TickWaitsTests(unittest.TestCase):
    def test_the_tick_returns_only_after_its_experiment_ended(self):
        service = _Service()
        with patch.object(nexus_tick, "_build_service", return_value=service), \
                patch.object(nexus_tick, "_reconcile_stale_claims"):
            self.assertEqual(nexus_tick.main(["--db-path", ":memory:"]), 0)
        self.assertTrue(service.finished.is_set())
        self.assertTrue(service.closed)

    def test_the_tick_also_waits_for_an_experiment_started_by_another(self):
        # #388: the resumed controller started deac59fb after fbe17810 was
        # rejected; the tick exited and left it implementing for 11 hours.
        service = _ChainingService()
        with patch.object(nexus_tick, "_build_service", return_value=service), \
                patch.object(nexus_tick, "_reconcile_stale_claims"):
            nexus_tick.main(["--db-path", ":memory:"])
        self.assertTrue(service.finished.is_set())

    def test_the_tick_returns_only_after_its_adoption_ended(self):
        # After #359 the next tick started an integration, exited, and left
        # the adoption in ``adopting``.
        service = _Service(prefix=ADOPTION_THREAD_PREFIX)
        with patch.object(nexus_tick, "_build_service", return_value=service), \
                patch.object(nexus_tick, "_reconcile_stale_claims"):
            self.assertEqual(nexus_tick.main(["--db-path", ":memory:"]), 0)
        self.assertTrue(service.finished.is_set())


if __name__ == "__main__":
    unittest.main()
