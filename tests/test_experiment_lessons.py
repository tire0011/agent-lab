"""#715: the lessons store learns from failed-then-fixed experiments and feeds
the matching lessons into the next coding run.

Covers record (exactly one lesson per failed-then-succeeded pair, with evidence
ids, including review-blocked adoptions; failures-only / successes-only produce
nothing), use (file/keyword token selection, the 3-5 cap, the conditional
"Lessons from earlier runs" prompt section fitted whole-entry into the prompt
budget, tracking only the entries actually sent), cleanup (signature-based
ineffective demotion that is replay-safe, stale demotion, the atomic hard cap
with bounded evicted-pair history) and visibility (operations registry +
``GET /lessons``). It also covers the crash/interruption cases: a step that
fails halfway or runs twice leaves no partial state.
"""

from __future__ import annotations

import json
import os
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
    EVICTED_PAIRS_MAX,
    INEFFECTIVE_DEMOTE_THRESHOLD,
    LESSONS_SECTION_MAX_CHARS,
    LESSONS_SECTION_TITLE,
    MAX_LESSONS_PER_PROMPT,
    MAX_LESSONS_TOTAL,
    STATUS_ACTIVE,
    STATUS_DEMOTED,
    STATUS_REMOVED,
    _changed_files,
    _goal_files,
    _signatures_match,
    _signature,
    demote_stale_lessons,
    enforce_hard_cap,
    failure_recurrence_stats,
    lessons_for_prompt,
    list_lessons,
    mark_injected,
    mark_injected_for,
    mark_ineffective,
    note_adoption_outcome,
    note_experiment_finished,
    phrase_pending_lessons,
    record_failure_event,
    record_lesson,
    render_lessons_section,
    select_lessons,
)
from modules.run_store import init_db

FAIL_ERROR = (
    "tests failed (exit code 1): tests.test_timing.test_budget computes the "
    "token budget from time.monotonic() deltas"
)
FAIL_OUTPUT = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_timing.py::TestBudget::test_budget - AssertionError: budget 12 != 30\n"
    "                    assert 12 == 30\n"
    "========================= 1 failed, 20 passed in 3.21s ========================="
)
REVIEW_BLOCK = (
    "Review by codex blocked the merge: Blocking problems: (1) tests must not "
    "compute the budget from wall clock in modules/timing_check.py"
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
    test_output: str | None = None,
    git_status: str | None = None,
    diff_stat: str | None = None,
    diff_text: str | None = None,
    quality_evaluation_json: str | None = None,
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO experiments
           (id, goal, status, created_at, source_type, source_id, error,
            test_exit_code, test_output, git_status, diff_stat, diff_text,
            quality_evaluation_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            experiment_id, goal, status, created_at, source_type, source_id, error,
            test_exit_code, test_output, git_status, diff_stat, diff_text,
            quality_evaluation_json,
        ),
    )
    conn.commit()
    conn.close()


def _insert_adoption(
    db_path: str,
    experiment_id: str,
    *,
    status: str,
    reason: str | None = None,
    created_at: str = "2026-08-02T12:00:00+00:00",
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO experiment_adoptions
           (id, experiment_id, status, reason, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (f"adopt-{experiment_id}", experiment_id, status, reason, created_at, created_at),
    )
    conn.commit()
    conn.close()


def _insert_verification_run(
    db_path: str,
    experiment_id: str,
    iteration: int,
    *,
    status: str = "failed",
    exit_code: int | None = 1,
    error: str | None = None,
    output: str | None = None,
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO verification_runs
           (experiment_id, iteration, started_at, ended_at, status, exit_code, output, error)
           VALUES (?, ?, '2026-08-01T00:00:00+00:00', '2026-08-01T00:01:00+00:00', ?, ?, ?, ?)""",
        (experiment_id, iteration, status, exit_code, output, error),
    )
    conn.commit()
    conn.close()


def _pair(db_path: str, *, failure_id="exp-fail", success_id="exp-ok",
          failure_status="failed", created_failure="2026-08-01T00:00:00+00:00",
          created_success="2026-08-02T00:00:00+00:00", error=FAIL_ERROR,
          git_status_failure=" M modules/timing_check.py\n?? tests/test_timing.py",
          git_status_success=" M modules/timing_check.py",
          test_output=FAIL_OUTPUT):
    _insert_experiment(
        db_path, failure_id, status=failure_status, created_at=created_failure,
        error=error, test_exit_code=1 if failure_status == "failed" else None,
        test_output=test_output, git_status=git_status_failure,
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
                       error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT,
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
    # The cause carries the stored diagnostics, not just "exit code 1".
    assert "time.monotonic()" in lesson["cause"]
    assert "test_budget" in lesson["cause"]  # from the test output tail
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


def test_record_lesson_is_idempotent_when_called_twice(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    conn = _conn(db)
    failure = dict(conn.execute("SELECT * FROM experiments WHERE id='exp-fail'").fetchone())
    success = dict(conn.execute("SELECT * FROM experiments WHERE id='exp-ok'").fetchone())
    first = record_lesson(conn, failure, success)
    second = record_lesson(conn, failure, success)
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM experiment_lessons").fetchone()[0] == 1
    conn.close()


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
                       error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
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
                       error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
    assert note_experiment_finished(db, "f-other") == []
    lessons = list_lessons(10, db_path=db)
    assert len(lessons) == 1
    assert lessons[0]["failure_experiment_id"] == "f-715"


def test_missing_or_nonterminal_experiment_produces_nothing(tmp_path):
    db = _db(tmp_path)
    assert note_experiment_finished(db, "does-not-exist") == []
    assert note_adoption_outcome(db, "does-not-exist") == []
    _insert_experiment(db, "active", status="queued", created_at="2026-08-01T00:00:00+00:00")
    assert note_experiment_finished(db, "active") == []
    assert note_adoption_outcome(db, "active") == []
    assert list_lessons(10, db_path=db) == []


# ---- adoption / review-blocked outcomes (review blocker 1) -------------------


def test_review_blocked_adoption_is_a_failure_and_pairs_with_a_later_adopted_success(tmp_path):
    db = _db(tmp_path)
    # The experiment row says 'succeeded', but the review blocked the merge.
    _insert_experiment(db, "exp-blocked", status="succeeded",
                       created_at="2026-08-01T00:00:00+00:00",
                       git_status=" M modules/timing_check.py")
    _insert_adoption(db, "exp-blocked", status="failed", reason=REVIEW_BLOCK)
    assert note_experiment_finished(db, "exp-blocked") == []
    assert list_lessons(10, db_path=db) == []
    # A later attempt on the same issue is adopted: exactly one lesson, with the
    # review finding as cause and kind 'review'.
    _insert_experiment(db, "exp-ok", status="succeeded",
                       created_at="2026-08-02T00:00:00+00:00",
                       git_status=" M modules/timing_check.py")
    _insert_adoption(db, "exp-ok", status="adopted")
    created = note_adoption_outcome(db, "exp-ok")
    assert len(created) == 1
    lesson = created[0]
    assert lesson["failure_experiment_id"] == "exp-blocked"
    assert lesson["success_experiment_id"] == "exp-ok"
    assert lesson["failure_kind"] == "review"
    assert "blocked the merge" in lesson["cause"]
    assert "modules/timing_check.py" in lesson["files"]
    # Replay does not duplicate.
    assert note_adoption_outcome(db, "exp-ok") == []
    assert len(list_lessons(10, db_path=db)) == 1


def test_review_blocked_attempt_is_not_counted_as_a_success(tmp_path):
    db = _db(tmp_path)
    _insert_experiment(db, "exp-fail", status="failed",
                       created_at="2026-08-01T00:00:00+00:00",
                       error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
    _insert_experiment(db, "exp-blocked", status="succeeded",
                       created_at="2026-08-01T12:00:00+00:00")
    _insert_adoption(db, "exp-blocked", status="needs_review", reason=REVIEW_BLOCK)
    # The review-blocked run finalizes: it is a failure without a later success.
    assert note_adoption_outcome(db, "exp-blocked") == []
    assert list_lessons(10, db_path=db) == []
    # The real fix arrives later and is adopted: both failures pair with it,
    # never with the review-blocked run as the success.
    _insert_experiment(db, "exp-ok", status="succeeded",
                       created_at="2026-08-03T00:00:00+00:00")
    _insert_adoption(db, "exp-ok", status="adopted")
    created = note_experiment_finished(db, "exp-ok")
    pairs = {(l["failure_experiment_id"], l["success_experiment_id"]) for l in created}
    assert pairs == {("exp-fail", "exp-ok"), ("exp-blocked", "exp-ok")}
    for lesson in list_lessons(10, db_path=db):
        assert lesson["success_experiment_id"] != "exp-blocked"


def test_lesson_is_demoted_when_its_success_later_fails_at_adoption(tmp_path):
    db = _db(tmp_path)
    _pair(db, failure_id="exp-fail", success_id="exp-ok")
    created = note_experiment_finished(db, "exp-ok")
    assert len(created) == 1
    lesson_id = created[0]["id"]
    # The "success" is later review-blocked: the lesson did not hold.
    _insert_adoption(db, "exp-ok", status="failed", reason=REVIEW_BLOCK,
                     created_at="2026-08-05T00:00:00+00:00")
    assert note_adoption_outcome(db, "exp-ok") == []
    lesson = list_lessons(10, db_path=db)[0]
    assert lesson["id"] == lesson_id
    assert lesson["status"] == STATUS_DEMOTED
    # A demoted lesson is no longer selected.
    conn = _conn(db)
    assert select_lessons(conn, "monotonic budget timing tests",
                          ["modules/timing_check.py"]) == []
    conn.close()


# ---- evidence: diagnostics, fix description, files, sanitization ------------


def test_cause_uses_stored_diagnostics_not_generic_exit_message(tmp_path):
    db = _db(tmp_path)
    _insert_experiment(
        db, "f", status="failed", created_at="2026-08-01T00:00:00+00:00",
        error="tests failed (exit code 1)", test_exit_code=1, test_output=FAIL_OUTPUT,
    )
    _insert_experiment(db, "s", status="succeeded", created_at="2026-08-02T00:00:00+00:00")
    created = note_experiment_finished(db, "s")
    assert len(created) == 1
    cause = created[0]["cause"]
    assert cause != "tests failed (exit code 1)"
    assert "test_budget" in cause and "1 failed, 20 passed" in cause


def test_phrasing_prompt_contains_bounded_fix_description(tmp_path):
    db = _db(tmp_path)
    _insert_experiment(
        db, "f", status="failed", created_at="2026-08-01T00:00:00+00:00",
        error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT,
        git_status=" M modules/timing_check.py",
    )
    _insert_experiment(
        db, "s", status="succeeded", created_at="2026-08-02T00:00:00+00:00",
        git_status=" M modules/timing_check.py",
        diff_stat=" modules/timing_check.py | 6 ++++--\n 1 file changed, 4 insertions(+), 2 deletions(-)\n",
        diff_text="--- a/modules/timing_check.py\n+++ b/modules/timing_check.py\n@@ -1 +1 @@\n-budget = now() - start\n+budget = injected_clock() - start\n",
    )
    conn = _conn(db)
    failure = dict(conn.execute("SELECT * FROM experiments WHERE id='f'").fetchone())
    success = dict(conn.execute("SELECT * FROM experiments WHERE id='s'").fetchone())
    seen: list[str] = []

    class FakeLlm:
        model = "fake-local"

        def __call__(self, prompt: str) -> str:
            seen.append(prompt)
            return "Inject the clock; never compute budgets from time.monotonic() deltas."

    lesson = record_lesson(conn, failure, success, llm=FakeLlm())
    assert lesson["phrased"] is True
    prompt = seen[0]
    # The model sees what the successful attempt changed, bounded.
    assert "modules/timing_check.py" in prompt
    assert "injected_clock" in prompt
    assert "1 file changed" in prompt
    assert len(prompt) < 4000
    conn.close()


def test_record_lesson_phrases_rule_with_llm_and_falls_back_on_error(tmp_path):
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

    _pair(db, failure_id="f2", success_id="s2",
          created_failure="2026-08-03T00:00:00+00:00", created_success="2026-08-04T00:00:00+00:00")

    def broken(prompt: str) -> str:
        raise RuntimeError("model down")

    failure2 = dict(conn.execute("SELECT * FROM experiments WHERE id='f2'").fetchone())
    success2 = dict(conn.execute("SELECT * FROM experiments WHERE id='s2'").fetchone())
    lesson2 = record_lesson(conn, failure2, success2, llm=broken)
    # sanitized fallback rule (bounded copy of the cause), not an exception
    assert lesson2["rule"] == lesson2["cause"][:experiment_lessons.MAX_RULE_CHARS]
    assert lesson2["phrased"] is False
    conn.close()


def test_changed_files_parses_stat_numstat_and_porcelain(tmp_path):
    # verification stores `git diff --cached --stat HEAD` output, not numstat.
    row = {
        "git_status": " M modules/timing_check.py\n?? tests/test_timing.py\nR  old.py -> new.py\n",
        "diff_stat": (
            " modules/api.py | 12 ++++---\n"
            " tests/test_api.py | 3 ++\n"
            " 2 files changed, 15 insertions(+), 3 deletions(-)\n"
        ),
    }
    files = _changed_files(row, use_git=False)
    assert "modules/timing_check.py" in files
    assert "tests/test_timing.py" in files
    assert "new.py" in files and "old.py" not in files
    assert "modules/api.py" in files          # --stat format
    assert "tests/test_api.py" in files
    assert not any("file changed" in f for f in files)  # summary line skipped
    # tab-separated numstat still parses too
    numstat = {"git_status": "", "diff_stat": "4\t2\tmodules/x.py\n0\t1\ttests/test_y.py\n"}
    assert _changed_files(numstat, use_git=False) == ["modules/x.py", "tests/test_y.py"]


def test_goal_files_extracts_paths_from_issue_text():
    goal = ("GitHub Issue #720: fix `modules/timing_check.py` and see "
            "modules/experiment_lessons.py for the store")
    files = _goal_files(goal)
    assert "modules/timing_check.py" in files
    assert "modules/experiment_lessons.py" in files


def test_evidence_is_redacted_before_storage_and_keywords(tmp_path):
    db = _db(tmp_path)
    secret_error = FAIL_ERROR + " with AGENT_API_KEY=sk-abc123def456ghi789 and token: hunter2hunter2"
    _insert_experiment(db, "f", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       error=secret_error, test_exit_code=1, test_output=FAIL_OUTPUT,
                       git_status="/abs/path/secret M modules/timing_check.py")
    _insert_experiment(db, "s", status="succeeded", created_at="2026-08-02T00:00:00+00:00")
    created = note_experiment_finished(db, "s")
    lesson = created[0]
    assert "sk-abc123def456ghi789" not in lesson["cause"]
    assert "hunter2hunter2" not in lesson["cause"]
    assert "REDACTED" in lesson["cause"]
    assert "sk-abc123def456ghi789" not in lesson["rule"]
    assert all("sk-abc123" not in k and "hunter2" not in k for k in lesson["keywords"])
    assert all("/abs/path" not in f for f in lesson["files"])


# ---- use: selection + prompt section ---------------------------------------


def _seed_lesson(db_path: str, lesson_no: int, *, keyword: str, files: list[str] | None = None,
                 created_at: str | None = None, goal: str | None = None,
                 error: str | None = None) -> str:
    conn = _conn(db_path)
    failure = {
        "id": f"f{lesson_no}", "goal": goal or f"issue {keyword}", "status": "failed",
        "source_type": "issue", "source_id": str(700 + lesson_no),
        "error": error or f"tests failed: {keyword} misuse", "test_exit_code": 1,
        "test_output": f"FAILED tests/test_x.py - {keyword} broke\n",
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


def test_two_unrelated_issue_headings_do_not_match(tmp_path):
    db = _db(tmp_path)
    # A lesson whose only shared words with the query are boilerplate.
    _seed_lesson(
        db, 1, keyword="worktree",
        goal="GitHub Issue #715: lessons store for failed-then-fixed experiments",
        error="tests failed: worktree quota exceeded during the adoption pipeline",
    )
    conn = _conn(db)
    unrelated = select_lessons(
        conn, "GitHub Issue #842: weekly cost report currency formatting", []
    )
    assert unrelated == []  # "github"/"issue" boilerplate must not match
    # Substring is not a match either: 'budget' does not match 'budgeting'.
    _seed_lesson(db, 2, keyword="budget")
    assert select_lessons(conn, "the budgeting approach changed", []) == []
    # But a real token match still works, including inside dotted names.
    assert select_lessons(conn, "never use time.monotonic() here", []) == []
    _seed_lesson(db, 3, keyword="monotonic")
    matched = select_lessons(conn, "wall clock via time.monotonic() is wrong", [])
    assert len(matched) == 1
    conn.close()


def test_selection_uses_goal_paths_and_retry_context_files(tmp_path):
    db = _db(tmp_path)
    lesson_id = _seed_lesson(db, 1, keyword="monotonic", files=["modules/timing_check.py"])
    # An earlier experiment on the same issue touched the file (retry context).
    _insert_experiment(db, "prior", status="failed", created_at="2026-07-01T00:00:00+00:00",
                       source_id="720", goal="GitHub Issue #720: timing check",
                       git_status=" M modules/timing_check.py")
    _insert_experiment(db, "current", status="queued", created_at="2026-08-01T00:00:00+00:00",
                       source_id="720", goal="GitHub Issue #720: more timing work")
    # No plan and no explicit files: the issue text path and the prior
    # experiment's changed file both drive the match.
    section, ids = lessons_for_prompt(
        db, goal="GitHub Issue #720: more timing work in modules/timing_check.py",
        experiment_id="current",
    )
    assert ids == [lesson_id]
    assert LESSONS_SECTION_TITLE in section
    # With the retry context the prior experiment's file still matches even
    # when the new goal has no path and no keyword.
    section3, ids3 = lessons_for_prompt(db, goal="xyzzy unrelated words here",
                                        experiment_id="current")
    assert ids3 == [lesson_id]
    # Without any context (no experiment, no files, no keywords): no section.
    section2, ids2 = lessons_for_prompt(db, goal="xyzzy unrelated words here")
    assert section2 == "" and ids2 == []


def test_render_fits_complete_entries_and_marks_only_what_fits(tmp_path):
    db = _db(tmp_path)
    ids = []
    for n in range(1, 7):
        ids.append(_seed_lesson(db, n, keyword=f"topic{n}", error=f"failure reason topic{n} " + "x" * 200))
    conn = _conn(db)
    lessons = select_lessons(conn, "topic1 topic2 topic3 topic4 topic5 topic6", [])
    assert len(lessons) == MAX_LESSONS_PER_PROMPT
    # A small budget fits only whole entries; the section is never cut mid-entry.
    section, sent = render_lessons_section(lessons, max_chars=400)
    assert 0 < len(section) <= 400
    assert 1 <= len(sent) < len(lessons)
    assert section.count("\n- ") == len(sent)
    assert not section.endswith("...")
    # The ids returned are exactly the rendered ones.
    _s, sent_ids = lessons_for_prompt(db, goal="topic1 topic2 topic3 topic4 topic5 topic6",
                                      max_chars=400)
    assert sent_ids == [l["id"] for l in sent]
    # Nothing fits at all -> empty section.
    assert render_lessons_section(lessons, max_chars=10) == ("", [])
    assert render_lessons_section([]) == ("", [])
    conn.close()


def test_lessons_for_prompt_does_not_mark_and_marks_only_sent(tmp_path):
    db = _db(tmp_path)
    lesson_id = _seed_lesson(db, 1, keyword="monotonic", files=["modules/timing_check.py"])
    goal = "GitHub Issue #721: never use monotonic wall time in budget tests"
    section, ids = lessons_for_prompt(db, goal=goal, files=["modules/timing_check.py"])
    assert ids == [lesson_id]
    assert LESSONS_SECTION_TITLE in section
    assert "monotonic" in section
    assert "modules/timing_check.py" in section
    assert len(section) <= LESSONS_SECTION_MAX_CHARS
    # Selection alone does not track an injection.
    conn = _conn(db)
    assert conn.execute("SELECT COUNT(*) FROM experiment_lesson_injections").fetchone()[0] == 0
    assert conn.execute(
        "SELECT injected_count FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()[0] == 0
    conn.close()
    # The caller marks what was actually sent.
    mark_injected_for(db, ids, "exp-run-1")
    conn = _conn(db)
    row = conn.execute(
        "SELECT injected_count, last_injected_at FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()
    assert row["injected_count"] == 1 and row["last_injected_at"]
    assert conn.execute(
        "SELECT COUNT(*) FROM experiment_lesson_injections WHERE experiment_id='exp-run-1'"
    ).fetchone()[0] == 1
    conn.close()
    # Marking twice (a replayed dispatch) still counts once.
    mark_injected_for(db, ids, "exp-run-1")
    conn = _conn(db)
    assert conn.execute(
        "SELECT injected_count FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()[0] == 1
    conn.close()

    # No match -> empty section, prompt stays unchanged.
    section_none, ids_none = lessons_for_prompt(db, goal="xyzzy nothing matching here")
    assert section_none == "" and ids_none == []

    # The rendered section is bounded even with a huge stored rule.
    conn = _conn(db)
    conn.execute("UPDATE experiment_lessons SET rule=? WHERE id=?", ("x" * 5000, lesson_id))
    conn.commit()
    conn.close()
    bounded, sent = lessons_for_prompt(db, goal=goal)
    assert 0 < len(bounded) <= LESSONS_SECTION_MAX_CHARS
    assert sent == [lesson_id]


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
        _seed_lesson(db, 1, keyword="monotonic", files=["modules/timing_check.py"])
        matching = service.create_experiment(
            "GitHub Issue #721: monotonic clock use in tests must be fixed"
        )
        prompt, ids = service._with_lessons_section("BASE PROMPT", matching)
        assert prompt.startswith("BASE PROMPT\n\n")
        assert LESSONS_SECTION_TITLE in prompt
        assert "monotonic" in prompt
        assert len(prompt) - len("BASE PROMPT\n\n") <= LESSONS_SECTION_MAX_CHARS
        assert len(ids) == 1

        unmatched = service.create_experiment("Totally unrelated goal about xyzzy")
        prompt2, ids2 = service._with_lessons_section("BASE PROMPT", unmatched)
        assert prompt2 == "BASE PROMPT" and ids2 == []
    finally:
        service.close()


def test_service_drops_lessons_section_rejected_by_goal_policy(tmp_path):
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
        # A stored rule that the coding safety checks reject (untrusted
        # advisory text must stay subject to them).
        _seed_lesson(db, 1, keyword="destructive",
                     error="failure: run rm -rf / on the workspace to clean up")
        experiment = service.create_experiment(
            "GitHub Issue #722: destructive cleanup rule test"
        )
        prompt, ids = service._with_lessons_section("BASE PROMPT", experiment)
        assert prompt == "BASE PROMPT" and ids == []
    finally:
        service.close()


def test_timing_repair_prompt_includes_lessons_section(tmp_path):
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
        lesson_id = _seed_lesson(db, 1, keyword="monotonic", files=["tests/test_timing.py"])
        experiment = service.create_experiment("GitHub Issue #730: drift-only test failure")
        task, ids = service._timing_repair_prompt(
            experiment, ["tests/test_timing.py::test_budget"], {"tests/test_timing.py::test_budget": "tail"},
            "250", ["tests/test_timing.py"], ("tests/test_timing.py",),
        )
        assert LESSONS_SECTION_TITLE in task
        assert "monotonic" in task
        assert ids == [lesson_id]
    finally:
        service.close()


def test_local_review_fix_task_includes_lessons_section(tmp_path):
    from modules.experiment_service import ExperimentService
    from modules.local_review_fix import _review_fix_task

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
        experiment = service.create_experiment("GitHub Issue #731: review blocked on wall clock")
        goal = "Fix every blocking review finding.\n(1) modules/timing_check.py must not use monotonic wall time"
        task, ids = _review_fix_task(
            service, experiment, goal, REVIEW_BLOCK, ["modules/timing_check.py"], "diff-text",
        )
        assert LESSONS_SECTION_TITLE in task
        assert "monotonic" in task
        assert ids == [lesson_id]
    finally:
        service.close()


# ---- cleanup ----------------------------------------------------------------


def test_ineffective_lesson_is_demoted_after_repeated_signature_and_replay_is_safe(tmp_path):
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
                       test_exit_code=1, test_output=FAIL_OUTPUT)
    _insert_verification_run(db, "exp-run-a", 1, error=FAIL_ERROR, output=FAIL_OUTPUT)
    note_experiment_finished(db, "exp-run-a")
    # Replaying finalization must not count the same event twice.
    note_experiment_finished(db, "exp-run-a")
    conn = _conn(db)
    row = conn.execute(
        "SELECT ineffective_count, status FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()
    assert row["ineffective_count"] == 1
    assert row["status"] == STATUS_ACTIVE  # below the threshold
    events = conn.execute(
        "SELECT COUNT(*) FROM experiment_lesson_events WHERE lesson_id=? AND experiment_id='exp-run-a'",
        (lesson_id,),
    ).fetchone()[0]
    assert events == 1  # one event row, not one per replay or per source row
    conn.close()

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
    conn.close()

    # The same failure reason a second time reaches the threshold -> demoted.
    _insert_experiment(db, "exp-run-c", status="failed",
                       created_at="2026-08-07T00:00:00+00:00",
                       goal="GitHub Issue #715: lessons store", error=FAIL_ERROR,
                       test_exit_code=1, test_output=FAIL_OUTPUT)
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


def test_failed_verification_repaired_within_same_experiment_is_counted(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    note_experiment_finished(db, "exp-ok")
    lesson = list_lessons(10, db_path=db)[0]
    lesson_id = lesson["id"]
    conn = _conn(db)
    mark_injected(conn, [lesson_id], "exp-repaired")
    conn.close()
    # The experiment itself succeeded, but an early verification iteration
    # failed with the same reason the lesson warns about - it still counts.
    _insert_experiment(db, "exp-repaired", status="succeeded",
                       created_at="2026-08-05T00:00:00+00:00",
                       goal="GitHub Issue #715: lessons store")
    _insert_verification_run(db, "exp-repaired", 1, error=FAIL_ERROR, output=FAIL_OUTPUT)
    _insert_verification_run(db, "exp-repaired", 2, status="succeeded", exit_code=0)
    note_experiment_finished(db, "exp-repaired")
    note_experiment_finished(db, "exp-repaired")  # replay
    conn = _conn(db)
    assert conn.execute(
        "SELECT ineffective_count FROM experiment_lessons WHERE id=?", (lesson_id,)
    ).fetchone()[0] == 1
    conn.close()


def test_signature_matching_rules():
    # identical short reasons match even with few qualifying words
    assert _signatures_match(_signature("git worktree add failed: disk full"),
                             _signature("git worktree add failed: disk full"))
    # generic exit message leaves an empty signature and never matches
    assert _signature("tests failed (exit code 1)") == ""
    assert not _signatures_match(_signature("tests failed (exit code 1)"), _signature("x"))
    # a short repeat contained in the longer cause matches
    cause = _signature(FAIL_ERROR)
    assert _signatures_match(cause, _signature("test_budget computes the token budget from time.monotonic() deltas"))
    # unrelated reasons do not match
    assert not _signatures_match(cause, _signature("git worktree add failed: disk full"))
    # generic overlap alone is not a match
    assert not _signatures_match(_signature("assertion failed in module alpha"),
                                 _signature("assertion failed in module beta"))


def test_record_failure_event_is_atomic_and_replay_safe(tmp_path):
    db = _db(tmp_path)
    _pair(db)
    note_experiment_finished(db, "exp-ok")
    lesson = list_lessons(10, db_path=db)[0]
    conn = _conn(db)
    sig = _signature(FAIL_ERROR)
    sig2 = _signature(FAIL_ERROR + "\nextra detail line from the same failure")
    assert sig != sig2  # two distinct events of the same failure
    assert record_failure_event(conn, lesson["id"], "exp-x", "test", sig) == STATUS_ACTIVE
    assert record_failure_event(conn, lesson["id"], "exp-x", "test", sig) == STATUS_ACTIVE
    row = conn.execute(
        "SELECT ineffective_count FROM experiment_lessons WHERE id=?", (lesson["id"],)
    ).fetchone()
    assert row["ineffective_count"] == 1  # replayed insert did not recount
    assert record_failure_event(conn, lesson["id"], "exp-x", "test", sig2) == STATUS_DEMOTED
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


def test_hard_cap_holds_and_eviction_cleans_dependent_rows(tmp_path):
    db = _db(tmp_path)
    conn = _conn(db)
    # Seed more pairs than the cap; recording must keep the store bounded.
    for n in range(MAX_LESSONS_TOTAL + 10):
        failure = {
            "id": f"f{n}", "goal": f"issue {n}", "status": "failed",
            "source_type": "issue", "source_id": str(900 + n),
            "error": f"tests failed: failure number {n}", "test_exit_code": 1,
            "test_output": f"FAILED tests/test_{n}.py\n",
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
    assert total == MAX_LESSONS_TOTAL  # evicted rows are deleted, not accumulated
    evicted = conn.execute("SELECT COUNT(*) FROM experiment_lesson_evicted_pairs").fetchone()[0]
    assert evicted == 10  # bounded dedup history remembers the evicted pairs
    conn.close()

    # Explicit cap enforcement removes the least valuable rows and cleans up.
    conn = _conn(db)
    kept = [dict(r) for r in conn.execute("SELECT * FROM experiment_lessons").fetchall()]
    victim = kept[0]
    mark_injected(conn, [victim["id"]], "exp-victim")
    conn.execute(
        "INSERT INTO experiment_lesson_events (lesson_id, experiment_id, signature, kind, counted_at)"
        " VALUES (?, 'exp-victim', 'sig', 'test', '2026-01-01T00:00:00+00:00')",
        (victim["id"],),
    )
    conn.commit()
    removed = enforce_hard_cap(conn, cap=5, now="2026-01-01T00:00:00+00:00")
    assert removed == MAX_LESSONS_TOTAL - 5
    assert conn.execute(
        "SELECT COUNT(*) FROM experiment_lessons WHERE status != ?", (STATUS_REMOVED,)
    ).fetchone()[0] == 5
    # The victim (lowest injected count among ties... the marked one is kept,
    # so check the dependent rows of whichever rows went away instead):
    remaining_ids = {r[0] for r in conn.execute("SELECT id FROM experiment_lessons").fetchall()}
    orphan_injections = conn.execute(
        "SELECT COUNT(*) FROM experiment_lesson_injections WHERE lesson_id NOT IN"
        f" ({','.join('?' for _ in remaining_ids)})", tuple(remaining_ids)
    ).fetchone()[0] if remaining_ids else 0
    assert orphan_injections == 0
    orphan_events = conn.execute(
        "SELECT COUNT(*) FROM experiment_lesson_events WHERE lesson_id NOT IN"
        f" ({','.join('?' for _ in remaining_ids)})", tuple(remaining_ids)
    ).fetchone()[0] if remaining_ids else 0
    assert orphan_events == 0
    conn.close()

    # An evicted pair is not re-learned on reconcile.
    db2 = _db(tmp_path / "second")
    conn = _conn(db2)
    failure = {"id": "fA", "goal": "issue A", "status": "failed", "source_type": "issue",
               "source_id": "A", "error": "tests failed: alpha problem", "test_exit_code": 1}
    success = {"id": "sA", "goal": "issue A fixed", "status": "succeeded",
               "source_type": "issue", "source_id": "A", "git_status": ""}
    assert record_lesson(conn, failure, success, cap=1) is not None
    assert enforce_hard_cap(conn, cap=0 + 1, now="2026-01-01T00:00:00+00:00") == 0
    # force eviction of the only lesson by recording another pair with cap=1
    failure2 = {"id": "fB", "goal": "issue B", "status": "failed", "source_type": "issue",
                "source_id": "B", "error": "tests failed: beta problem", "test_exit_code": 1}
    success2 = {"id": "sB", "goal": "issue B fixed", "status": "succeeded",
                "source_type": "issue", "source_id": "B", "git_status": ""}
    record_lesson(conn, failure2, success2, cap=1)
    assert conn.execute("SELECT COUNT(*) FROM experiment_lessons").fetchone()[0] == 1
    pairs = {(r[0], r[1]) for r in conn.execute(
        "SELECT failure_experiment_id, success_experiment_id FROM experiment_lesson_evicted_pairs"
    ).fetchall()}
    assert ("fA", "sA") in pairs
    # Reconciling the source again must not resurrect the evicted pair.
    _insert_experiment(db2, "fA", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       source_id="A", goal="issue A", error="tests failed: alpha problem",
                       test_exit_code=1)
    _insert_experiment(db2, "sA", status="succeeded", created_at="2026-08-02T00:00:00+00:00",
                       source_id="A", goal="issue A fixed")
    assert note_experiment_finished(db2, "sA") == []
    assert len(list_lessons(10, db_path=db2)) == 1  # only the fB/sB lesson
    conn.close()


def test_hard_cap_insert_and_eviction_are_atomic_on_crash(tmp_path):
    db = _db(tmp_path)
    conn = _conn(db)
    # Seed one lesson, then make any eviction abort mid-transaction.
    failure = {"id": "fA", "goal": "issue A", "status": "failed", "source_type": "issue",
               "source_id": "A", "error": "tests failed: alpha problem", "test_exit_code": 1}
    success = {"id": "sA", "goal": "issue A fixed", "status": "succeeded",
               "source_type": "issue", "source_id": "A", "git_status": ""}
    first = record_lesson(conn, failure, success, cap=1)
    assert first is not None
    conn.execute(
        "CREATE TRIGGER abort_on_lesson_delete BEFORE DELETE ON experiment_lessons "
        "BEGIN SELECT RAISE(ABORT, 'simulated crash during eviction'); END"
    )
    conn.commit()
    failure2 = {"id": "fB", "goal": "issue B", "status": "failed", "source_type": "issue",
                "source_id": "B", "error": "tests failed: beta problem", "test_exit_code": 1}
    success2 = {"id": "sB", "goal": "issue B fixed", "status": "succeeded",
                "source_type": "issue", "source_id": "B", "git_status": ""}
    with pytest.raises(sqlite3.IntegrityError):
        record_lesson(conn, failure2, success2, cap=1)
    conn.rollback()
    # The failed insert was rolled back with the eviction: the store still has
    # exactly the first lesson and is not left over the cap.
    rows = conn.execute("SELECT id FROM experiment_lessons").fetchall()
    assert len(rows) == 1 and rows[0][0] == first["id"]
    conn.execute("DROP TRIGGER abort_on_lesson_delete")
    conn.commit()
    # After the crash is gone, recording works again and enforces the cap.
    assert record_lesson(conn, failure2, success2, cap=1) is not None
    assert conn.execute("SELECT COUNT(*) FROM experiment_lessons").fetchone()[0] == 1
    conn.close()


def test_public_functions_initialize_schema_on_a_raw_connection(tmp_path):
    # A caller holding a plain sqlite3 connection (no init_db) still works:
    # schema creation happens at entry, outside any transaction.
    path = str(tmp_path / "raw.db")
    conn = sqlite3.connect(path)
    assert enforce_hard_cap(conn, cap=5) == 0
    assert demote_stale_lessons(conn) == 0
    assert select_lessons(conn, "anything", []) == []
    failure = {"id": "f", "goal": "issue", "status": "failed", "source_type": "issue",
               "source_id": "1", "error": "tests failed: raw connection check", "test_exit_code": 1}
    success = {"id": "s", "goal": "issue fixed", "status": "succeeded", "source_type": "issue",
               "source_id": "1", "git_status": ""}
    lesson = record_lesson(conn, failure, success)
    assert lesson is not None
    mark_injected(conn, [lesson["id"]], "exp-r")
    conn.close()
    assert len(list_lessons(10, db_path=path)) == 1


def test_evicted_pair_history_is_bounded(tmp_path):
    db = _db(tmp_path)
    conn = _conn(db)
    for n in range(EVICTED_PAIRS_MAX + 25):
        failure = {"id": f"f{n}", "goal": f"issue {n}", "status": "failed",
                   "source_type": "issue", "source_id": str(1000 + n),
                   "error": f"tests failed: bounded {n}", "test_exit_code": 1}
        success = {"id": f"s{n}", "goal": f"issue {n} fixed", "status": "succeeded",
                   "source_type": "issue", "source_id": str(1000 + n), "git_status": ""}
        record_lesson(conn, failure, success, cap=1)
    count = conn.execute("SELECT COUNT(*) FROM experiment_lesson_evicted_pairs").fetchone()[0]
    assert count <= EVICTED_PAIRS_MAX
    assert conn.execute("SELECT COUNT(*) FROM experiment_lessons").fetchone()[0] == 1
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
            assert "time.monotonic()" in prompt
            return "- \"Inject the clock; never use time.monotonic() in tests.\""

    phrased = phrase_pending_lessons(3, llm=FakeLlm(), db_path=db)
    assert phrased == [lesson["id"]]
    updated = list_lessons(10, db_path=db)[0]
    assert updated["phrased"] is True
    assert updated["rule"] == "Inject the clock; never use time.monotonic() in tests."
    assert updated["model"] == "fake-local"
    # Already phrased -> the sweep has nothing left to do (and is replay-safe).
    assert phrase_pending_lessons(3, llm=FakeLlm(), db_path=db) == []
    assert list_lessons(10, db_path=db)[0]["rule"] == updated["rule"]

    # A failing model keeps the fallback rule and does not raise.
    _pair(db, failure_id="f2", success_id="s2",
          created_failure="2026-08-03T00:00:00+00:00", created_success="2026-08-04T00:00:00+00:00")
    note_experiment_finished(db, "s2")

    def broken(prompt: str) -> str:
        raise RuntimeError("llama-server down")

    assert phrase_pending_lessons(3, llm=broken, db_path=db) == []
    kept = [l for l in list_lessons(10, db_path=db) if l["failure_experiment_id"] == "f2"][0]
    assert kept["phrased"] is False
    assert kept["rule"] == kept["cause"][:experiment_lessons.MAX_RULE_CHARS]

    # Garbage replies are rejected: empty or too short to be a rule.
    for garbage in ("", "no", "ok then\nsecond line"):
        assert phrase_pending_lessons(3, llm=lambda p, g=garbage: g, db_path=db) == []
    # A multi-line reply is reduced to its one clean first line.
    assert phrase_pending_lessons(3, llm=lambda p: "Inject the clock in tests.\nignore the rest", db_path=db) == [kept["id"]]
    assert [l for l in list_lessons(10, db_path=db) if l["id"] == kept["id"]][0]["rule"] == "Inject the clock in tests."
    # A reply containing a secret is stored redacted.
    _pair(db, failure_id="f3", success_id="s3",
          created_failure="2026-08-05T00:00:00+00:00", created_success="2026-08-06T00:00:00+00:00")
    note_experiment_finished(db, "s3")
    leaky_lesson = [l for l in list_lessons(10, db_path=db) if l["failure_experiment_id"] == "f3"][0]

    def leaky(prompt: str) -> str:
        return "Always set token=hunter2hunter2 before running the suite"

    phrased2 = phrase_pending_lessons(3, llm=leaky, db_path=db)
    assert phrased2 == [leaky_lesson["id"]]
    stored = [l for l in list_lessons(10, db_path=db) if l["id"] == leaky_lesson["id"]][0]
    assert "hunter2hunter2" not in stored["rule"]
    assert "REDACTED" in stored["rule"]


# ---- measure: recurrence stats -------------------------------------------------


def test_failure_recurrence_stats_counts_repeated_signatures(tmp_path):
    db = _db(tmp_path)
    _insert_experiment(db, "a1", status="failed", created_at="2026-08-01T00:00:00+00:00",
                       source_id="715", error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
    _insert_experiment(db, "a2", status="failed", created_at="2026-08-02T00:00:00+00:00",
                       source_id="715", error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
    _insert_experiment(db, "a3", status="failed", created_at="2026-08-03T00:00:00+00:00",
                       source_id="715", error="git worktree add failed: disk full")
    _insert_experiment(db, "b1", status="failed", created_at="2026-08-04T00:00:00+00:00",
                       source_id="999", error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT)
    stats = failure_recurrence_stats(db, window=20)
    assert stats["window"] == 4
    assert stats["failure_events"] == 4
    # a2 repeats a1's signature on the same source; b1 is a different source.
    assert stats["repeated"] == 1
    assert stats["repeat_rate"] == 0.25


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
        assert "recurrence" in text  # the measure rides along on the read side
        with self.assertRaises(Exception):
            registry.execute("lessons_list", {"limit": 0})

    def test_api_lists_lessons_with_redacted_fields(self):
        from fastapi.testclient import TestClient

        import api

        # Simulate a keyword derived from raw evidence containing a secret.
        conn = _conn(self.db_path)
        conn.execute(
            "UPDATE experiment_lessons SET keywords_json=? WHERE id=?",
            (json.dumps(["monotonic", "sk-abc123def456ghi789"]), self.lesson["id"]),
        )
        conn.commit()
        conn.close()

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
        assert all("sk-abc123" not in k for k in lesson["keywords"])
        assert any("REDACTED" in k or "****" in k or k == "monotonic" for k in lesson["keywords"])
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
        # A failed attempt on issue #999 exists before the successful run; the
        # terminal _force_status hook reconciles but finds no success yet.
        failed = service.create_experiment("GitHub Issue #999: fix monotonic budget test")
        service._force_status(
            failed.id, "failed", ended_at="2026-08-01T00:00:00+00:00",
            error=FAIL_ERROR, test_exit_code=1, test_output=FAIL_OUTPUT,
        )
        assert list_lessons(10, db_path=db) == []

        # The retry on the same issue succeeds; finalization records exactly one
        # lesson with both evidence ids.
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

        # The next coding run on a matching issue gets the section injected,
        # and the injection is tracked exactly once for that run.
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
        conn = _conn(db)
        injections = conn.execute(
            "SELECT lesson_id FROM experiment_lesson_injections WHERE experiment_id=?",
            (followup.id,),
        ).fetchall()
        assert [r[0] for r in injections] == [lessons[0]["id"]]
        assert conn.execute(
            "SELECT injected_count FROM experiment_lessons WHERE id=?", (lessons[0]["id"],)
        ).fetchone()[0] == 1
        conn.close()
    finally:
        service.close()
