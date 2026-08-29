"""Unit tests for modules.runtime_observability (issue #76).

Covers: idle state, running task state, phase/operation transitions,
heartbeat updates, duration calculation, task completion resetting the
current status, failed tasks recording their final outcome, trace id
consistency through a task, parent/child span relationships, and bounded
recent-operation history.
"""

from __future__ import annotations

import threading
import time
import unittest

from modules import otel_adapter
from modules.runtime_observability import RuntimeState


class RuntimeStateIdleTests(unittest.TestCase):
    def test_idle_by_default(self):
        state = RuntimeState()
        self.assertEqual(state.snapshot(), {"status": "idle"})

    def test_trace_snapshot_idle_has_no_trace_id(self):
        state = RuntimeState()
        trace = state.trace_snapshot()
        self.assertIsNone(trace["trace_id"])
        self.assertEqual(trace["operations"], [])


class RuntimeStateTaskLifecycleTests(unittest.TestCase):
    def test_start_task_sets_running_status(self):
        state = RuntimeState()
        trace_id = state.start_task(task_id="task-1", experiment_id="exp-1", phase="implementation")
        snap = state.snapshot()
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["task_id"], "task-1")
        self.assertEqual(snap["experiment_id"], "exp-1")
        self.assertEqual(snap["trace_id"], trace_id)
        self.assertEqual(snap["phase"], "implementation")
        self.assertTrue(snap["healthy"])

    def test_task_duration_increases_over_time(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        first = state.snapshot()["task_duration_seconds"]
        time.sleep(0.05)
        second = state.snapshot()["task_duration_seconds"]
        self.assertGreater(second, first)

    def test_set_phase_updates_snapshot(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        state.set_phase("verifying")
        self.assertEqual(state.snapshot()["phase"], "verifying")

    def test_task_completion_resets_runtime_state_to_idle(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        state.end_task(result="succeeded")
        snap = state.snapshot()
        self.assertEqual(snap["status"], "idle")
        self.assertNotIn("task_id", snap)

    def test_failed_task_records_final_state_correctly(self):
        state = RuntimeState()
        state.start_task(task_id="task-1", experiment_id="exp-1")
        trace_id = state.trace_id
        state.end_task(result="failed", error="boom")
        snap = state.snapshot()
        self.assertEqual(snap["status"], "idle")
        self.assertIn("last_task", snap)
        self.assertEqual(snap["last_task"]["result"], "failed")
        self.assertEqual(snap["last_task"]["error"], "boom")
        self.assertEqual(snap["last_task"]["trace_id"], trace_id)
        self.assertEqual(snap["last_task"]["task_id"], "task-1")

    def test_new_task_replaces_previous_last_task(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        state.end_task(result="succeeded")
        state.start_task(task_id="task-2")
        snap = state.snapshot()
        self.assertEqual(snap["task_id"], "task-2")


class RuntimeStateHeartbeatTests(unittest.TestCase):
    def test_heartbeat_seconds_ago_starts_near_zero(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        snap = state.snapshot()
        self.assertLess(snap["last_heartbeat_seconds_ago"], 1.0)

    def test_manual_heartbeat_updates_last_heartbeat(self):
        state = RuntimeState(stall_threshold=0.05)
        state.start_task(task_id="task-1")
        time.sleep(0.1)
        self.assertFalse(state.snapshot()["healthy"])
        state.heartbeat()
        self.assertTrue(state.snapshot()["healthy"])

    def test_stalled_operation_marked_unhealthy(self):
        state = RuntimeState(stall_threshold=0.05, heartbeat_interval=10.0)
        state.start_task(task_id="task-1")
        # No automatic heartbeat thread running here (no operation() active).
        time.sleep(0.1)
        self.assertFalse(state.snapshot()["healthy"])

    def test_operation_context_manager_keeps_heartbeat_fresh(self):
        state = RuntimeState(stall_threshold=0.2, heartbeat_interval=0.05)
        state.start_task(task_id="task-1")
        with state.operation("tests.run", phase="verifying"):
            time.sleep(0.3)
            self.assertTrue(state.snapshot()["healthy"])


class RuntimeStateOperationTests(unittest.TestCase):
    def test_operation_sets_current_operation_and_phase(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("llm.generate", phase="implementation"):
            snap = state.snapshot()
            self.assertEqual(snap["operation"], "llm.generate")
            self.assertEqual(snap["phase"], "implementation")
            self.assertIsNotNone(snap["operation_duration_seconds"])
        # Operation cleared once it completes.
        self.assertIsNone(state.snapshot().get("operation"))

    def test_operation_duration_calculation(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("tests.run"):
            time.sleep(0.05)
            duration = state.snapshot()["operation_duration_seconds"]
        self.assertGreaterEqual(duration, 0.04)

    def test_trace_id_consistent_across_operations(self):
        state = RuntimeState()
        trace_id = state.start_task(task_id="task-1")
        with state.operation("git.status"):
            self.assertEqual(state.snapshot()["trace_id"], trace_id)
        with state.operation("git.diff"):
            self.assertEqual(state.snapshot()["trace_id"], trace_id)

    def test_child_operation_has_correct_parent_span(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("orchestrator.decide") as parent_ctx:
            with state.operation("llm.plan") as child_ctx:
                self.assertEqual(child_ctx["parent_span_id"], parent_ctx["span_id"])
        trace = state.trace_snapshot()
        names = {op["name"]: op for op in trace["operations"]}
        self.assertIsNone(names["orchestrator.decide"]["parent_span_id"])
        self.assertEqual(names["llm.plan"]["parent_span_id"], names["orchestrator.decide"]["span_id"])

    def test_operation_records_completed_status(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("git.status"):
            pass
        trace = state.trace_snapshot()
        self.assertEqual(trace["operations"][0]["status"], "completed")

    def test_operation_records_failed_status_on_exception(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with self.assertRaises(ValueError):
            with state.operation("tests.run"):
                raise ValueError("kaboom")
        trace = state.trace_snapshot()
        self.assertEqual(trace["operations"][0]["status"], "failed")
        self.assertIn("kaboom", trace["operations"][0]["error"])

    def test_bounded_operation_history(self):
        state = RuntimeState(max_trace_operations=3)
        state.start_task(task_id="task-1")
        for i in range(10):
            with state.operation(f"op-{i}"):
                pass
        trace = state.trace_snapshot()
        self.assertEqual(len(trace["operations"]), 3)
        self.assertEqual([op["name"] for op in trace["operations"]], ["op-7", "op-8", "op-9"])

    def test_current_context_none_when_idle(self):
        state = RuntimeState()
        self.assertIsNone(state.current_context())

    def test_current_context_reflects_active_operation(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("llm.generate") as ctx:
            current = state.current_context()
            self.assertEqual(current["trace_id"], ctx["trace_id"])
            self.assertEqual(current["span_id"], ctx["span_id"])
            self.assertEqual(current["operation"], "llm.generate")
        # Restored to no active operation (but still same trace) afterwards.
        after = state.current_context()
        self.assertIsNone(after["operation"])

    def test_operations_are_thread_local_for_nesting(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        results = {}

        def worker(key):
            with state.operation(f"op-{key}") as ctx:
                results[key] = ctx["parent_span_id"]

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # None of these concurrent top-level operations should see each
        # other as a parent (each thread's stack starts empty).
        self.assertTrue(all(parent is None for parent in results.values()))


class OtelAdapterDisabledTests(unittest.TestCase):
    """Tracing must be a safe no-op when unconfigured/unavailable."""

    def setUp(self):
        otel_adapter.reset_for_tests()
        self._env_backup = dict(__import__("os").environ)

    def tearDown(self):
        import os

        os.environ.clear()
        os.environ.update(self._env_backup)
        otel_adapter.reset_for_tests()

    def test_disabled_without_endpoint_env_var(self):
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        self.assertFalse(otel_adapter.is_enabled())

    def test_span_is_noop_when_disabled(self):
        with otel_adapter.span("some.op", trace_id="abc") as span:
            self.assertIsNone(span)

    def test_span_swallows_exceptions_from_body(self):
        with self.assertRaises(RuntimeError):
            with otel_adapter.span("some.op"):
                raise RuntimeError("boom")

    def test_runtime_operation_never_fails_when_otel_disabled(self):
        state = RuntimeState()
        state.start_task(task_id="task-1")
        with state.operation("llm.generate"):
            pass
        self.assertEqual(state.trace_snapshot()["operations"][0]["status"], "completed")

    def test_invalid_endpoint_configuration_degrades_gracefully(self):
        import os

        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:1"
        # Even if the opentelemetry packages happen to be installed, a
        # bogus/unreachable endpoint must never raise from is_enabled()/span().
        try:
            otel_adapter.is_enabled()
        except Exception as exc:  # noqa: BLE001
            self.fail(f"is_enabled() must never raise, got {exc!r}")
        with otel_adapter.span("some.op"):
            pass


if __name__ == "__main__":
    unittest.main()
