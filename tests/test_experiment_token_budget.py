"""Experiment-level worker token accounting in shadow mode (#532)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_service.service import ExperimentService, worker_run_token_total


class ExperimentTokenBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = str(root / "agent.db")
        self.log_dir = str(root / "logs")

    def _service(self, budget: str | None) -> ExperimentService:
        env = {}
        if budget is not None:
            env["NEXUS_EXPERIMENT_TOKEN_BUDGET"] = budget
        with patch.dict(os.environ, env, clear=False):
            if budget is None:
                os.environ.pop("NEXUS_EXPERIMENT_TOKEN_BUDGET", None)
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


if __name__ == "__main__":
    unittest.main()
