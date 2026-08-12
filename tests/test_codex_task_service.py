"""Unit tests for modules.codex_task_service.CodexTaskService.

The real CodexWorker/codex process is never invoked; a fake worker object is
injected instead so tests consume no Codex quota.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.codex_worker import CodexResult
from modules.codex_task_service import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    CodexTaskService,
    TaskNotFoundError,
    TaskNotRunnableError,
)


def _make_result(**overrides) -> CodexResult:
    defaults = dict(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        runtime_ms=42,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
        thread_id="tid-1",
        final_message="done",
        usage={"input_tokens": 10, "output_tokens": 5},
        task="task",
        working_dir="/agent",
        sandbox="read-only",
        error=None,
    )
    defaults.update(overrides)
    return CodexResult(**defaults)


class CodexTaskServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.worker = MagicMock()
        self.service = CodexTaskService(worker=self.worker, db_path=self.db_path)
        self.addCleanup(self.service.close)

    def _new_service(self) -> CodexTaskService:
        """A second service instance pointed at the same db file (reload)."""
        svc = CodexTaskService(worker=MagicMock(), db_path=self.db_path)
        self.addCleanup(svc.close)
        return svc


class CreateAndLoadTests(CodexTaskServiceTestCase):
    def test_create_task_is_queued(self):
        task = self.service.create_task(
            prompt="do something", working_dir="/agent", sandbox="read-only",
            timeout_seconds=30,
        )
        self.assertEqual(task.status, STATUS_QUEUED)
        self.assertTrue(task.id)
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.ended_at)

    def test_task_can_be_reloaded(self):
        task = self.service.create_task(
            prompt="inspect readme", working_dir="/agent", sandbox="read-only",
            timeout_seconds=60,
        )
        # Reload via a fresh service instance pointed at the same db file.
        other = self._new_service()
        loaded = other.get_task(task.id)
        self.assertEqual(loaded.id, task.id)
        self.assertEqual(loaded.prompt, "inspect readme")
        self.assertEqual(loaded.status, STATUS_QUEUED)

    def test_get_nonexistent_task_raises(self):
        with self.assertRaises(TaskNotFoundError):
            self.service.get_task("does-not-exist")

    def test_list_tasks_sorted_and_limited(self):
        ids = []
        for i in range(5):
            t = self.service.create_task(
                prompt=f"task {i}", working_dir="/agent", sandbox="read-only",
                timeout_seconds=10,
            )
            ids.append(t.id)

        tasks = self.service.list_tasks(limit=3)
        self.assertEqual(len(tasks), 3)
        # Most recently created first.
        self.assertEqual([t.id for t in tasks], list(reversed(ids))[:3])

    def test_create_task_rejects_empty_prompt(self):
        with self.assertRaises(ValueError):
            self.service.create_task(prompt="   ", working_dir="/agent")

    def test_create_task_rejects_invalid_sandbox(self):
        with self.assertRaises(ValueError):
            self.service.create_task(
                prompt="task", working_dir="/agent", sandbox="not-a-mode"
            )


class RunTaskSuccessTests(CodexTaskServiceTestCase):
    def test_successful_worker_call_yields_succeeded(self):
        self.worker.run.return_value = _make_result(success=True, exit_code=0)
        task = self.service.create_task(prompt="task", working_dir="/agent")

        result_task = self.service.run_task(task.id)

        self.assertEqual(result_task.status, STATUS_SUCCEEDED)
        self.assertEqual(result_task.thread_id, "tid-1")
        self.assertEqual(result_task.final_message, "done")
        self.assertEqual(result_task.usage, {"input_tokens": 10, "output_tokens": 5})
        self.assertIsNotNone(result_task.started_at)
        self.assertIsNotNone(result_task.ended_at)
        self.assertIsNone(result_task.error)

    def test_status_is_running_before_worker_invoked(self):
        seen_status = {}

        def fake_run(**kwargs):
            # At call time, the persisted status must already be 'running'.
            reloaded = self.service.get_task(task.id)
            seen_status["status"] = reloaded.status
            return _make_result()

        self.worker.run.side_effect = fake_run
        task = self.service.create_task(prompt="task", working_dir="/agent")
        self.service.run_task(task.id)

        self.assertEqual(seen_status["status"], STATUS_RUNNING)

    def test_worker_called_with_stored_task_parameters(self):
        self.worker.run.return_value = _make_result()
        task = self.service.create_task(
            prompt="do the thing", working_dir="/agent", sandbox="read-only",
            timeout_seconds=99,
        )
        self.service.run_task(task.id)

        self.worker.run.assert_called_once_with(
            task="do the thing",
            working_dir="/agent",
            sandbox="read-only",
            timeout_seconds=99,
        )

    def test_public_api_does_not_pass_output_schema(self):
        # The public /codex/tasks API must remain unaffected by the
        # experiment service's structured output_schema feature: no
        # output_schema kwarg is ever passed to the worker here.
        self.worker.run.return_value = _make_result()
        task = self.service.create_task(prompt="do the thing", working_dir="/agent", sandbox="read-only")
        self.service.run_task(task.id)

        call_kwargs = self.worker.run.call_args.kwargs
        self.assertNotIn("output_schema", call_kwargs)


class RunTaskFailureTests(CodexTaskServiceTestCase):
    def test_worker_error_yields_failed(self):
        self.worker.run.return_value = _make_result(
            success=False, exit_code=1, error=None
        )
        task = self.service.create_task(prompt="task", working_dir="/agent")
        result_task = self.service.run_task(task.id)
        self.assertEqual(result_task.status, STATUS_FAILED)

    def test_worker_config_error_yields_failed_with_message(self):
        self.worker.run.return_value = _make_result(
            success=False, exit_code=None, error="working_dir does not exist"
        )
        task = self.service.create_task(prompt="task", working_dir="/agent")
        result_task = self.service.run_task(task.id)
        self.assertEqual(result_task.status, STATUS_FAILED)
        self.assertIn("does not exist", result_task.error)

    def test_timeout_yields_timed_out(self):
        self.worker.run.return_value = _make_result(
            success=False, exit_code=None, timed_out=True,
            error="Codex process timed out after 5s",
        )
        task = self.service.create_task(
            prompt="task", working_dir="/agent", timeout_seconds=5
        )
        result_task = self.service.run_task(task.id)
        self.assertEqual(result_task.status, STATUS_TIMED_OUT)
        self.assertTrue(result_task.timed_out)

    def test_worker_exception_leaves_task_failed_not_running(self):
        self.worker.run.side_effect = RuntimeError("boom")
        task = self.service.create_task(prompt="task", working_dir="/agent")

        result_task = self.service.run_task(task.id)

        self.assertEqual(result_task.status, STATUS_FAILED)
        self.assertNotEqual(result_task.status, STATUS_RUNNING)
        self.assertIn("boom", result_task.error)


class DoubleStartTests(CodexTaskServiceTestCase):
    def test_cannot_start_task_twice(self):
        self.worker.run.return_value = _make_result()
        task = self.service.create_task(prompt="task", working_dir="/agent")

        self.service.run_task(task.id)  # first run succeeds

        with self.assertRaises(TaskNotRunnableError):
            self.service.run_task(task.id)

    def test_running_task_cannot_be_started_again(self):
        # Simulate a task stuck in 'running' by manipulating state directly.
        self.worker.run.return_value = _make_result()
        task = self.service.create_task(prompt="task", working_dir="/agent")
        self.service._conn.execute(
            "UPDATE codex_tasks SET status = 'running' WHERE id = ?", (task.id,)
        )
        self.service._conn.commit()

        with self.assertRaises(TaskNotRunnableError):
            self.service.run_task(task.id)

    def test_run_nonexistent_task_raises(self):
        with self.assertRaises(TaskNotFoundError):
            self.service.run_task("missing-id")


class LimitsTests(CodexTaskServiceTestCase):
    def test_prompt_is_truncated_to_configured_limit(self):
        svc = CodexTaskService(
            worker=MagicMock(), db_path=self.db_path, max_prompt_bytes=20
        )
        self.addCleanup(svc.close)
        long_prompt = "x" * 100
        task = svc.create_task(prompt=long_prompt, working_dir="/agent")
        self.assertLess(len(task.prompt.encode("utf-8")), 100)
        self.assertIn("[... truncated]", task.prompt)

    def test_final_message_is_truncated_to_configured_limit(self):
        svc = CodexTaskService(
            worker=MagicMock(), db_path=self.db_path, max_final_message_bytes=10
        )
        self.addCleanup(svc.close)
        svc.worker.run.return_value = _make_result(final_message="y" * 100)
        task = svc.create_task(prompt="task", working_dir="/agent")
        result_task = svc.run_task(task.id)
        self.assertIn("[... truncated]", result_task.final_message)

    def test_error_message_is_truncated_to_configured_limit(self):
        svc = CodexTaskService(
            worker=MagicMock(), db_path=self.db_path, max_error_bytes=10
        )
        self.addCleanup(svc.close)
        svc.worker.run.return_value = _make_result(
            success=False, exit_code=None, error="z" * 100
        )
        task = svc.create_task(prompt="task", working_dir="/agent")
        result_task = svc.run_task(task.id)
        self.assertIn("[... truncated]", result_task.error)


class UsageAndThreadIdTests(CodexTaskServiceTestCase):
    def test_usage_and_thread_id_are_persisted(self):
        self.worker.run.return_value = _make_result(
            thread_id="thread-xyz", usage={"input_tokens": 42, "output_tokens": 7}
        )
        task = self.service.create_task(prompt="task", working_dir="/agent")
        result_task = self.service.run_task(task.id)

        self.assertEqual(result_task.thread_id, "thread-xyz")
        self.assertEqual(result_task.usage, {"input_tokens": 42, "output_tokens": 7})

        # Persisted across reload too.
        reloaded = self._new_service().get_task(task.id)
        self.assertEqual(reloaded.thread_id, "thread-xyz")
        self.assertEqual(reloaded.usage, {"input_tokens": 42, "output_tokens": 7})


class InterruptedTaskRecoveryTests(CodexTaskServiceTestCase):
    def test_orphaned_running_tasks_marked_failed(self):
        task = self.service.create_task(prompt="task", working_dir="/agent")
        # Simulate a crash mid-run: status stuck at 'running'.
        self.service._conn.execute(
            "UPDATE codex_tasks SET status = 'running', started_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", task.id),
        )
        self.service._conn.commit()

        marked = self.service.mark_interrupted_tasks_failed()

        self.assertIn(task.id, marked)
        reloaded = self.service.get_task(task.id)
        self.assertEqual(reloaded.status, STATUS_FAILED)
        self.assertIsNotNone(reloaded.error)
        self.worker.run.assert_not_called()

    def test_queued_tasks_are_not_affected_by_recovery(self):
        task = self.service.create_task(prompt="task", working_dir="/agent")
        self.service.mark_interrupted_tasks_failed()
        reloaded = self.service.get_task(task.id)
        self.assertEqual(reloaded.status, STATUS_QUEUED)

    def test_terminal_tasks_are_not_affected_by_recovery(self):
        self.worker.run.return_value = _make_result()
        task = self.service.create_task(prompt="task", working_dir="/agent")
        self.service.run_task(task.id)
        self.service.mark_interrupted_tasks_failed()
        reloaded = self.service.get_task(task.id)
        self.assertEqual(reloaded.status, STATUS_SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
