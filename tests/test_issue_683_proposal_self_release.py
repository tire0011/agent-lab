"""Nexus releases its own proposals after a wait, with exceptions (#683)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_nexus_initiative import InitiativeFixture

MARK = "\n\n---\nSuggested by the Nexus planning step. Remove the `proposal` label to let Nexus work on it."


def _proposal(number, title="Local model: faster patches", body="change x, test y", hours=13, comments=()):
    created = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    return {"number": number, "author": {"login": "tire0011"}, "title": title, "body": body + MARK,
            "labels": [{"name": "proposal"}], "createdAt": created,
            "comments": [{"body": c} for c in comments]}


class SelfReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()
        self.released: list[int] = []

    def _tick(self, issues, **extra):
        svc = self.fx.service(issue_lister=lambda: issues,
                              proposal_releaser=lambda n, t: self.released.append(n), **extra)
        self.addCleanup(svc.close)
        svc.tick()
        return svc

    def test_an_own_proposal_is_released_after_twelve_hours(self):
        self._tick([_proposal(700)])
        self.assertEqual(self.released, [700])

    def test_a_young_proposal_waits(self):
        self._tick([_proposal(700, hours=3)])
        self.assertEqual(self.released, [])

    def test_safeguards_needs_stop_and_foreign_issues_stay_with_the_owner(self):
        issues = [
            _proposal(701, body="raise the daily budget"),
            _proposal(702, title="Loosen the sudoers rule"),
            _proposal(703, title="Nexus needs: a GPU"),
            _proposal(704, comments=("Bitte stop, erst später",)),
            {**_proposal(705), "body": "status page"},  # not written by the planner
        ]
        self._tick(issues)
        self.assertEqual(self.released, [])

    def test_the_wait_is_a_mailbox_setting(self):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS service_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            conn.execute("INSERT OR REPLACE INTO service_settings VALUES ('nexus.proposal_wait_hours', '2', 'now')")
        self._tick([_proposal(700, hours=3)])
        self.assertEqual(self.released, [700])


if __name__ == "__main__":
    unittest.main()
