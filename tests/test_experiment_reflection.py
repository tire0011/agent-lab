import json

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
