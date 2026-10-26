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
from modules.nexus_controller_service import EXPERIMENT_THREAD_PREFIX


class _Service:
    controller = None

    def __init__(self):
        self.finished = threading.Event()
        self.closed = False

    def abandoned_work(self):
        return []

    def tick(self):
        def run():
            time.sleep(0.3)
            self.finished.set()

        threading.Thread(target=run, daemon=True, name=f"{EXPERIMENT_THREAD_PREFIX}abc").start()
        return SimpleNamespace(acted=False, candidate=None, decision="dispatched", detail="")

    def close(self):
        self.closed = True


class TickWaitsTests(unittest.TestCase):
    def test_the_tick_returns_only_after_its_experiment_ended(self):
        service = _Service()
        with patch.object(nexus_tick, "_build_service", return_value=service), \
                patch.object(nexus_tick, "_reconcile_stale_claims"):
            self.assertEqual(nexus_tick.main(["--db-path", ":memory:"]), 0)
        self.assertTrue(service.finished.is_set())
        self.assertTrue(service.closed)


if __name__ == "__main__":
    unittest.main()
