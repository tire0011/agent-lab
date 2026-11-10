"""#630: stored reflections are readable and reach the planner prompt.

API (authenticated), matching read-only MCP tools, operations catalog entries,
and the 'IDEAS FROM YOUR OWN REFLECTIONS' section of the planner prompt.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api
import nexus_mcp
from modules.experiment_reflection import (
    get_reflection,
    list_reflections,
    recent_suggestions,
)
from modules.experiment_service import ExperimentNotFoundError
from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS
from modules.nexus_planner import IssuePlanner, planning_prompt
from modules.operations_registry import OperationsRegistry
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture

HEADERS = {"x-api-key": "test-key"}
SECTION = "IDEAS FROM YOUR OWN REFLECTIONS"
ISSUES = [{"number": 1, "author": {"login": "tire0011"}, "title": "Work", "body": ""}]


def _suggestion(title, **extra):
    value = {"title": title, "benefit": f"b-{title}", "cost": "small", "risk": "low",
             "measure": f"m-{title}"}
    value.update(extra)
    return value


def _store(conn, experiment_id, created_at, suggestions, *, status="completed", error=None):
    conn.execute(
        """INSERT INTO experiment_reflections
           (experiment_id, created_at, model, suggestions_json, error, status)
           VALUES (?, ?, 'fake-local', ?, ?, ?)""",
        (experiment_id, created_at, json.dumps(suggestions), error, status),
    )
    conn.commit()


_MINIMAL_TABLE = """CREATE TABLE experiment_reflections (
    experiment_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, model TEXT NOT NULL,
    suggestions_json TEXT NOT NULL, error TEXT NULL, status TEXT NOT NULL DEFAULT 'completed')"""


class _ReflectionDb:
    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "agent.db")
        conn = init_db(self.path)
        for exp_id in ("exp-a", "exp-b", "exp-p", "exp-none"):
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at) VALUES (?, 'g', 'succeeded', ?)",
                (exp_id, "2026-11-01T00:00:00Z"),
            )
        _store(conn, "exp-a", "2026-11-01T10:00:00+00:00",
               [_suggestion("Cache the planner context"),
                _suggestion("Retry merges", duplicate_of="Retry merges with current main")])
        _store(conn, "exp-b", "2026-11-02T10:00:00+00:00", [],
               error="RuntimeError: model down")
        _store(conn, "exp-p", "2026-11-03T10:00:00+00:00", [], status="pending")
        conn.close()


class ReadHelperTests(unittest.TestCase):
    def setUp(self):
        self.db = _ReflectionDb(self)

    def test_get_returns_completed_and_ignores_pending_or_missing(self):
        stored = get_reflection("exp-a", db_path=self.db.path)
        self.assertEqual(stored["model"], "fake-local")
        self.assertEqual(stored["suggestions"][0]["title"], "Cache the planner context")
        self.assertIsNone(get_reflection("exp-p", db_path=self.db.path))
        self.assertIsNone(get_reflection("exp-none", db_path=self.db.path))

    def test_list_is_newest_first_without_pending(self):
        ids = [r["experiment_id"] for r in list_reflections(10, db_path=self.db.path)]
        self.assertEqual(ids, ["exp-b", "exp-a"])
        self.assertEqual(len(list_reflections(1, db_path=self.db.path)), 1)

    def test_missing_table_reads_as_empty_and_is_not_created(self):
        path = str(Path(tempfile.mkdtemp()) / "bare.db")
        sqlite3.connect(path).close()
        self.assertIsNone(get_reflection("x", db_path=path))
        self.assertEqual(list_reflections(5, db_path=path), [])
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(recent_suggestions(conn), [])
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("experiment_reflections", tables)

    def test_recent_suggestions_skip_duplicates_repeats_and_cap_at_limit(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(_MINIMAL_TABLE)
        _store(conn, "old", "2026-11-01", [_suggestion("Shared idea"), _suggestion("Old only")])
        _store(conn, "new", "2026-11-05", [
            _suggestion("shared  IDEA"),
            _suggestion("Dup", duplicate_of="Existing issue"),
            *[_suggestion(f"Idea {n}") for n in range(6)],
        ])
        _store(conn, "pend", "2026-11-09", [_suggestion("Pending idea")], status="pending")
        found = recent_suggestions(conn, limit=5)
        titles = [s["title"] for s in found]
        self.assertEqual(titles, ["shared  IDEA", "Idea 0", "Idea 1", "Idea 2", "Idea 3"])
        self.assertEqual(found[0]["experiment_id"], "new")
        self.assertNotIn("duplicate_of", found[0])
        all_titles = [s["title"] for s in recent_suggestions(conn, limit=50)]
        self.assertNotIn("Dup", all_titles)
        self.assertNotIn("Pending idea", all_titles)
        self.assertEqual(sum(t.lower().split() == ["shared", "idea"] for t in all_titles), 1)
        self.assertIn("Old only", all_titles)


class ReflectionApiTests(unittest.TestCase):
    def setUp(self):
        self.db = _ReflectionDb(self)
        self.service = MagicMock()
        self.service.db_path = self.db.path

        def resolve(experiment_id):
            known = ("exp-a", "exp-b", "exp-p", "exp-none")
            match = [e for e in known if e.startswith(experiment_id)]
            if len(match) != 1:
                raise ExperimentNotFoundError(experiment_id)
            return MagicMock(id=match[0])

        self.service.resolve_experiment.side_effect = resolve
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_returns_stored_reflection(self):
        response = self.client.get("/experiments/exp-a/reflection", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["experiment_id"], "exp-a")
        self.assertEqual(body["model"], "fake-local")
        self.assertEqual(body["suggestions"][0]["title"], "Cache the planner context")
        self.assertEqual(body["suggestions"][1]["duplicate_of"], "Retry merges with current main")
        self.assertIsNone(body["error"])

    def test_404_without_reflection_pending_claim_or_unknown_experiment(self):
        for exp_id in ("exp-none", "exp-p", "nope"):
            response = self.client.get(f"/experiments/{exp_id}/reflection", headers=HEADERS)
            self.assertEqual(response.status_code, 404, exp_id)

    def test_401_without_or_with_wrong_key(self):
        for headers in ({}, {"x-api-key": "wrong"}):
            self.assertEqual(self.client.get("/experiments/exp-a/reflection", headers=headers).status_code, 401)
            self.assertEqual(self.client.get("/reflections", headers=headers).status_code, 401)

    def test_list_newest_first_with_limit(self):
        response = self.client.get("/reflections", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([r["experiment_id"] for r in body["reflections"]], ["exp-b", "exp-a"])
        self.assertEqual(body["reflections"][0]["error"], "RuntimeError: model down")
        limited = self.client.get("/reflections?limit=1", headers=HEADERS).json()
        self.assertEqual(limited["count"], 1)
        self.assertEqual(self.client.get("/reflections?limit=0", headers=HEADERS).status_code, 422)


class ReflectionMcpAndCatalogTests(unittest.TestCase):
    def setUp(self):
        self.db = _ReflectionDb(self)

    def test_tools_are_read_only(self):
        for name in ("experiment_reflection_get", "reflections_list"):
            self.assertIn(name, READ_ONLY_TOOLS)
            self.assertNotIn(name, WRITE_TOOLS)

    def test_mcp_tools_call_the_api(self):
        class FakeMcp:
            def __init__(self):
                self.tools = {}
                self.session_manager = object()

            def tool(self, **_kwargs):
                def decorate(function):
                    self.tools[function.__name__] = function
                    return function
                return decorate

            def streamable_http_app(self, **_kwargs):
                return FastAPI()

        service = MagicMock()
        service.db_path = self.db.path
        service.resolve_experiment.side_effect = (
            lambda e: MagicMock(id=e) if e in ("exp-a", "exp-none") else (_ for _ in ()).throw(ExperimentNotFoundError(e))
        )
        fake = FakeMcp()
        with (
            patch.object(nexus_mcp, "MCPServer", return_value=fake),
            patch.object(api, "_resolve_experiment_service", return_value=service),
        ):
            nexus_mcp._build_mount_app()
            got = fake.tools["experiment_reflection_get"](experiment_id="exp-a")
            listed = fake.tools["reflections_list"](limit=1)
            with self.assertRaisesRegex(ValueError, "404"):
                fake.tools["experiment_reflection_get"](experiment_id="exp-none")
            with self.assertRaisesRegex(ValueError, "invalid limit"):
                fake.tools["reflections_list"](limit=0)
        self.assertEqual(got["suggestions"][0]["title"], "Cache the planner context")
        self.assertEqual([r["experiment_id"] for r in listed["reflections"]], ["exp-b"])

    def test_operations_catalog_entries_and_handlers(self):
        experiments = MagicMock()
        experiments.resolve_experiment.side_effect = lambda e: MagicMock(id=e)
        registry = OperationsRegistry(experiment_service=experiments, db_path=self.db.path)
        self.addCleanup(registry.close)
        specs = {spec.name: spec for spec in registry.catalog()}
        for name in ("experiment_reflection", "reflections_list"):
            self.assertIn(name, specs)
            self.assertFalse(specs[name].mutates_state)
        shown = registry.execute("experiment_reflection", {"experiment_id": "exp-a"})
        self.assertTrue(shown.success)
        text = "\n".join(shown.output_lines)
        self.assertIn("Cache the planner context", text)
        self.assertIn("[duplicate of: Retry merges with current main]", text)
        none = registry.execute("experiment_reflection", {"experiment_id": "exp-none"})
        self.assertIn("no reflection stored", none.output_lines[0])
        listed = registry.execute("reflections_list", {"limit": 5})
        self.assertTrue(listed.success)
        self.assertIn("exp-b", listed.output_lines[0])


class PlannerPromptTests(unittest.TestCase):
    def test_section_lists_given_ideas_and_is_absent_without(self):
        ideas = [dict(_suggestion("Cache the planner context"), experiment_id="exp-a")]
        prompt = planning_prompt(ISSUES, reflections=ideas)
        self.assertIn(SECTION, prompt)
        self.assertIn("- Cache the planner context (from experiment exp-a): benefit: b-Cache", prompt)
        self.assertEqual(planning_prompt(ISSUES, reflections=[]), planning_prompt(ISSUES))
        self.assertEqual(planning_prompt(ISSUES, reflections=None), planning_prompt(ISSUES))

    def _prompt_from_tick(self, prepare=None) -> str:
        prompts: list[str] = []
        fx = InitiativeFixture()
        if prepare is not None:
            conn = sqlite3.connect(fx.db_path)
            prepare(conn)
            conn.close()
        svc = fx.service(
            issue_lister=lambda: ISSUES,
            planner=IssuePlanner(complete=lambda p: prompts.append(p) or "{}"),
            reflection_sweep=lambda **_: [],
        )
        self.addCleanup(svc.close)
        svc.tick()
        self.assertEqual(len(prompts), 1)
        return prompts[0]

    def test_initiative_adds_non_duplicate_suggestions(self):
        def prepare(conn):
            conn.execute(_MINIMAL_TABLE)
            _store(conn, "e1", "2026-11-01", [_suggestion("Old local-model probe")])
            _store(conn, "e2", "2026-11-05", [
                _suggestion("Measure local model latency"),
                _suggestion("Retry merges", duplicate_of="Retry merges with current main"),
                *[_suggestion(f"Idea {n}") for n in range(5)],
            ])

        prompt = self._prompt_from_tick(prepare)
        self.assertIn(SECTION, prompt)
        section = prompt.split(SECTION, 1)[1]
        self.assertIn("Measure local model latency (from experiment e2)", section)
        for n in range(4):
            self.assertIn(f"Idea {n}", section)
        self.assertNotIn("Idea 4", section)  # at most five
        self.assertNotIn("Old local-model probe", section)
        self.assertNotIn("Retry merges", section)  # marked duplicate_of

    def test_prompt_unchanged_when_table_missing_or_empty(self):
        missing = self._prompt_from_tick()
        empty = self._prompt_from_tick(lambda conn: conn.execute(_MINIMAL_TABLE))
        only_duplicates = self._prompt_from_tick(lambda conn: (
            conn.execute(_MINIMAL_TABLE),
            _store(conn, "e1", "2026-11-01", [_suggestion("Dup", duplicate_of="Open issue")]),
        ))
        self.assertNotIn(SECTION, missing)
        self.assertEqual(missing, empty)
        self.assertEqual(missing, only_duplicates)
        self.assertEqual(missing, planning_prompt(ISSUES, recent=[], budget_left=5))


if __name__ == "__main__":
    unittest.main()
