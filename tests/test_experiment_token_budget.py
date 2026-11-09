"""Experiment-level worker token accounting in shadow mode (#532)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.claude_worker import _LiveUsageParser
from modules.experiment_service.service import ExperimentService, worker_run_token_total


class ExperimentTokenBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = str(root / "agent.db")
        self.log_dir = str(root / "logs")

    def _service(self, budget: str | None, mode: str | None = None) -> ExperimentService:
        env = {}
        if budget is not None:
            env["NEXUS_EXPERIMENT_TOKEN_BUDGET"] = budget
        if mode is not None:
            env["NEXUS_EXPERIMENT_TOKEN_BUDGET_MODE"] = mode
        with patch.dict(os.environ, env, clear=False):
            if budget is None:
                os.environ.pop("NEXUS_EXPERIMENT_TOKEN_BUDGET", None)
            if mode is None:
                os.environ.pop("NEXUS_EXPERIMENT_TOKEN_BUDGET_MODE", None)
            service = ExperimentService(
                selection_worker=object(),
                worker_factory=lambda _path: object(),
                plan_worker_factory=lambda _path: object(),
                db_path=self.db_path,
                repo_root=self.tmp.name,
                worktrees_root=str(Path(self.tmp.name) / "worktrees"),
                experiment_log_dir=self.log_dir,
            )
        self.addCleanup(service.close)
        return service

    @staticmethod
    def _warning_entries(service: ExperimentService, experiment_id: str) -> list[dict]:
        path = service.experiment_log_path(experiment_id)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "token_budget_exceeded"
        ]

    def test_two_runs_sum_and_warn_exactly_once(self) -> None:
        service = self._service("100")
        experiment = service.create_experiment("Measure two worker runs")
        elog = service.get_experiment_logger(experiment.id)

        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 50, "output_tokens": 40}, elog
        )
        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 60, "output_tokens": 30}, elog
        )
        usage = service.get_experiment(experiment.id).implementation_usage
        self.assertEqual(usage["tokens_consumed"], 180)
        self.assertEqual(usage["token_run_count"], 2)
        # A later run remains counted but must not emit another warning.
        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 1, "output_tokens": 1}, elog
        )

        usage = service.get_experiment(experiment.id).implementation_usage
        self.assertEqual(usage["token_budget"], 100)
        self.assertEqual(usage["tokens_consumed"], 182)
        self.assertEqual(usage["token_run_count"], 3)
        warnings = self._warning_entries(service, experiment.id)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["budget"], 100)
        self.assertEqual(warnings[0]["total"], 180)
        self.assertEqual(warnings[0]["run_count"], 2)

    def test_implementation_fallback_and_repair_runs_are_all_summed(self) -> None:
        service = self._service("1000")
        experiment = service.create_experiment("Count every invocation kind")
        elog = service.get_experiment_logger(experiment.id)

        # These correspond to the three call sites in the pipeline: initial
        # implementation, provider fallback, and a repair iteration.
        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 10, "output_tokens": 20}, elog
        )
        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 30, "output_tokens": 40}, elog
        )
        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 50, "output_tokens": 60}, elog
        )

        usage = service.get_experiment(experiment.id).implementation_usage
        self.assertEqual(usage["tokens_consumed"], 210)
        self.assertEqual(usage["token_run_count"], 3)

    def test_unset_budget_preserves_existing_behavior(self) -> None:
        service = self._service(None)
        experiment = service.create_experiment("Budget disabled")
        encoded = service._record_worker_run_usage(
            experiment.id,
            {"input_tokens": 90, "output_tokens": 90},
            service.get_experiment_logger(experiment.id),
        )

        self.assertEqual(json.loads(encoded), {"input_tokens": 90, "output_tokens": 90})
        self.assertIsNone(service.get_experiment(experiment.id).implementation_usage)
        self.assertEqual(self._warning_entries(service, experiment.id), [])

    def test_missing_usage_adds_zero_without_failing(self) -> None:
        service = self._service("100")
        experiment = service.create_experiment("Worker omitted usage")
        service._record_worker_run_usage(
            experiment.id, None, service.get_experiment_logger(experiment.id)
        )

        usage = service.get_experiment(experiment.id).implementation_usage
        self.assertEqual(usage["tokens_consumed"], 0)
        self.assertEqual(usage["token_run_count"], 1)
        self.assertEqual(self._warning_entries(service, experiment.id), [])

    def test_provider_cache_token_fields_use_one_helper(self) -> None:
        claude = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 8,
        }
        codex = {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 9}
        self.assertEqual(worker_run_token_total(claude), 30)
        self.assertEqual(worker_run_token_total(codex), 24)

    def test_claude_live_stream_counts_each_message_id_once(self) -> None:
        calls = []
        parser = _LiveUsageParser(lambda usage: calls.append(usage) or False)
        events = [
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10}}},
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10}}},
            {"type": "assistant", "message": {"id": "m2", "usage": {"output_tokens": 5}}},
        ]

        for event in events:
            self.assertFalse(parser.consume(json.dumps(event)))

        self.assertEqual(calls, [{"input_tokens": 10}, {"output_tokens": 5}])

    def test_enforce_live_crossing_stops_and_records_needs_review(self) -> None:
        service = self._service("10", "enforce")
        experiment = service.create_experiment("Stop a runaway Claude stream")
        callback, state = service._live_worker_usage_callback(
            experiment.id, service.get_experiment_logger(experiment.id)
        )

        self.assertFalse(callback({"input_tokens": 6}))
        self.assertTrue(callback({"output_tokens": 5}))

        result = service.get_experiment(experiment.id)
        self.assertEqual(state, {"tokens": 11, "calls": 2})
        self.assertEqual(result.status, "needs_review")
        self.assertIn("budget=10", result.error)
        self.assertIn("consumed=11", result.error)

    def test_shadow_live_crossing_logs_but_does_not_stop(self) -> None:
        service = self._service("10", "shadow")
        experiment = service.create_experiment("Observe a Claude stream")
        callback, _state = service._live_worker_usage_callback(
            experiment.id, service.get_experiment_logger(experiment.id)
        )

        self.assertFalse(callback({"input_tokens": 11}))

        self.assertEqual(service.get_experiment(experiment.id).status, "queued")
        warnings = self._warning_entries(service, experiment.id)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["mode"], "shadow")

    def test_enforce_end_of_run_usage_handles_codex_limit(self) -> None:
        service = self._service("10", "enforce")
        experiment = service.create_experiment("Count Codex at turn completion")

        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 8, "output_tokens": 3},
            service.get_experiment_logger(experiment.id),
        )

        result = service.get_experiment(experiment.id)
        self.assertEqual(result.status, "needs_review")
        self.assertIn("completed before usage was available", result.error)


if __name__ == "__main__":
    unittest.main()
