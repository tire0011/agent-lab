"""Deterministic measurements and delivery checks for #716."""

from datetime import datetime, timedelta, timezone

import fastapi.routing
import httpx
import pytest

import api
import data_sources
from modules import measurement_health, nexus_planner
from modules.measurement_health import check_pipelines
from modules.operations_registry import OperationsRegistry
from modules.run_store import init_db

NOW = datetime(2026, 11, 16, 18, 54, 30, tzinfo=timezone.utc)
ISSUES = [{"number": 716, "title": "Measurement health"}]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv("NEXUS_SHADOW_REPLAY", raising=False)
    monkeypatch.delenv("NEXUS_SHADOW_REPLAY_UNIT", raising=False)
    monkeypatch.delenv("NEXUS_PLANNER_SHADOW", raising=False)
    conn = init_db(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def add_replays(db, count, *, status="local_too_slow", age=0):
    for i in range(count):
        db.execute(
            "INSERT INTO local_replays "
            "(experiment_id, prompt_variant, status, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"exp-{i}", "default", status, f"the time budget allows only {308 + i} output tokens",
             (NOW - timedelta(hours=age, seconds=count - i)).isoformat()),
        )


def test_same_failure_with_variable_token_counts_is_stuck(db):
    add_replays(db, 5)
    result = check_pipelines(db, NOW)["local_replays"]
    assert result["status"] == "stuck"
    assert result["outcome"] == "local_too_slow"
    assert result["normalized_reason"] == "the time budget allows only output tokens"
    assert result["reason"] == "the time budget allows only 312 output tokens"
    assert result["row_ids"] == [5, 4, 3, 2, 1]


@pytest.mark.parametrize("status, reason", [
    ("scored", None),
    ("error", "the time budget allows only 312 output tokens"),
    ("local_too_slow", "different reason"),
])
def test_mixed_outcomes_or_reasons_are_ok(db, status, reason):
    add_replays(db, 5)
    db.execute("UPDATE local_replays SET status=?, error=? WHERE id=3", (status, reason))
    assert check_pipelines(db, NOW)["local_replays"]["status"] == "ok"


@pytest.mark.parametrize("row_count", [0, 1])
def test_stale_only_when_enabled(db, monkeypatch, row_count):
    add_replays(db, row_count, status="scored", age=25)
    assert check_pipelines(db, NOW)["local_replays"]["status"] == "stale"
    monkeypatch.setenv("NEXUS_SHADOW_REPLAY", "0")
    result = check_pipelines(db, NOW)["local_replays"]
    assert result["status"] == "ok"
    assert result["enabled"] is False


def test_fewer_than_threshold_is_ok(db):
    add_replays(db, 4)
    assert check_pipelines(db, NOW)["local_replays"]["status"] == "ok"


def test_other_measurement_pipelines(db):
    initial = check_pipelines(db, NOW)
    assert initial["planner_shadow"]["status"] == "stale"
    assert initial["local_model_bakeoffs"]["status"] == "ok"
    for i in range(5):
        db.execute(
            "INSERT INTO planner_shadow "
            "(tick_id, paid_issue, local_valid, prompt_tokens, seconds, error, created_at) "
            "VALUES (?, 716, 0, 100, 0.1, ?, ?)",
            (str(i), f"timeout after {i} seconds", NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO local_model_bakeoffs (run_id, created_at, model, status, error) "
            "VALUES ('run', ?, 'model', 'switch_failed', ?)",
            (NOW.isoformat(), f"exit code {i}"),
        )
    results = check_pipelines(db, NOW)
    assert results["planner_shadow"]["status"] == "stuck"
    assert results["local_model_bakeoffs"]["status"] == "stuck"


def test_hunk_replay_failures_and_configured_cadence(db, monkeypatch):
    monkeypatch.setenv("NEXUS_SHADOW_REPLAY_UNIT", "hunk")
    results = check_pipelines(db, NOW)
    assert results["local_replay_units"]["status"] == "stale"
    assert results["local_replays"]["status"] == "ok"
    for i in range(5):
        db.execute(
            "INSERT INTO local_replay_units "
            "(experiment_id, file, symbol, prompt_variant, status, error, created_at) "
            "VALUES (?, 'app.py', 'handler', 'default', 'timeout', ?, ?)",
            (str(i), f"timed out after {i} seconds", NOW.isoformat()),
        )
    assert check_pipelines(db, NOW)["local_replay_units"]["status"] == "stuck"


def test_legacy_planner_shadow_uses_tick_timestamp(db):
    db.execute("ALTER TABLE planner_shadow DROP COLUMN created_at")
    db.execute("CREATE TABLE nexus_initiative_log (tick_id TEXT PRIMARY KEY, created_at TEXT)")
    db.execute("INSERT INTO nexus_initiative_log VALUES ('tick', ?)", (NOW.isoformat(),))
    db.execute(
        "INSERT INTO planner_shadow (tick_id, paid_issue, local_valid, prompt_tokens, seconds) "
        "VALUES ('tick', 716, 1, 100, 0.1)"
    )
    result = check_pipelines(db, NOW)["planner_shadow"]
    assert result["status"] == "ok"
    assert result["latest_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_problems_lists_stuck_and_stale(db, monkeypatch):
    add_replays(db, 5)
    monkeypatch.setattr(data_sources, "read_health", lambda path, now: check_pipelines(db, NOW))
    monkeypatch.setattr(data_sources, "RUNS_DIR", data_sources.REPO_ROOT / "missing-runs")
    monkeypatch.setattr(data_sources, "EXPERIMENT_DB_PATH", data_sources.REPO_ROOT / "missing.db")

    # The sandbox can stall when AnyIO wakes the request loop from a worker.
    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", run_inline)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/problems")
    assert response.status_code == 200
    problems = {entry["source"]: entry for entry in response.json()["problems"]}
    assert problems["measurement_stuck"]["summary"] == (
        "local replays: 5x local_too_slow: the time budget allows only 312 output tokens"
    )
    assert "5, 4, 3, 2, 1" in problems["measurement_stuck"]["detail"]
    assert problems["measurement_stale"]["id"] == "planner_shadow"


def test_first_planner_prompt_starts_with_stuck_measurement(db, monkeypatch):
    add_replays(db, 5)
    monkeypatch.setattr(nexus_planner, "read_health", lambda path, now: check_pipelines(db, NOW))
    prompts = []
    planner = nexus_planner.IssuePlanner(
        lambda prompt: prompts.append(prompt) or '{"issue": 716, "reason": "Fix the pipeline"}',
        db_path=":memory:",
    )
    choice = planner.choose(ISSUES)
    assert choice.issue == 716
    assert len(prompts) == 1
    assert prompts[0].startswith(
        "STUCK MEASUREMENT PIPELINES:\n"
        "local replays: 5x local_too_slow: the time budget allows only 312 output tokens\n"
    )


def test_read_only_operation_returns_structured_results(db, monkeypatch):
    add_replays(db, 5)
    monkeypatch.setattr(measurement_health, "read_health", lambda path, now: check_pipelines(db, NOW))
    registry = OperationsRegistry(experiment_service=None, db_path=":memory:")
    try:
        result = registry.execute("measurement_health", {})
        assert result.success
        assert result.metrics["pipelines"]["local_replays"]["row_ids"] == [5, 4, 3, 2, 1]
    finally:
        registry.close()
