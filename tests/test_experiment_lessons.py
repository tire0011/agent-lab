"""#715: the lessons store learns from failed-then-fixed experiments and feeds
the matching lessons into the next coding run.

Covers record (exactly one lesson per failed-then-succeeded pair, with evidence
ids; failures-only / successes-only produce nothing), use (file/keyword
selection, the 3-5 cap, the conditional "Lessons from earlier runs" prompt
section, the prompt-size bound), cleanup (ineffective demotion, stale demotion,
the hard cap) and visibility (operations registry + ``GET /lessons``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AGENT_API_KEY", "test-key")

from modules import experiment_lessons
from modules.experiment_lessons import (
    INEFFECTIVE_DEMOTE_THRESHOLD,
    LESSONS_SECTION_MAX_CHARS,
    LESSONS_SECTION_TITLE,
    MAX_LESSONS_PER_PROMPT,
    MAX_LESSONS_TOTAL,
    STATUS_ACTIVE,
    STATUS_DEMOTED,
    STATUS_REMOVED,
    demote_stale_lessons,
    enforce_hard_cap,
    lessons_for_prompt,
    list_lessons,
    mark_injected,
    mark_ineffective,
    note_experiment_finished,
    phrase_pending_lessons,
    record_lesson,
    render_lessons_section,
    select_lessons,
)
from modules.run_store import init_db

FAIL_ERROR = (
    "tests failed (exit code 1): tests.test_timing.test_budget computes the "
    "token budget from time.monotonic() deltas"
)


def _db(tmp_path) -> str:
    return str(tmp_path / "lessons.db")


def _insert_experiment(
    db_path: str,
    experiment_id: str,
    *,
    status: str,
    created_at: str,
    goal: str = "GitHub Issue #715: lessons store",
    source_type: str | None = "issue",
    source_id: str | None = "715",
    error: str | None = None,
    test_exit_code: int | None = None,
    git_status: str | None = None,
    test_output: str | None = None,
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO experiments
           (id, goal, status, created_at, source_type, source_id, error,
            test_exit_code, git_status, test_output)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            experiment_id, goal, status, created_at, source_type, source_id, error,
            test_exit_code, git_status, test_output,
        ),
    )
    conn.commit()
    conn.close()


def _pair(db_path: str, *, failure_id="exp-fail", success_id="exp-ok",
          failure_status="failed", created_failure="2026-08-01T00:00:00+00:00",
          created_success="2026-08-02T00:00:00+00:00", error=FAIL_ERROR,
          git_status_failure=" M modules/timing_check.py\n?? tests/test_timing.py",
          git_status_success=" M modules/timing_check.py"):
    _insert_experiment(
        db_path, failure_id, status=failure_status, created_at=created_failure,
        error=error, test_exit_code=1 if failure_status == "failed" else None,
        git_status=git_status_failure,
    )
    _insert_experiment(
        db_path, success_id, status="succeeded", created_at=created_success,
        git_status=git_status_success,
    )


def _conn(db_path: str) -> sqlite3.Connection:
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---- record ---------------------------------------------------------------


def test_failed_then_succeeded_pair_produces_exactly_one_lesson_with_evidence(tmp_path):
    db = _db(tmp_path)
    # The failure finalizes while no success exists yet: nothing to learn.
    _insert_experiment(db, "exp-fail", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       error=FAIL_ERROR, test_exit_code=1,
                       git_status=" M modules/timing_check.py\n?? tests/test_timing.py")
    assert note_experiment_finished(db, "exp-fail") == []
    assert list_lessons(10, db_path=db) == []
    # The later success on the same issue finalizes: exactly one lesson.
    _insert_experiment(db, "exp-ok", status="succeeded", created_at="2026-08-02T00:00:00+00:00",
                       git_status=" M modules/timing_check.py")
    created = note_experiment_finished(db, "exp-ok")
    assert len(created) == 1
    lesson = created[0]
    assert lesson["failure_experiment_id"] == "exp-fail"
    assert lesson["success_experiment_id"] == "exp-ok"
    assert lesson["source_type"] == "issue" and lesson["source_id"] == "715"
    assert lesson["failure_kind"] == "test"
    assert "time.monotonic()" in lesson["cause"]
    assert lesson["rule"]  # fallback rule is the finding text until phrased
    assert lesson["phrased"] is False
    assert "modules/timing_check.py" in lesson["files"]
    assert "tests/test_timing.py" in lesson["files"]
    assert any("monotonic" in k for k in lesson["keywords"])
    assert lesson["status"] == STATUS_ACTIVE
    # Idempotent: re-notifying either side does not add a second lesson.
    assert note_experiment_finished(db, "exp-ok") == []
    assert note_experiment_finished(db, "exp-fail") == []
    stored = list_lessons(10, db_path=db)
    assert len(stored) == 1
    assert stored[0]["id"] == lesson["id"]


def test_success_noting_first_also_finds_the_later_pair_exactly_once(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    created = note_experiment_finished(db, "exp-ok")
    assert len(created) == 1
    assert note_experiment_finished(db, "exp-fail") == []
    assert len(list_lessons(10, db_path=db)) == 1


def test_only_failures_or_only_successes_produce_nothing(tmp_path):
    db = _db(tmp_path)
    _insert_experiment(db, "f1", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       error=FAIL_ERROR, test_exit_code=1)
    _insert_experiment(db, "f2", status="needs_review", created_at="2026-08-02T00:00:00+00:00",
                       error="quality evaluation: missing acceptance criteria")
    _insert_experiment(db, "f3", status="timed_out", created_at="2026-08-03T00:00:00+00:00",
                       error="implementation phase timed out")
    for experiment_id in ("f1", "f2", "f3"):
        assert note_experiment_finished(db, experiment_id) == []
    assert list_lessons(10, db_path=db) == []

    db2 = _db(tmp_path / "second")
    _insert_experiment(db2, "s1", status="succeeded", created_at="2026-08-01T00:00:00+00:00")
    _insert_experiment(db2, "s2", status="succeeded", created_at="2026-08-02T00:00:00+00:00")
    assert note_experiment_finished(db2, "s1") == []
    assert note_experiment_finished(db2, "s2") == []
    assert list_lessons(10, db_path=db2) == []


def test_pairs_are_scoped_to_the_same_source(tmp_path):
    db = _db(tmp_path)
    _pair(db, failure_id="f-715", success_id="s-715")
    assert len(note_experiment_finished(db, "s-715")) == 1
    # A failure on a different issue must not pair with issue 715's success.
    _insert_experiment(db, "f-other", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       goal="GitHub Issue #999: other work", source_id="999",
                       error=FAIL_ERROR, test_exit_code=1)
    assert note_experiment_finished(db, "f-other") == []
    lessons = list_lessons(10, db_path=db)
    assert len(lessons) == 1
    assert lessons[0]["failure_experiment_id"] == "f-715"


def test_record_lesson_phrases_rule_with_llm_when_given(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    conn = _conn(db)
    failure = dict(conn.execute("SELECT * FROM experiments WHERE id='exp-fail'").fetchone())
    success = dict(conn.execute("SELECT * FROM experiments WHERE id='exp-ok'").fetchone())

    class FakeLlm:
        model = "fake-local"

        def __call__(self, prompt: str) -> str:
            assert "time.monotonic()" in prompt
            return "Inject the clock; never compute budgets from time.monotonic() deltas."

    lesson = record_lesson(conn, failure, success, llm=FakeLlm())
    assert lesson["rule"] == "Inject the clock; never compute budgets from time.monotonic() deltas."
    assert lesson["phrased"] is True
    assert lesson["model"] == "fake-local"
    # A broken model falls back to the finding text instead of failing.
    _pair(db, failure_id="f2", success_id="s2",
          created_failure="2026-08-03T00:00:00+00:00", created_success="2026-08-04T00:00:00+00:00")

    def broken(prompt: str) -> str:
        raise RuntimeError("model down")

    failure2 = dict(conn.execute("SELECT * FROM experiments WHERE id='f2'").fetchone())
    success2 = dict(conn.execute("SELECT * FROM experiments WHERE id='s2'").fetchone())
    lesson2 = record_lesson(conn, failure2, success2, llm=broken)
    assert lesson2["rule"] == lesson2["cause"]
    assert lesson2["phrased"] is False
    conn.close()


# ---- use: selection + prompt section ---------------------------------------


def _seed_lesson(db_path: str, lesson_no: int, *, keyword: str, files: list[str] | None = None,
                 created_at: str | None = None) -> str:
    conn = _conn(db_path)
    failure = {
        "id": f"f{lesson_no}", "goal": f"issue {keyword}", "status": "failed",
        "source_type": "issue", "source_id": str(700 + lesson_no),
        "error": f"tests failed: {keyword} misuse", "test_exit_code": 1,
        "git_status": "\n".join(f" M {f}" for f in (files or [])),
    }
    success = {
        "id": f"s{lesson_no}", "goal": f"issue {keyword} fixed", "status": "succeeded",
        "source_type": "issue", "source_id": str(700 + lesson_no), "git_status": "",
    }
    lesson = record_lesson(conn, failure, success, now=created_at)
    conn.close()
    assert lesson is not None
    return lesson["id"]


def test_selection_matches_by_file_and_keyword_and_respects_cap(tmp_path):
    db = _db(tmp_path)
    file_lesson = _seed_lesson(db, 1, keyword="budget", files=["modules/timing_check.py"])
    keyword_lesson = _seed_lesson(db, 2, keyword="accounting")
    other_lesson = _seed_lesson(db, 3, keyword="unrelated")
    for n in range(4, 10):
        _seed_lesson(db, n, keyword="budget")

    conn = _conn(db)
    selected = select_lessons(
        conn, "GitHub Issue #720: token budget accounting in tests", ["modules/timing_check.py"]
    )
    ids = [lesson["id"] for lesson in selected]
    assert file_lesson in ids  # file match ranks first
    assert selected[0]["id"] == file_lesson
    assert any(i != file_lesson for i in ids)  # keyword matches are selected too
    assert other_lesson not in ids  # no keyword/file overlap -> not selected
    assert len(selected) <= MAX_LESSONS_PER_PROMPT  # the 3-5 cap holds

    # Keyword-only matching: the unique keyword picks its lesson alone.
    by_keyword = select_lessons(conn, "pure accounting review", [])
    assert [lesson["id"] for lesson in by_keyword] == [keyword_lesson]

    assert select_lessons(conn, "completely different xyzzy topic", []) == []
    limited = select_lessons(conn, "budget budget budget", [], limit=3)
    assert 1 <= len(limited) <= 3
    conn.close()


def test_prompt_section_only_when_lessons_match_and_bounded(tmp_path):
    db = _db(tmp_path)
    assert lessons_for_prompt(db, goal="anything at all") == ("", [])
    lesson_id = _seed_lesson(db, 1, keyword="monotonic", files=["modules/timing_check.py"])

    goal = "GitHub Issue #721: never use monotonic wall time in budget tests"
    section, ids = lessons_for_prompt(db, goal=goal, files=["modules/timing_check.py"],
                                      experiment_id="exp-run-1")
    assert ids == [lesson_id]
    assert LESSONS_SECTION_TITLE in section
    assert "monotonic" in section
    assert "modules/timing_check.py" in section
    assert len(section) <= LESSONS_SECTION_MAX_CHARS

    # The injection was tracked once for this experiment.
    conn = _conn(db)
    row = conn.execute(
        "SELECT injected_count, last_injected_at FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()
    assert row["injected_count"] == 1 and row["last_injected_at"]
    injections = conn.execute(
        "SELECT COUNT(*) FROM experiment_lesson_injections WHERE experiment_id='exp-run-1'"
    ).fetchone()[0]
    assert injections == 1
    conn.close()
    section_again, _ = lessons_for_prompt(db, goal=goal, experiment_id="exp-run-1")
    conn = _conn(db)
    assert conn.execute(
        "SELECT injected_count FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()[0] == 1  # still once: the same run does not count twice
    conn.close()
    assert section_again == section

    # No match -> empty section, prompt stays unchanged.
    section_none, ids_none = lessons_for_prompt(db, goal="xyzzy nothing matching here")
    assert section_none == "" and ids_none == []

    # The rendered section is bounded even with a huge rule.
    conn = _conn(db)
    conn.execute(
        "UPDATE experiment_lessons SET rule=? WHERE id=?", ("x" * 5000, lesson_id)
    )
    conn.commit()
    conn.close()
    bounded, _ = lessons_for_prompt(db, goal=goal)
    assert 0 < len(bounded) <= LESSONS_SECTION_MAX_CHARS
    assert render_lessons_section([]) == ""


def test_service_prompt_contains_section_only_when_lessons_match(tmp_path):
    from modules.experiment_service import ExperimentService

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_p.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    db = _db(tmp_path)
    service = ExperimentService(
        worker_factory=MagicMock(), db_path=db, repo_root=str(repo),
        worktrees_root=str(tmp_path / "worktrees"),
    )
    try:
        lesson_id = _seed_lesson(db, 1, keyword="monotonic", files=["modules/timing_check.py"])
        matching = service.create_experiment(
            "GitHub Issue #721: monotonic clock use in tests must be fixed"
        )
        prompt = service._with_lessons_section("BASE PROMPT", matching)
        assert prompt.startswith("BASE PROMPT\n\n")
        assert LESSONS_SECTION_TITLE in prompt
        assert "monotonic" in prompt

        unmatched = service.create_experiment("Totally unrelated goal about xyzzy")
        assert service._with_lessons_section("BASE PROMPT", unmatched) == "BASE PROMPT"
        assert lesson_id
    finally:
        service.close()


# ---- cleanup ----------------------------------------------------------------


def test_ineffective_lesson_is_demoted_after_repeated_failure(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    note_experiment_finished(db, "exp-ok")  # create the lesson
    lesson = list_lessons(10, db_path=db)[0]
    lesson_id = lesson["id"]

    conn = _conn(db)
    mark_injected(conn, [lesson_id], "exp-run-a")
    conn.close()

    # A new run got the lesson injected and failed with the same reason anyway.
    _insert_experiment(db, "exp-run-a", status="failed",
                       created_at="2026-08-05T00:00:00+00:00",
                       goal="GitHub Issue #715: lessons store", error=FAIL_ERROR,
                       test_exit_code=1, test_output=FAIL_ERROR)
    note_experiment_finished(db, "exp-run-a")
    conn = _conn(db)
    row = conn.execute(
        "SELECT ineffective_count, status FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()
    assert row["ineffective_count"] == 1
    assert row["status"] == STATUS_ACTIVE  # below the threshold

    # A different failure reason does not count against the lesson.
    _insert_experiment(db, "exp-run-b", status="failed",
                       created_at="2026-08-06T00:00:00+00:00",
                       goal="GitHub Issue #715: lessons store",
                       error="git worktree add failed: disk full", test_exit_code=None)
    conn = _conn(db)
    mark_injected(conn, [lesson_id], "exp-run-b")
    conn.close()
    note_experiment_finished(db, "exp-run-b")
    conn = _conn(db)
    assert conn.execute(
        "SELECT ineffective_count FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()[0] == 1

    # The same failure reason a second time reaches the threshold -> demoted.
    _insert_experiment(db, "exp-run-c", status="failed",
                       created_at="2026-08-07T00:00:00+00:00",
                       goal="GitHub Issue #715: lessons store", error=FAIL_ERROR,
                       test_exit_code=1, test_output=FAIL_ERROR)
    conn = _conn(db)
    mark_injected(conn, [lesson_id], "exp-run-c")
    conn.close()
    note_experiment_finished(db, "exp-run-c")
    conn = _conn(db)
    row = conn.execute(
        "SELECT ineffective_count, status FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()
    assert row["ineffective_count"] == INEFFECTIVE_DEMOTE_THRESHOLD
    assert row["status"] == STATUS_DEMOTED
    # A demoted lesson is no longer selected for prompts.
    assert lesson_id not in [l["id"] for l in select_lessons(
        conn, "monotonic budget timing tests", ["modules/timing_check.py"]
    )]
    conn.close()

    # mark_ineffective alone also demotes at the threshold.
    db2 = _db(tmp_path / "second")
    _pair(db2, failure_id="fa", success_id="sa")
    note_experiment_finished(db2, "sa")
    lesson2 = list_lessons(10, db_path=db2)[0]
    conn = _conn(db2)
    assert mark_ineffective(conn, lesson2["id"]) == STATUS_ACTIVE
    assert mark_ineffective(conn, lesson2["id"]) == STATUS_DEMOTED
    conn.close()


def test_stale_unused_lesson_is_demoted(tmp_path):
    db = _db(tmp_path)
    old_id = _seed_lesson(db, 1, keyword="stale", created_at="2020-01-01T00:00:00+00:00")
    fresh_id = _seed_lesson(db, 2, keyword="fresh", created_at="2026-06-01T00:00:00+00:00")
    conn = _conn(db)
    # recently injected -> not stale even if created long ago
    old_but_used = _seed_lesson(db, 3, keyword="used", created_at="2020-01-01T00:00:00+00:00")
    mark_injected(conn, [old_but_used], "exp-x", now="2026-06-20T00:00:00+00:00")
    demoted = demote_stale_lessons(conn, now="2026-06-25T00:00:00+00:00")
    assert demoted >= 1
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute("SELECT id, status FROM experiment_lessons").fetchall()
    }
    assert statuses[old_id] == STATUS_DEMOTED       # never used, ancient
    assert statuses[fresh_id] == STATUS_ACTIVE       # created recently
    assert statuses[old_but_used] == STATUS_ACTIVE   # used within the window
    conn.close()


def test_hard_cap_holds(tmp_path):
    db = _db(tmp_path)
    conn = _conn(db)
    # Seed more pairs than the cap; recording must keep the store bounded.
    for n in range(MAX_LESSONS_TOTAL + 10):
        failure = {
            "id": f"f{n}", "goal": f"issue {n}", "status": "failed",
            "source_type": "issue", "source_id": str(900 + n),
            "error": f"tests failed: failure number {n}", "test_exit_code": 1,
            "git_status": "",
        }
        success = {
            "id": f"s{n}", "goal": f"issue {n} fixed", "status": "succeeded",
            "source_type": "issue", "source_id": str(900 + n), "git_status": "",
        }
        assert record_lesson(conn, failure, success) is not None
    live = conn.execute(
        "SELECT COUNT(*) FROM experiment_lessons WHERE status != ?", (STATUS_REMOVED,)
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM experiment_lessons").fetchone()[0]
    assert live == MAX_LESSONS_TOTAL  # hard cap holds
    assert total == MAX_LESSONS_TOTAL + 10  # evicted rows are kept as 'removed'
    # Explicit cap enforcement removes the least valuable rows.
    removed = enforce_hard_cap(conn, cap=5, now="2026-01-01T00:00:00+00:00")
    assert removed == MAX_LESSONS_TOTAL - 5
    live = conn.execute(
        "SELECT COUNT(*) FROM experiment_lessons WHERE status != ?", (STATUS_REMOVED,)
    ).fetchone()[0]
    assert live == 5
    conn.close()


# ---- phrasing sweep ----------------------------------------------------------


def test_phrase_pending_lessons_uses_llm_and_falls_back(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    note_experiment_finished(db, "exp-ok")
    lesson = list_lessons(10, db_path=db)[0]
    assert lesson["phrased"] is False

    class FakeLlm:
        model = "fake-local"

        def __call__(self, prompt: str) -> str:
            return "- \"Inject the clock; never use time.monotonic() in tests.\""

    phrased = phrase_pending_lessons(3, llm=FakeLlm(), db_path=db)
    assert phrased == [lesson["id"]]
    updated = list_lessons(10, db_path=db)[0]
    assert updated["phrased"] is True
    assert updated["rule"] == "Inject the clock; never use time.monotonic() in tests."
    assert updated["model"] == "fake-local"
    # Already phrased -> the sweep has nothing left to do.
    assert phrase_pending_lessons(3, llm=FakeLlm(), db_path=db) == []

    # A failing model keeps the fallback rule and does not raise.
    _pair(db, failure_id="f2", success_id="s2",
          created_failure="2026-08-03T00:00:00+00:00",
          created_success="2026-08-04T00:00:00+00:00")
    note_experiment_finished(db, "s2")
    fallback = [l for l in list_lessons(10, db_path=db) if l["failure_experiment_id"] == "f2"][0]
    assert fallback["phrased"] is False

    def broken(prompt: str) -> str:
        raise RuntimeError("llama-server down")

    assert phrase_pending_lessons(3, llm=broken, db_path=db) == []
    kept = [l for l in list_lessons(10, db_path=db) if l["failure_experiment_id"] == "f2"][0]
    assert kept["phrased"] is False
    assert kept["rule"] == kept["cause"]


# ---- visibility: operations registry + API ------------------------------------


class _LessonsVisibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        _pair(self.db_path)
        note_experiment_finished(self.db_path, "exp-ok")
        self.lesson = list_lessons(10, db_path=self.db_path)[0]

    def test_operations_registry_lists_lessons(self):
        from modules.operations_registry import OperationsRegistry

        registry = OperationsRegistry(experiment_service=MagicMock(), db_path=self.db_path)
        self.addCleanup(registry.close)
        specs = {spec.name: spec for spec in registry.catalog()}
        assert "lessons_list" in specs
        assert specs["lessons_list"].mutates_state is False
        result = registry.execute("lessons_list", {"limit": 5})
        assert result.success
        text = "\n".join(result.output_lines)
        assert self.lesson["id"][:12] in text
        assert "time.monotonic()" in text
        assert "exp-fail" in text and "exp-ok" in text
        with self.assertRaises(Exception):
            registry.execute("lessons_list", {"limit": 0})

    def test_api_lists_lessons(self):
        from fastapi.testclient import TestClient

        import api

        service = MagicMock()
        service.db_path = self.db_path
        api.app.dependency_overrides[api.get_experiment_service] = lambda: service
        self.addCleanup(api.app.dependency_overrides.clear)
        client = TestClient(api.app)
        headers = {"x-api-key": "test-key"}

        assert client.get("/lessons").status_code == 401
        assert client.get("/lessons", headers={"x-api-key": "wrong"}).status_code == 401
        response = client.get("/lessons", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        lesson = body["lessons"][0]
        assert lesson["id"] == self.lesson["id"]
        assert lesson["failure_experiment_id"] == "exp-fail"
        assert lesson["success_experiment_id"] == "exp-ok"
        assert lesson["status"] == STATUS_ACTIVE
        assert "time.monotonic()" in lesson["rule"]
        limited = client.get("/lessons?limit=1", headers=headers).json()
        assert limited["count"] == 1
        assert client.get("/lessons?limit=0", headers=headers).status_code == 422


# ---- end-to-end through the experiment pipeline --------------------------------


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tests").mkdir()
    (path / "tests" / "test_placeholder.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _service(tmp_path: Path, captured_tasks: list[str]):
    from modules.codex_worker import CodexResult
    from modules.experiment_service import ExperimentService

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    worker = MagicMock()

    def run(*args, **kwargs):
        captured_tasks.append(kwargs.get("task") or "")
        target = Path(kwargs["working_dir"]) / "stub.py"
        if not target.exists():
            target.write_text('"""Stub."""\n')
        return CodexResult(
            success=True, exit_code=0, stdout="", stderr="", runtime_ms=5,
            timed_out=False, stdout_truncated=False, stderr_truncated=False,
            thread_id="t", final_message="done",
            usage={"input_tokens": 1, "output_tokens": 1},
            task=kwargs.get("task") or "", working_dir=kwargs.get("working_dir", ""),
            sandbox="workspace-write", error=None,
        )

    worker.run.side_effect = run
    service = ExperimentService(
        worker_factory=lambda worktree_path: worker,
        db_path=str(tmp_path / "agent.db"),
        repo_root=str(repo),
        worktrees_root=str(tmp_path / "worktrees"),
    )
    service.test_python_executable = sys.executable
    return service


def test_run_experiment_records_lesson_and_next_prompt_gets_the_section(tmp_path):
    captured_tasks: list[str] = []
    service = _service(tmp_path, captured_tasks)
    db = service.db_path
    try:
        # A failed attempt on issue #999 exists before the successful run.
        failed = service.create_experiment("GitHub Issue #999: fix monotonic budget test")
        service._force_status(
            failed.id, "failed", ended_at="2026-08-01T00:00:00+00:00",
            error=FAIL_ERROR, test_exit_code=1,
        )
        assert list_lessons(10, db_path=db) == []

        # The retry on the same issue succeeds; the run's own finalization
        # records exactly one lesson with both evidence ids.
        retry = service.create_experiment(
            "GitHub Issue #999: fix the monotonic budget check"
        )
        result = service.run_experiment(retry.id)
        assert result.status == "succeeded", result.error
        lessons = list_lessons(10, db_path=db)
        assert len(lessons) == 1
        assert lessons[0]["failure_experiment_id"] == failed.id
        assert lessons[0]["success_experiment_id"] == retry.id
        assert lessons[0]["source_id"] == "999"
        # The implementation prompt of that run could not contain the lesson
        # yet (it was created by this very run).
        assert LESSONS_SECTION_TITLE not in captured_tasks[0]

        # The next coding run on a matching issue gets the section injected
        # (keyword match on the goal; no plan files needed).
        captured_tasks.clear()
        followup = service.create_experiment(
            "GitHub Issue #1000: monotonic budget accounting in the timing check"
        )
        result2 = service.run_experiment(followup.id)
        assert result2.status == "succeeded", result2.error
        assert any(LESSONS_SECTION_TITLE in task for task in captured_tasks)
        task_with_section = next(t for t in captured_tasks if LESSONS_SECTION_TITLE in t)
        assert "monotonic" in task_with_section
        # Prompt budget: the section is bounded.
        section = task_with_section.split(LESSONS_SECTION_TITLE, 1)[1]
        assert len(section) <= LESSONS_SECTION_MAX_CHARS + len(LESSONS_SECTION_TITLE)
    finally:
        service.close()
