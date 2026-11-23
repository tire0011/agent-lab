"""Provider-specific extra budget: ``extra_budget_<provider>`` by mailbox lets
Nexus start more runs after the general budget is used up, each pinned to that
provider (e.g. Mistral on the owner's Vibe subscription)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from modules import coding_provider
from modules.coding_provider import NEXT_PIN_SETTING, choose_provider, pin_next_provider
from modules.mailbox.processing import _process_setting
from modules.nexus_initiative import PROVIDER_EXTRA_PREFIX
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture

PROVIDERS = ["codex", "claude", "mistral"]


def _msg(**meta):
    return SimpleNamespace(meta=meta, file_path=Path("inbox/x.md"), body="")


def _issues(*numbers):
    return lambda: [
        {"number": n, "author": {"login": "tire0011"}, "title": f"Issue {n}", "body": ""} for n in numbers
    ]


class MailboxSettingTests(unittest.TestCase):
    def test_provider_extra_budget_is_stored_for_24_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "agent.db"))
            try:
                reply, _ = _process_setting(_msg(key="extra_budget_mistral", value="5"), Path("."), conn, 1)
                self.assertIn("extra_budget_mistral is now 5 for the next 24 hours", reply)
                row = conn.execute(
                    "SELECT value FROM service_settings WHERE key = 'nexus.extra_budget.mistral'"
                ).fetchone()
                self.assertEqual(row[0], "5")
                self.assertIn("Unknown setting",
                              _process_setting(_msg(key="extra_budget_gemini", value="1"), Path("."), conn, 1)[0])
            finally:
                conn.close()


class NextPinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        init_db(self.db).close()
        patcher = mock.patch("modules.coding_provider.available_providers", return_value=list(PROVIDERS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pin_wins_once_and_keeps_the_rotation_position(self):
        first = choose_provider(db_path=self.db)
        pin_next_provider("mistral", db_path=self.db)
        self.assertEqual(choose_provider(db_path=self.db), "mistral")
        # The rotation continues after the provider chosen before the pin.
        self.assertEqual(choose_provider(db_path=self.db), PROVIDERS[(PROVIDERS.index(first) + 1) % 3])

    def test_the_experiment_services_selection_worker_leaves_the_pin_for_the_implementation(self):
        """2026-11-21: ExperimentService built its selection worker eagerly and
        used up the Mistral pin; the implementation then ran on Claude."""
        from modules.experiment_service import ExperimentService
        pin_next_provider("mistral", db_path=self.db)
        with mock.patch("modules.coding_provider.build_worker", return_value=mock.Mock()):
            service = ExperimentService(db_path=self.db)
            self.assertIsNotNone(service.selection_worker)
        self.assertEqual(choose_provider(db_path=self.db), "mistral")

    def test_stale_or_unavailable_pins_are_dropped(self):
        pin_next_provider("mistral", db_path=self.db)
        with sqlite3.connect(self.db) as conn:
            old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
            conn.execute("UPDATE service_settings SET updated_at = ? WHERE key = ?", (old, NEXT_PIN_SETTING))
        self.assertEqual(choose_provider(db_path=self.db), "codex")
        pin_next_provider("gemini", db_path=self.db)
        self.assertNotEqual(choose_provider(db_path=self.db), "gemini")
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM service_settings WHERE key = ?", (NEXT_PIN_SETTING,)).fetchone())


class InitiativeTests(unittest.TestCase):
    def _service(self, extra: str | None, *, set_at: datetime | None = None, issues=(1, 2, 3)):
        fx = InitiativeFixture()
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS service_settings "
                         "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            if extra is not None:
                conn.execute("INSERT INTO service_settings VALUES ('nexus.extra_budget.mistral', ?, ?)",
                             (extra, (set_at or datetime.now(timezone.utc)).isoformat()))
        patchers = [
            mock.patch("modules.coding_provider.available_providers", return_value=list(PROVIDERS)),
            mock.patch("modules.coding_provider.provider_cooldowns", return_value={}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        svc = fx.service(daily_budget=1, issue_lister=_issues(*issues))
        self.addCleanup(svc.close)
        return fx, svc

    def _link(self, fx, decision):
        # A finished dispatch: the issue now has an experiment (not busy any more).
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                         (f"e{decision.candidate.ref}", int(decision.candidate.ref)))

    def _pin(self, fx):
        with sqlite3.connect(fx.db_path) as conn:
            row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (NEXT_PIN_SETTING,)).fetchone()
        return row[0] if row else None

    def test_extra_mistral_runs_after_the_budget_is_used_up(self):
        fx, svc = self._service("1")
        first = svc.tick()
        self.assertTrue(first.acted)
        self.assertNotEqual(first.detail, PROVIDER_EXTRA_PREFIX + "mistral")
        self.assertIsNone(self._pin(fx))
        self._link(fx, first)

        extra = svc.tick()  # general budget (1) used up -> the Mistral extra start
        self.assertTrue(extra.acted)
        self.assertEqual(extra.detail, PROVIDER_EXTRA_PREFIX + "mistral")
        self.assertEqual(self._pin(fx), "mistral")
        self._link(fx, extra)

        self.assertEqual(svc.tick().decision, "budget_exhausted")  # extra used up too
        self.assertEqual(fx.controller.issues, [1, 2])

    def test_no_or_expired_extra_changes_nothing(self):
        for extra, set_at in ((None, None), ("3", datetime.now(timezone.utc) - timedelta(days=2))):
            fx, svc = self._service(extra, set_at=set_at)
            self._link(fx, svc.tick())
            self.assertEqual(svc.tick().decision, "budget_exhausted")
            self.assertIsNone(self._pin(fx))

    def test_a_cooling_provider_gets_no_extra_start(self):
        fx, svc = self._service("2")
        self._link(fx, svc.tick())
        with mock.patch("modules.coding_provider.provider_cooldowns", return_value={"mistral": "later"}):
            self.assertEqual(svc.tick().decision, "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
