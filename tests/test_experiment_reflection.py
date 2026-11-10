import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

import modules.experiment_reflection as reflection
from modules.experiment_reflection import MAX_SUMMARY_CHARS, reflect
from modules.run_store import init_db


class FakeLlm:
    model = "fake-local"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _db(tmp_path, *, status="succeeded"):
    path = str(tmp_path / "reflection.db")
    conn = init_db(path)
    conn.execute(
        """INSERT INTO experiments
           (id, goal, status, created_at, plan_json, git_status,
            selection_runtime_ms, implementation_runtime_ms,
            selection_usage_json, implementation_usage_json,
            test_exit_code, test_output, error)
           VALUES ('exp-1', ?, ?, '2026-01-01T00:00:00Z', ?, ?, 12, 34, ?, ?, 0, 'ok', NULL)""",
        ("g" * 2500, status, '["step"]', " M modules/a.py\n?? tests/test_a.py",
         '{"total_tokens":10}', '{"total_tokens":20}'),
    )
    conn.commit()
    conn.close()
    return path


def _suggestion(title="Improve cache"):
    return {"title": title, "benefit": "faster", "cost": "small", "risk": "low", "measure": "latency"}


def _row(path):
    conn = init_db(path)
    row = conn.execute(
        "SELECT model, suggestions_json, error FROM experiment_reflections WHERE experiment_id='exp-1'"
    ).fetchone()
    status = conn.execute("SELECT status FROM experiments WHERE id='exp-1'").fetchone()[0]
    conn.close()
    return row, status


def test_zero_suggestions_and_compact_prompt(tmp_path):
    path = _db(tmp_path)
    llm = FakeLlm('{"suggestions":[]}')
    assert reflect("exp-1", llm=llm, db_path=path) == []
    row, _ = _row(path)
    assert row == ("fake-local", "[]", None)
    prompt = llm.calls[0]["prompt"]
    assert "Changed files: modules/a.py, tests/test_a.py" in prompt
    assert "Phase timings" in prompt and "Token usage" in prompt
    assert len(prompt.rsplit("\n\n", 1)[1]) <= MAX_SUMMARY_CHARS
    assert llm.calls[0]["max_tokens"] == 500


def test_three_suggestions_are_stored(tmp_path):
    path = _db(tmp_path)
    values = [_suggestion(f"idea {n}") for n in range(3)]
    assert reflect("exp-1", llm=FakeLlm(json.dumps({"suggestions": values})), db_path=path) == values


def test_five_suggestions_are_cut_to_three(tmp_path):
    path = _db(tmp_path)
    values = [_suggestion(f"idea {n}") for n in range(5)]
    result = reflect("exp-1", llm=FakeLlm(json.dumps({"suggestions": values})), db_path=path)
    assert result == values[:3]
    assert len(json.loads(_row(path)[0][1])) == 3


def test_invalid_json_stores_empty_result_and_error(tmp_path):
    path = _db(tmp_path)
    assert reflect("exp-1", llm=FakeLlm("not json"), db_path=path) == []
    row, _ = _row(path)
    assert row[1] == "[]"
    assert "JSONDecodeError" in row[2]


def test_raising_llm_stores_error_without_changing_experiment(tmp_path):
    path = _db(tmp_path, status="needs_review")
    assert reflect("exp-1", llm=FakeLlm(error=RuntimeError("offline")), db_path=path) == []
    row, status = _row(path)
    assert "RuntimeError: offline" in row[2]
    assert status == "needs_review"


def test_duplicate_title_is_marked(tmp_path):
    path = _db(tmp_path)
    llm = FakeLlm(json.dumps({"suggestions": [_suggestion("Add cache metrics")]}))
    result = reflect(
        "exp-1", ["Add cache metrics to experiment dashboard", "Unrelated issue"],
        llm=llm, db_path=path,
    )
    assert result[0]["duplicate_of"] == "Add cache metrics to experiment dashboard"


def test_second_call_is_idempotent_and_does_not_run_model(tmp_path):
    path = _db(tmp_path)
    first = FakeLlm('{"suggestions":[]}')
    second = FakeLlm(error=AssertionError("must not run"))
    reflect("exp-1", llm=first, db_path=path)
    assert reflect("exp-1", llm=second, db_path=path) == []
    assert len(first.calls) == 1
    assert second.calls == []


def test_concurrent_calls_submit_exactly_one_model_job(tmp_path):
    path = _db(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class BlockingLlm(FakeLlm):
        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            assert release.wait(timeout=5)
            return '{"suggestions":[]}'

    llm = BlockingLlm()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reflect, "exp-1", llm=llm, db_path=path)
        assert entered.wait(timeout=5)
        second = pool.submit(reflect, "exp-1", llm=llm, db_path=path)
        assert second.result(timeout=5) == []
        release.set()
        assert first.result(timeout=5) == []

    assert len(llm.calls) == 1
    conn = init_db(path)
    assert conn.execute(
        "SELECT status FROM experiment_reflections WHERE experiment_id='exp-1'"
    ).fetchone()[0] == "completed"
    conn.close()


def _reflection_state(path):
    conn = init_db(path)
    row = conn.execute(
        """SELECT status, claim_token, lease_expires_at, suggestions_json, error
           FROM experiment_reflections WHERE experiment_id='exp-1'"""
    ).fetchone()
    conn.close()
    return row


def _leave_pending_claim(path, *, lease_expires_at):
    """What a caller killed after its claim commit leaves behind."""
    conn = init_db(path)
    conn.execute(
        """INSERT INTO experiment_reflections
           (experiment_id, created_at, model, suggestions_json, error,
            status, claim_token, lease_expires_at)
           VALUES ('exp-1', '2026-01-01T00:00:00+00:00', 'dead', '[]', NULL,
                   'pending', 'dead-owner', ?)""",
        (lease_expires_at,),
    )
    conn.commit()
    conn.close()


def _iso(delta_seconds):
    return (reflection._utcnow() + timedelta(seconds=delta_seconds)).isoformat()


def test_stale_pending_claim_from_dead_caller_is_recovered(tmp_path):
    path = _db(tmp_path)
    _leave_pending_claim(path, lease_expires_at=_iso(-1))
    values = [_suggestion("idea")]
    llm = FakeLlm(json.dumps({"suggestions": values}))
    assert reflect("exp-1", llm=llm, db_path=path) == values
    assert len(llm.calls) == 1
    status, token, lease, stored, error = _reflection_state(path)
    assert (status, token, lease, error) == ("completed", None, None, None)
    assert json.loads(stored) == values
    # Completed now: further calls are idempotent again.
    again = FakeLlm(error=AssertionError("must not run"))
    assert reflect("exp-1", llm=again, db_path=path) == values
    assert again.calls == []


def test_legacy_pending_claim_without_lease_is_recovered(tmp_path):
    path = _db(tmp_path)
    _leave_pending_claim(path, lease_expires_at=None)
    llm = FakeLlm('{"suggestions":[]}')
    assert reflect("exp-1", llm=llm, db_path=path) == []
    assert len(llm.calls) == 1
    assert _reflection_state(path)[0] == "completed"


def test_live_pending_claim_is_not_run_twice(tmp_path):
    path = _db(tmp_path)
    _leave_pending_claim(path, lease_expires_at=_iso(600))
    llm = FakeLlm(error=AssertionError("must not run"))
    assert reflect("exp-1", llm=llm, db_path=path) == []
    assert llm.calls == []
    assert _reflection_state(path)[:2] == ("pending", "dead-owner")


class _Interrupted(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit escaping mid-inference."""


def test_interrupted_inference_releases_claim_for_retry(tmp_path, monkeypatch):
    path = _db(tmp_path)
    real_run_model = reflection._run_model

    def interrupted(*args, **kwargs):
        # The caller's thread is interrupted while waiting on the model.
        raise _Interrupted()

    monkeypatch.setattr(reflection, "_run_model", interrupted)
    with pytest.raises(_Interrupted):
        reflect("exp-1", llm=FakeLlm('{"suggestions":[]}'), db_path=path)
    assert _reflection_state(path) is None
    monkeypatch.setattr(reflection, "_run_model", real_run_model)
    llm = FakeLlm('{"suggestions":[]}')
    assert reflect("exp-1", llm=llm, db_path=path) == []
    assert len(llm.calls) == 1
    assert _reflection_state(path)[0] == "completed"


def test_failed_finalization_releases_claim_for_retry(tmp_path, monkeypatch):
    path = _db(tmp_path)
    real_finalize = reflection._finalize

    def broken_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(reflection, "_finalize", broken_finalize)
    first = FakeLlm('{"suggestions":[]}')
    with pytest.raises(sqlite3.OperationalError):
        reflect("exp-1", llm=first, db_path=path)
    assert _reflection_state(path) is None

    monkeypatch.setattr(reflection, "_finalize", real_finalize)
    second = FakeLlm('{"suggestions":[]}')
    assert reflect("exp-1", llm=second, db_path=path) == []
    assert len(first.calls) == len(second.calls) == 1
    assert _reflection_state(path)[0] == "completed"


def test_failed_finalization_and_release_recovers_after_lease(tmp_path, monkeypatch):
    path = _db(tmp_path)
    real_finalize, real_release = reflection._finalize, reflection._release

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reflection, "_finalize", broken)
    monkeypatch.setattr(reflection, "_release", broken)
    with pytest.raises(sqlite3.OperationalError):
        reflect("exp-1", llm=FakeLlm('{"suggestions":[]}'), db_path=path)
    assert _reflection_state(path)[0] == "pending"
    monkeypatch.setattr(reflection, "_finalize", real_finalize)
    monkeypatch.setattr(reflection, "_release", real_release)

    # Lease still running: the claim is respected, the model is not run.
    blocked = FakeLlm(error=AssertionError("must not run"))
    assert reflect("exp-1", llm=blocked, db_path=path) == []
    assert blocked.calls == []

    # After the lease runs out the claim is stale and taken over.
    later = reflection._utcnow() + timedelta(seconds=reflection._lease_seconds() + 1)
    monkeypatch.setattr(reflection, "_utcnow", lambda: later)
    values = [_suggestion("retry")]
    retry = FakeLlm(json.dumps({"suggestions": values}))
    assert reflect("exp-1", llm=retry, db_path=path) == values
    assert len(retry.calls) == 1
    assert _reflection_state(path)[0] == "completed"


def test_stale_owner_cannot_finalize_after_takeover(tmp_path):
    path = _db(tmp_path)
    _leave_pending_claim(path, lease_expires_at=_iso(-1))
    conn = reflection._connect(path)
    try:
        assert reflection._claim(conn, "exp-1", "m", "new-owner", 60) == ("claimed", [])
        assert not reflection._finalize(conn, "exp-1", "dead-owner", [_suggestion()], None)
        reflection._release(conn, "exp-1", "dead-owner")
        assert reflection._finalize(conn, "exp-1", "new-owner", [], None)
    finally:
        conn.close()
    assert _reflection_state(path)[:2] == ("completed", None)


def test_concurrent_takeover_of_stale_claim_runs_model_once(tmp_path):
    path = _db(tmp_path)
    _leave_pending_claim(path, lease_expires_at=_iso(-1))
    entered = threading.Event()
    release = threading.Event()

    class BlockingLlm(FakeLlm):
        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            assert release.wait(timeout=5)
            return '{"suggestions":[]}'

    llm = BlockingLlm()
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(reflect, "exp-1", llm=llm, db_path=path)
        assert entered.wait(timeout=5)
        others = [pool.submit(reflect, "exp-1", llm=llm, db_path=path) for _ in range(3)]
        assert [f.result(timeout=5) for f in others] == [[], [], []]
        release.set()
        assert first.result(timeout=5) == []
    assert len(llm.calls) == 1
    assert _reflection_state(path)[0] == "completed"


def test_lease_keeper_extends_lease_while_owner_is_alive(tmp_path):
    path = _db(tmp_path)
    conn = reflection._connect(path)
    try:
        assert reflection._claim(conn, "exp-1", "m", "owner", 60)[0] == "claimed"
        conn.execute(
            "UPDATE experiment_reflections SET lease_expires_at=? WHERE experiment_id='exp-1'",
            (_iso(1),),
        )
        keeper = reflection._LeaseKeeper(path, "exp-1", "owner", 60, interval=0.02).start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                lease = _reflection_state(path)[2]
                if lease > _iso(30):
                    break
                time.sleep(0.02)
            assert _reflection_state(path)[2] > _iso(30)
        finally:
            keeper.stop()
    finally:
        conn.close()


def test_schema_migration_adds_claim_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE experiment_reflections (
               experiment_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
               model TEXT NOT NULL, suggestions_json TEXT NOT NULL, error TEXT NULL)"""
    )
    conn.execute("INSERT INTO experiment_reflections VALUES ('old', 't', 'm', '[]', NULL)")
    conn.commit()
    conn.close()
    conn = init_db(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(experiment_reflections)")}
    assert {"status", "claim_token", "lease_expires_at"} <= columns
    assert conn.execute(
        "SELECT status FROM experiment_reflections WHERE experiment_id='old'"
    ).fetchone()[0] == "completed"
    conn.close()
