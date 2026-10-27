import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.experiment_reflection_service import (
    MAX_REFLECTION_TOKENS,
    ExperimentReflectionService,
)
from modules.experiment_service import ExperimentService


def _experiment(status="succeeded"):
    return SimpleNamespace(
        id="exp-reflection", goal="Make experiments cheaper", status=status,
        plan={"steps": ["measure", "change"]}, git_status=" M modules/a.py\n?? tests/test_a.py",
        research_runtime_ms=10, selection_runtime_ms=20, reformat_retry_runtime_ms=None,
        policy_replan_runtime_ms=None, implementation_runtime_ms=30,
        research_usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        selection_usage=None, reformat_retry_usage=None, policy_replan_usage=None,
        implementation_usage={"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
        test_exit_code=0, error=None,
    )


def _suggestion(number):
    return {
        "title": f"Improvement {number}",
        "expected_benefit": "Faster experiments",
        "cost": "One small change",
        "risk": "Low",
        "measurement": "Compare median runtime",
    }


class FakeLlm:
    model = "cheap-test-model"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


class ExperimentReflectionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    async def _run(self, payload, **kwargs):
        client = FakeLlm(payload)
        service = ExperimentReflectionService(
            db_path=self.db_path, llm_client=client, **kwargs
        )
        with service._conn:
            service._conn.execute(
                "INSERT INTO experiments (id,goal,status,created_at) VALUES (?,?,?,?)",
                ("exp-reflection", "Make experiments cheaper", "succeeded", "2026-01-01T00:00:00+00:00"),
            )
        try:
            result = await service.reflect(_experiment())
        finally:
            service.close()
        return result, client

    async def test_zero_suggestions_is_valid_and_persisted(self):
        result, client = await self._run({"suggestions": []})
        self.assertEqual(result, [])
        self.assertEqual(client.calls[0]["max_tokens"], MAX_REFLECTION_TOKENS)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT suggestions_json,error FROM experiment_reflections"
            ).fetchone()
        self.assertEqual(json.loads(row[0]), [])
        self.assertIsNone(row[1])

    async def test_explicit_no_useful_improvement_answer_is_valid(self):
        result, _ = await self._run("No useful improvement found")
        self.assertEqual(result, [])

    async def test_three_structured_suggestions_are_kept(self):
        result, _ = await self._run({"suggestions": [_suggestion(i) for i in range(3)]})
        self.assertEqual(len(result), 3)
        self.assertEqual(
            set(result[0]),
            {"title", "expected_benefit", "cost", "risk", "measurement"},
        )

    async def test_too_many_suggestions_are_hard_limited_to_three(self):
        result, _ = await self._run({"suggestions": [_suggestion(i) for i in range(8)]})
        self.assertEqual([item["title"] for item in result], [
            "Improvement 0", "Improvement 1", "Improvement 2"
        ])

    async def test_open_issue_titles_are_deduplicated(self):
        result, _ = await self._run(
            {"suggestions": [_suggestion(0), _suggestion(1)]},
            open_issue_titles=lambda: ["  IMPROVEMENT-0  "],
        )
        self.assertEqual([item["title"] for item in result], ["Improvement 1"])

    async def test_failure_is_recorded_and_never_changes_experiment_status(self):
        experiment = _experiment(status="failed")
        service = ExperimentReflectionService(
            db_path=self.db_path, llm_client=FakeLlm(error=RuntimeError("offline"))
        )
        with service._conn:
            service._conn.execute(
                "INSERT INTO experiments (id,goal,status,created_at) VALUES (?,?,?,?)",
                (experiment.id, experiment.goal, experiment.status, "2026-01-01T00:00:00+00:00"),
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await service.reflect(experiment)
        finally:
            service.close()
        self.assertEqual(experiment.status, "failed")
        with sqlite3.connect(self.db_path) as conn:
            error = conn.execute(
                "SELECT error FROM experiment_reflections WHERE experiment_id=?",
                (experiment.id,),
            ).fetchone()[0]
            status = conn.execute(
                "SELECT status FROM experiments WHERE id=?", (experiment.id,)
            ).fetchone()
        self.assertIn("offline", error)
        self.assertEqual(status[0], "failed")

    async def test_run_keeps_terminal_status_when_reflector_raises(self):
        class BrokenReflector:
            def reflect_sync(self, experiment):
                raise RuntimeError("reflection broke")

        service = ExperimentService(
            selection_worker=object(),
            worker_factory=lambda _: object(),
            plan_worker_factory=lambda _: object(),
            db_path=self.db_path,
            repo_root=self.tmp.name,
            worktrees_root=self.tmp.name,
            experiment_log_dir=str(Path(self.tmp.name) / "logs"),
            reflection_service=BrokenReflector(),
        )
        experiment = service.create_experiment("terminal status is immutable")

        def finish_failed(experiment_id, **_kwargs):
            service._force_status(experiment_id, "failed", error="original failure")
            return service.get_experiment(experiment_id)

        try:
            with patch.object(service, "_run_pipeline", side_effect=finish_failed):
                result = service.run_experiment(experiment.id)
        finally:
            service.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "original failure")


if __name__ == "__main__":
    unittest.main()
