"""Experiment-level worker token accounting: shadow mode (#532), live count and
enforce mode (#533)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.claude_worker import ClaudeWorker, _LiveUsageParser
from modules.coding_worker import STOP_REQUESTED, run_watched
from modules.experiment_service import STATUS_FAILED, STATUS_NEEDS_REVIEW
from modules.experiment_service.service import ExperimentService, worker_run_token_total
from tests.test_issue_193_coding_provider_wiring import _init_git_repo, _worker_result


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
        self.assertIn("stop_reason=end_of_run_count", result.error)
        self.assertIn("budget=10", result.error)
        self.assertIn("consumed=11", result.error)

    def test_end_of_run_enforce_never_overwrites_a_cancellation(self) -> None:
        service = self._service("10", "enforce")
        experiment = service.create_experiment("Cancelled while running")
        service._force_status(experiment.id, "cancelled", ended_at="now")

        service._record_worker_run_usage(
            experiment.id, {"input_tokens": 50},
            service.get_experiment_logger(experiment.id),
        )

        self.assertEqual(service.get_experiment(experiment.id).status, "cancelled")



def _assistant(message_id: str, **usage: int) -> str:
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage}})


# A fake ``claude`` that records each start, streams three assistant events (the
# second repeats m1's id) and then keeps streaming harmless events for 30s. It only
# ends quickly when someone stops it.
_RUNAWAY_CLAUDE = """
import json, os, sys, time
sys.stdin.read()
with open({marker!r}, "a") as fh:
    fh.write(str(os.getpid()) + "\\n")
for line in {events!r}:
    print(line, flush=True)
deadline = time.time() + 30
while time.time() < deadline:
    print(json.dumps({{"type": "system", "subtype": "tick"}}), flush=True)
    time.sleep(0.1)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "session_id": "s"}}), flush=True)
"""

# Same three events, then the run ends normally.
_FINITE_CLAUDE = """
import json, sys
sys.stdin.read()
for line in {events!r}:
    print(line, flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "session_id": "s",
                  "usage": {{"input_tokens": 120, "output_tokens": 0}}}}), flush=True)
"""

_EVENTS = [
    _assistant("m1", input_tokens=50, output_tokens=10),
    _assistant("m1", input_tokens=50, output_tokens=10),
    _assistant("m2", input_tokens=50, output_tokens=10),
]


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


class LiveEnforcementThroughTheWorkerTests(unittest.TestCase):
    """The budget stop goes through the real watched worker and pipeline (#533)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_git_repo(self.repo)
        self.marker = self.root / "claude-starts.txt"

    def _agent(self, template: str) -> str:
        path = self.root / "claude"
        path.write_text(
            f"#!{sys.executable}\n"
            + template.format(marker=str(self.marker), events=_EVENTS)
        )
        path.chmod(0o755)
        return str(path)

    def _service(self, mode: str, worker_factory) -> ExperimentService:
        env = {"NEXUS_EXPERIMENT_TOKEN_BUDGET": "100", "NEXUS_EXPERIMENT_TOKEN_BUDGET_MODE": mode}
        with patch.dict(os.environ, env, clear=False):
            service = ExperimentService(
                db_path=str(self.root / "agent.db"),
                repo_root=str(self.repo),
                worktrees_root=str(self.root / "worktrees"),
                experiment_log_dir=str(self.root / "logs"),
                worker_factory=worker_factory,
            )
        self.addCleanup(service.close)
        return service

    def _log(self, service: ExperimentService, experiment_id: str, event: str) -> list[dict]:
        path = service.experiment_log_path(experiment_id)
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [entry for entry in entries if entry.get("event") == event]

    def test_run_watched_stops_the_process_when_the_observer_asks(self) -> None:
        agent = self._agent(_RUNAWAY_CLAUDE)
        seen: list[str] = []
        started = time.monotonic()

        run = run_watched(
            [agent], cwd=str(self.root), env={}, input_text="t",
            stall_seconds=20, max_seconds=60, poll_seconds=0.1,
            on_stdout_line=lambda line: seen.append(line) or '"m2"' in line,
        )

        self.assertEqual(run.stop_reason, STOP_REQUESTED)
        self.assertLess(time.monotonic() - started, 15)
        pid = int(self.marker.read_text().split()[0])
        self.assertFalse(_alive(pid))

    def test_enforce_stops_claude_and_the_pipeline_keeps_needs_review(self) -> None:
        agent = self._agent(_RUNAWAY_CLAUDE)
        starts = []

        def factory(_worktree: str) -> ClaudeWorker:
            starts.append(_worktree)
            return ClaudeWorker(claude_bin=agent)

        service = self._service("enforce", factory)
        experiment = service.create_experiment(goal="Add a small helper.")
        started = time.monotonic()

        with patch.object(service, "_verify") as verify:
            result = service.run_experiment(experiment.id)

        self.assertLess(time.monotonic() - started, 20, "the runaway worker was not stopped")
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("stop_reason=live_count_worker_stopped", result.error)
        self.assertIn("budget=100", result.error)
        # m1 (60) + m2 (60); the repeated m1 is not counted again.
        self.assertIn("consumed=120", result.error)
        self.assertEqual(result.implementation_usage["live_api_call_count"], 2)
        self.assertEqual(result.implementation_usage["tokens_consumed"], 120)
        # One paid run only: no fallback, no verification, no repair.
        self.assertEqual(len(starts), 1)
        pids = self.marker.read_text().split()
        self.assertEqual(len(pids), 1)
        self.assertFalse(_alive(int(pids[0])))
        verify.assert_not_called()
        warnings = self._log(service, experiment.id, "token_budget_exceeded")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["mode"], "enforce")
        # The stored outcome is the final one.
        self.assertEqual(service.get_experiment(experiment.id).status, STATUS_NEEDS_REVIEW)

    def test_shadow_crossing_only_logs_and_the_worker_finishes(self) -> None:
        agent = self._agent(_FINITE_CLAUDE)
        service = self._service("shadow", lambda _path: None)
        experiment = service.create_experiment(goal="Observe a Claude stream")
        elog = service.get_experiment_logger(experiment.id)
        callback, state = service._live_worker_usage_callback(experiment.id, elog)

        result = ClaudeWorker(claude_bin=agent).run(
            task="t", working_dir=str(self.root), sandbox="workspace-write",
            timeout_seconds=20, stall_seconds=5, on_live_usage=callback,
        )

        self.assertTrue(result.success, result.error)
        self.assertFalse(result.timed_out)
        self.assertEqual(state, {"tokens": 120, "calls": 2})
        current = service.get_experiment(experiment.id)
        self.assertEqual(current.status, "queued")
        self.assertIsNone(current.error)
        warnings = self._log(service, experiment.id, "token_budget_exceeded")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["mode"], "shadow")
        self.assertEqual(warnings[0]["total"], 120)


class _CountingWorker:
    """A Codex-like worker whose usage arrives only with its final result."""

    def __init__(self, provider: str = "codex", **overrides) -> None:
        self.provider = provider
        self.calls: list[dict] = []
        self.overrides = overrides

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return _worker_result(self.provider, sandbox="workspace-write", **self.overrides)


class EndOfRunEnforcementInThePipelineTests(unittest.TestCase):
    """Codex can only be checked at the end of a run; that stop must also hold (#533)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_git_repo(self.repo)

    def _service(self, worker) -> ExperimentService:
        env = {"NEXUS_EXPERIMENT_TOKEN_BUDGET": "100", "NEXUS_EXPERIMENT_TOKEN_BUDGET_MODE": "enforce"}
        with patch.dict(os.environ, env, clear=False):
            service = ExperimentService(
                db_path=str(self.root / "agent.db"),
                repo_root=str(self.repo),
                worktrees_root=str(self.root / "worktrees"),
                experiment_log_dir=str(self.root / "logs"),
                worker_factory=lambda _path: worker,
            )
        self.addCleanup(service.close)
        return service

    def _assert_budget_stop(self, result) -> None:
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("stop_reason=end_of_run_count", result.error)
        self.assertIn("budget=100", result.error)
        self.assertIn("consumed=150", result.error)

    def test_successful_run_over_budget_skips_verification(self) -> None:
        worker = _CountingWorker(usage={"input_tokens": 100, "output_tokens": 50})
        service = self._service(worker)
        experiment = service.create_experiment(goal="Add a small helper.")

        with patch.object(service, "_verify") as verify:
            result = service.run_experiment(experiment.id)

        self._assert_budget_stop(result)
        self.assertEqual(len(worker.calls), 1)
        verify.assert_not_called()

    def test_failed_run_over_budget_is_not_turned_into_failed_or_timed_out(self) -> None:
        for overrides in (
            {"success": False, "error": "codex exited 1"},
            {"success": False, "timed_out": True, "error": "codex stalled"},
        ):
            with self.subTest(**overrides):
                worker = _CountingWorker(usage={"input_tokens": 100, "output_tokens": 50}, **overrides)
                service = self._service(worker)
                experiment = service.create_experiment(goal="Add a small helper.")

                result = service.run_experiment(experiment.id)

                self._assert_budget_stop(result)
                self.assertEqual(len(worker.calls), 1)

    def test_overload_fallback_does_not_start_after_a_budget_stop(self) -> None:
        worker = _CountingWorker(
            usage={"input_tokens": 100, "output_tokens": 50},
            success=False, error="API Error: 529 Overloaded",
        )
        service = self._service(worker)
        experiment = service.create_experiment(goal="Add a small helper.")
        impl = "modules.experiment_service.service_parts.implementation"

        with patch(f"{impl}.other_provider", return_value="claude"), \
                patch(f"{impl}.build_worker") as build_worker:
            result = service.run_experiment(experiment.id)

        self._assert_budget_stop(result)
        build_worker.assert_not_called()
        self.assertEqual(len(worker.calls), 1)

    def test_context_fallback_does_not_start_after_a_budget_stop(self) -> None:
        from modules.local_coding_worker import CONTEXT_TOO_SMALL

        local = _CountingWorker(
            "local", usage={"input_tokens": 100, "output_tokens": 50},
            success=False, error=f"{CONTEXT_TOO_SMALL}: goal does not fit",
        )
        external = _CountingWorker()
        service = self._service(external)
        service.local_worker_factory = lambda _path: local
        experiment = service.create_experiment(goal="Add a small helper.", complexity="small")
        with patch.object(service, "_must_use_external_provider", return_value=False):
            result = service.run_experiment(experiment.id)

        self._assert_budget_stop(result)
        self.assertEqual(len(local.calls), 1)
        self.assertEqual(external.calls, [])

    def test_repair_run_stopped_live_keeps_needs_review_and_does_not_repeat(self) -> None:
        class LiveStoppedRepair(_CountingWorker):
            def run(self, **kwargs):
                self.calls.append(kwargs)
                # The run observer crosses the budget, so the watchdog stopped Claude.
                self.stopped = kwargs["on_live_usage"]({"input_tokens": 150})
                return _worker_result(
                    "claude", success=False, timed_out=True, usage=None,
                    error="Claude stopped by the run observer",
                )

        worker = LiveStoppedRepair("claude")
        service = self._service(worker)
        experiment = service.create_experiment(goal="Repair the failing test.")
        service._force_status(experiment.id, STATUS_FAILED, test_exit_code=1, error="tests failed")
        failed = service.get_experiment(experiment.id)
        elog = service.get_experiment_logger(experiment.id)

        with patch.object(service, "_verify") as verify:
            result = service._repair_failed_verification(
                experiment.id, str(self.root), elog, failed,
            )

        self.assertTrue(worker.stopped)
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("stop_reason=live_count_worker_stopped", result.error)
        self.assertIn("budget=100", result.error)
        self.assertIn("consumed=150", result.error)
        self.assertEqual(len(worker.calls), 1)
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
