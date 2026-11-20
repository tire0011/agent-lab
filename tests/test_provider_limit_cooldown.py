"""#753: a provider usage limit is recognized, cools the provider down until
its reset, and a per-issue loop guard stops quick no-change repeats."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from modules import coding_provider
from modules.coding_provider import (
    choose_provider,
    is_provider_limit,
    is_provider_overload,
    note_provider_limit,
    other_provider,
    parse_limit_reset,
    provider_cooldowns,
)
from modules.nexus_initiative import (
    DECISION_PROVIDERS_COOLING_DOWN,
    DECISION_SKIPPED_LOOP_GUARD,
    _is_provider_overload,
)
from tests.test_nexus_initiative import InitiativeFixture

# Verbatim provider messages.
LIMIT_MESSAGES = (
    "You've hit your weekly limit · resets 12pm (UTC)",  # Claude, 2026-11-18
    "You've hit your limit · resets 3pm (UTC)",
    "You've hit your session limit · resets 9:30am (UTC)",
    "You've hit your 5-hour limit · resets 5am (UTC)",
    "You've hit your daily limit · resets in 3h",
    "Claude AI usage limit reached|1762300800",
    "Claude usage limit reached",
    "You've hit your usage limit. Upgrade to Pro or try again in 2 hours 15 minutes.",  # Codex
    "stream error: You exceeded your current quota, please check your plan and billing details. "
    "insufficient_quota",  # OpenAI
    "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
    "quota exceeded for model gpt-5-codex; try again at 2026-11-18T14:00:00Z",
    "http_402; GitHub checkpoint failed: implementation produced no publishable source/test/documentation changes",  # Mistral, 2026-11-20
    "Error: 402 Payment Required",
    "insufficient credits on this API key",
)
NORMAL_FAILURES = (
    "tests failed (exit code 1): tests/test_a.py::test_rate_limit_resets",
    "tests failed (exit code 1): tests/test_quota.py::test_daily_limit",
    "kaboom",
    "implementation phase timed out",
    "Claude produced no parseable result object",
    "verification failed: AssertionError: limit must be positive",
    "tests failed (exit code 1): tests/test_issue_402_widgets.py::test_payment",
)
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
ISSUE = [{"number": 745, "author": {"login": "tire0011"}, "title": "Gate preflight", "body": ""}]


def _add_settings_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS service_settings "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )


class LimitRecognitionTests(unittest.TestCase):
    def test_every_verbatim_limit_message_is_a_limit_and_an_overload(self):
        for message in LIMIT_MESSAGES:
            with self.subTest(message=message):
                self.assertTrue(is_provider_limit(message))
                self.assertTrue(is_provider_overload(message))
                self.assertTrue(_is_provider_overload("failed", message))

    def test_normal_failures_are_neither(self):
        for message in NORMAL_FAILURES:
            with self.subTest(message=message):
                self.assertFalse(is_provider_limit(message))
                self.assertFalse(_is_provider_overload("failed", message))

    def test_a_plain_overload_is_no_limit(self):
        self.assertTrue(is_provider_overload("API Error: 529 Overloaded"))
        self.assertFalse(is_provider_limit("API Error: 529 Overloaded"))


class ResetParsingTests(unittest.TestCase):
    def test_clock_time_is_the_next_such_time(self):
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[0], NOW), NOW.replace(hour=12))
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[2], NOW), NOW.replace(hour=9, minute=30))
        # 5am already passed at 08:00: tomorrow.
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[3], NOW), NOW.replace(hour=5) + timedelta(days=1))
        self.assertEqual(parse_limit_reset("limit · resets 12am (UTC)", NOW), NOW.replace(hour=0) + timedelta(days=1))

    def test_relative_and_absolute_times(self):
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[4], NOW), NOW + timedelta(hours=3))
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[7], NOW), NOW + timedelta(hours=2, minutes=15))
        self.assertEqual(parse_limit_reset(LIMIT_MESSAGES[10], NOW), NOW.replace(hour=14))
        self.assertEqual(
            parse_limit_reset(LIMIT_MESSAGES[5], NOW), datetime.fromtimestamp(1762300800, tz=timezone.utc)
        )

    def test_no_reset_given(self):
        self.assertIsNone(parse_limit_reset("Claude usage limit reached", NOW))
        self.assertIsNone(parse_limit_reset(LIMIT_MESSAGES[8], NOW))


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"AGENT_CODING_PROVIDERS": "codex,claude"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_CODING_PROVIDER", None)
        available = patch.object(coding_provider, "is_available", return_value=True)
        available.start()
        self.addCleanup(available.stop)

    def _setting(self, key):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def test_cooldown_is_persisted_with_the_parsed_reset(self):
        now = datetime.now(timezone.utc)
        until = note_provider_limit("claude", "You've hit your daily limit · resets in 3h", db_path=self.db_path, now=now)
        self.assertEqual(until, now + timedelta(hours=3))
        self.assertEqual(self._setting("provider_cooldown_until.claude"), until.isoformat())
        self.assertEqual(provider_cooldowns(db_path=self.db_path), {"claude": until})

    def test_no_reset_means_one_hour(self):
        now = datetime.now(timezone.utc)
        until = note_provider_limit("codex", "insufficient_quota", db_path=self.db_path, now=now)
        self.assertEqual(until, now + timedelta(hours=1))

    def test_normal_failures_and_unknown_providers_set_nothing(self):
        self.assertIsNone(note_provider_limit("claude", "kaboom", db_path=self.db_path))
        self.assertIsNone(note_provider_limit("local", LIMIT_MESSAGES[0], db_path=self.db_path))
        self.assertEqual(provider_cooldowns(db_path=self.db_path), {})

    def test_claude_is_skipped_until_the_reset_then_returns(self):
        now = datetime.now(timezone.utc)
        until = note_provider_limit("claude", "limit · resets in 2h", db_path=self.db_path, now=now)
        self.assertEqual({choose_provider(db_path=self.db_path) for _ in range(4)}, {"codex"})
        self.assertEqual(coding_provider.allowed_providers(db_path=self.db_path), ["codex"])
        self.assertEqual(other_provider("codex", db_path=self.db_path), None)
        self.assertEqual(other_provider("claude", db_path=self.db_path), "codex")
        # After the reset Claude is back in the rotation.
        self.assertEqual(provider_cooldowns(db_path=self.db_path, now=until + timedelta(seconds=1)), {})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE service_settings SET value = ? WHERE key = 'provider_cooldown_until.claude'",
                ((now - timedelta(seconds=1)).isoformat(),),
            )
        self.assertEqual({choose_provider(db_path=self.db_path) for _ in range(4)}, {"codex", "claude"})

    def test_a_later_cooldown_is_kept(self):
        now = datetime.now(timezone.utc)
        late = note_provider_limit("claude", "limit · resets in 5h", db_path=self.db_path, now=now)
        self.assertEqual(note_provider_limit("claude", "Claude usage limit reached", db_path=self.db_path, now=now), late)


class InitiativeTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()
        _add_settings_table(self.fx.db_path)
        env = patch.dict(os.environ, {"AGENT_CODING_PROVIDERS": "codex,claude", "NEXUS_GATE_PREFLIGHT": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_CODING_PROVIDER", None)
        available = patch.object(coding_provider, "is_available", return_value=True)
        available.start()
        self.addCleanup(available.stop)

    def _cool(self, provider, hours):
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO service_settings VALUES (?, ?, ?)",
                (f"provider_cooldown_until.{provider}", until.isoformat(), until.isoformat()),
            )
        return until

    def _no_change(self, exp_id, ended, error="implementation produced no publishable source/test/documentation changes"):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, ended_at, created_at, error) VALUES (?,?,?,?,?,?)",
                (exp_id, "g", "failed", ended.isoformat(), ended.isoformat(), error),
            )
            conn.execute("INSERT INTO github_experiment_links VALUES (?, ?)", (exp_id, 745))

    def test_all_providers_cooling_down_starts_nothing_paid(self):
        self._cool("claude", 4)
        codex = self._cool("codex", 1)
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        decision = svc.tick()
        self.assertEqual(decision.decision, DECISION_PROVIDERS_COOLING_DOWN)
        self.assertEqual(json.loads(decision.detail)["earliest_reset"], codex.isoformat())
        self.assertEqual(self.fx.controller.issues, [])
        self.assertEqual(self.fx.adoption.started, [])
        self.assertEqual(svc.history(1)[0]["decision"], DECISION_PROVIDERS_COOLING_DOWN)

    def test_one_provider_left_still_dispatches(self):
        self._cool("claude", 4)
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        self.assertEqual(svc.tick().decision, "dispatched")

    def test_loop_guard_triggers_on_the_fourth_quick_no_change_failure(self):
        start = datetime.now(timezone.utc) - timedelta(minutes=50)
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        for i in range(3):
            self._no_change(f"e{i}", start + timedelta(minutes=10 * i), error=LIMIT_MESSAGES[0])
        self.assertNotIn(745, svc._loop_guarded_issues())
        self._no_change("e3", start + timedelta(minutes=30))
        guarded = svc._loop_guarded_issues()
        self.assertIn(745, guarded)
        self.assertEqual(
            guarded[745]["paused_until"], (start + timedelta(minutes=30, hours=6)).isoformat()
        )
        decision = svc.tick()
        self.assertEqual(decision.decision, "idle")
        self.assertEqual(self.fx.controller.issues, [])
        logged = [h for h in svc.history(10) if h["decision"] == DECISION_SKIPPED_LOOP_GUARD]
        self.assertEqual(len(logged), 1)
        svc.tick()  # logged once per trigger, not every tick
        self.assertEqual(len([h for h in svc.history(10) if h["decision"] == DECISION_SKIPPED_LOOP_GUARD]), 1)

    def test_slow_no_change_failures_do_not_trigger(self):
        start = datetime.now(timezone.utc) - timedelta(hours=5)
        svc = self.fx.service()
        self.addCleanup(svc.close)
        for i in range(4):
            self._no_change(f"e{i}", start + timedelta(minutes=25 * i))
        self.assertNotIn(745, svc._loop_guarded_issues())

    def test_the_guard_lifts_after_six_hours(self):
        start = datetime.now(timezone.utc) - timedelta(hours=7)
        svc = self.fx.service()
        self.addCleanup(svc.close)
        for i in range(4):
            self._no_change(f"e{i}", start + timedelta(minutes=5 * i))
        self.assertNotIn(745, svc._loop_guarded_issues())


class Replay20261118Tests(unittest.TestCase):
    """Measure (#753): the 08:00-10:00 UTC sequence of 2026-11-18.

    Then 15 experiments started, every Claude run ending at once with the
    weekly-limit message. The replay runs 15 starts 8 minutes apart through the
    real selection, handoff and cooldown code; the replay clock starts now, so
    "resets 12pm" lies four hours ahead as it did at 08:00.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"AGENT_CODING_PROVIDERS": "codex,claude"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_CODING_PROVIDER", None)
        available = patch.object(coding_provider, "is_available", return_value=True)
        available.start()
        self.addCleanup(available.stop)
        # The rotation stood at Codex, so Claude was next - as on 2026-11-18.
        conn = coding_provider.init_db(self.db_path)
        conn.execute(
            "INSERT INTO service_settings VALUES ('coding_provider.last_selected', 'codex', ?)",
            (NOW.isoformat(),),
        )
        conn.commit()
        conn.close()

    def _replay(self, *, codex_limited: bool) -> list[str]:
        events: list[str] = []
        now = datetime.now(timezone.utc)
        for _start in range(15):
            if set(provider_cooldowns(db_path=self.db_path, now=now)) >= {"codex", "claude"}:
                events.append(DECISION_PROVIDERS_COOLING_DOWN)
                now += timedelta(minutes=8)
                continue
            provider = choose_provider(db_path=self.db_path)
            events.append(provider)
            while provider is not None:
                if provider == "claude":
                    error = "You've hit your weekly limit · resets in 4h"
                elif codex_limited:
                    error = "You've hit your usage limit. Try again in 3 hours."
                else:
                    break  # Codex did the work
                self.assertTrue(is_provider_overload(error))
                note_provider_limit(provider, error, db_path=self.db_path, now=now)
                provider = other_provider(provider, db_path=self.db_path)
                if provider is not None:
                    events.append(f"{provider} (handoff)")
            now += timedelta(minutes=8)
        return events

    def test_at_most_one_claude_attempt_then_codex(self):
        events = self._replay(codex_limited=False)
        self.assertEqual(events[:2], ["claude", "codex (handoff)"])
        self.assertEqual(events.count("claude"), 1)
        self.assertEqual(set(events[2:]), {"codex"})

    def test_both_limited_ends_in_providers_cooling_down(self):
        events = self._replay(codex_limited=True)
        self.assertEqual(events.count("claude"), 1)
        self.assertEqual(events[:2], ["claude", "codex (handoff)"])
        self.assertEqual(set(events[2:]), {DECISION_PROVIDERS_COOLING_DOWN})


if __name__ == "__main__":
    unittest.main()
