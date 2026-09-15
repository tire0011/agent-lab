"""Issue #193: tasks and experiments record the coding agent that ran them.

Covers the wiring between ``modules.coding_provider`` and the two services
that execute work — no CLI is launched: workers are fakes and the draw is
either seeded or patched.
"""

import random
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.claude_worker import ClaudeWorker
from modules.coding_provider import PROVIDER_CLAUDE, PROVIDER_CODEX
from modules.coding_worker import WorkerResult
from modules.codex_task_service import (
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    CodexTaskService,
)
from modules.codex_worker import CodexWorker
from modules.experiment_service import ExperimentService
from modules.run_store import init_db


def _worker_result(provider: str, **overrides) -> WorkerResult:
    defaults = dict(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        runtime_ms=7,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
        thread_id="tid-1",
        final_message="done",
        usage={"input_tokens": 1, "output_tokens": 2},
        task="task",
        working_dir="/agent",
        sandbox="read-only",
        error=None,
        provider=provider,
    )
    defaults.update(overrides)
    return WorkerResult(**defaults)


class _FakeWorker:
    """Minimal stand-in for a real worker, with a provider name."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return _worker_result(self.provider)


# ---------------------------------------------------------------------------
# CodexTaskService
# ---------------------------------------------------------------------------

class TaskProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")

    def _service(self, **kwargs) -> CodexTaskService:
        service = CodexTaskService(db_path=self.db_path, **kwargs)
        self.addCleanup(service.close)
        return service

    def test_provider_is_drawn_when_the_caller_names_none(self):
        service = self._service()
        with patch(
            "modules.codex_task_service.choose_provider", return_value=PROVIDER_CLAUDE
        ) as draw:
            task = service.create_task(prompt="do it", working_dir="/agent")
        draw.assert_called_once()
        self.assertEqual(task.provider, PROVIDER_CLAUDE)

    def test_named_provider_is_used_as_given(self):
        service = self._service()
        task = service.create_task(
            prompt="do it", working_dir="/agent", provider=PROVIDER_CLAUDE
        )
        self.assertEqual(task.provider, PROVIDER_CLAUDE)

    def test_unknown_provider_is_refused(self):
        service = self._service()
        with self.assertRaises(ValueError):
            service.create_task(prompt="do it", working_dir="/agent", provider="gpt5-cli")

    def test_provider_survives_a_reload(self):
        service = self._service()
        task = service.create_task(
            prompt="do it", working_dir="/agent", provider=PROVIDER_CLAUDE
        )
        self.assertEqual(self._service().get_task(task.id).provider, PROVIDER_CLAUDE)

    def test_the_draw_alternates_across_tasks(self):
        service = self._service(rng=random.Random(20260915))
        with patch("modules.coding_provider.is_available", return_value=True):
            providers = {
                service.create_task(prompt=f"task {i}", working_dir="/agent").provider
                for i in range(40)
            }
        self.assertEqual(providers, {PROVIDER_CODEX, PROVIDER_CLAUDE})

    def test_run_task_builds_the_worker_for_the_recorded_provider(self):
        service = self._service()
        task = service.create_task(
            prompt="do it", working_dir="/agent", provider=PROVIDER_CLAUDE
        )
        fake = _FakeWorker(PROVIDER_CLAUDE)
        with patch(
            "modules.codex_task_service.build_worker", return_value=fake
        ) as build:
            finished = service.run_task(task.id)

        build.assert_called_once_with(PROVIDER_CLAUDE)
        self.assertEqual(finished.status, STATUS_SUCCEEDED)
        self.assertEqual(finished.provider, PROVIDER_CLAUDE)

    def test_an_injected_worker_pins_every_task_to_its_provider(self):
        fake = _FakeWorker(PROVIDER_CLAUDE)
        service = self._service(worker=fake)
        with patch("modules.codex_task_service.build_worker") as build:
            task = service.create_task(prompt="do it", working_dir="/agent")
            service.run_task(task.id)
        build.assert_not_called()
        self.assertEqual(task.provider, PROVIDER_CLAUDE)

    def test_a_test_double_without_a_provider_is_recorded_as_codex(self):
        service = self._service(worker=MagicMock())
        task = service.create_task(prompt="do it", working_dir="/agent")
        self.assertEqual(task.provider, PROVIDER_CODEX)

    def test_a_row_written_before_providers_existed_reads_as_codex(self):
        service = self._service()
        conn = init_db(self.db_path)
        with conn:
            conn.execute(
                """
                INSERT INTO codex_tasks (
                    id, prompt, working_dir, sandbox, timeout_seconds,
                    status, created_at, provider
                ) VALUES ('legacy', 'p', '/agent', 'read-only', 120, ?, '2026-01-01T00:00:00Z', NULL)
                """,
                (STATUS_QUEUED,),
            )
        conn.close()
        self.assertEqual(service.get_task("legacy").provider, PROVIDER_CODEX)

    def test_real_workers_are_built_for_both_providers(self):
        service = self._service()
        codex_task = service.create_task(
            prompt="p", working_dir="/agent", provider=PROVIDER_CODEX
        )
        claude_task = service.create_task(
            prompt="p", working_dir="/agent", provider=PROVIDER_CLAUDE
        )
        self.assertIsInstance(service._worker_for(codex_task), CodexWorker)
        self.assertIsInstance(service._worker_for(claude_task), ClaudeWorker)


# ---------------------------------------------------------------------------
# ExperimentService
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "IDEAS.md").write_text("# IDEAS\n\n- small thing\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_placeholder.py").write_text(
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class _ImplementingWorker(_FakeWorker):
    """Fake worker that actually writes the files the prompt asks for."""

    def run(self, **kwargs):
        self.calls.append(kwargs)
        working_dir = kwargs.get("working_dir")
        match = re.search(r"Affected files:\s*(.+)", kwargs.get("task") or "")
        if working_dir and match:
            for rel_path in (p.strip() for p in match.group(1).split(",")):
                if not rel_path:
                    continue
                target = Path(working_dir) / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text('"""Docstring."""\n')
        return _worker_result(self.provider, sandbox="workspace-write")


class ExperimentProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.worktrees_root = Path(self.tmp.name) / "worktrees"
        self.db_path = str(Path(self.tmp.name) / "agent.db")

    def _service(self, **kwargs) -> ExperimentService:
        service = ExperimentService(
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            **kwargs,
        )
        self.addCleanup(service.close)
        return service

    def test_the_agent_that_implemented_is_recorded(self):
        worker = _ImplementingWorker(PROVIDER_CLAUDE)
        service = self._service(worker_factory=lambda worktree: worker)
        experiment = service.create_experiment(goal="Add a small helper.")

        result = service.run_experiment(experiment.id)

        self.assertEqual(result.implementation_provider, PROVIDER_CLAUDE)
        self.assertEqual(
            service.get_experiment(experiment.id).implementation_provider,
            PROVIDER_CLAUDE,
        )

    def test_the_provider_is_recorded_even_when_the_run_fails(self):
        worker = _FakeWorker(PROVIDER_CLAUDE)
        worker.run = lambda **kwargs: _worker_result(  # type: ignore[assignment]
            PROVIDER_CLAUDE, success=False, timed_out=True, error="timed out"
        )
        service = self._service(worker_factory=lambda worktree: worker)
        experiment = service.create_experiment(goal="Add a small helper.")

        result = service.run_experiment(experiment.id)

        self.assertEqual(result.implementation_provider, PROVIDER_CLAUDE)

    def test_a_test_double_without_a_provider_records_nothing(self):
        double = MagicMock()
        double.run.return_value = _worker_result(PROVIDER_CODEX)
        service = self._service(worker_factory=lambda worktree: double)
        experiment = service.create_experiment(goal="Add a small helper.")

        result = service.run_experiment(experiment.id)

        self.assertIsNone(result.implementation_provider)

    def test_the_default_factory_keeps_one_agent_per_experiment(self):
        service = self._service(rng=random.Random(5))
        with patch("modules.coding_provider.is_available", return_value=True):
            worktree = str(self.worktrees_root / "exp-1")
            first = service.worker_factory(worktree).provider
            again = service.worker_factory(worktree).provider
        self.assertEqual(first, again)

    def test_the_default_factory_alternates_across_experiments(self):
        service = self._service(rng=random.Random(20260915))
        with patch("modules.coding_provider.is_available", return_value=True):
            providers = {
                service.worker_factory(str(self.worktrees_root / f"exp-{i}")).provider
                for i in range(40)
            }
        self.assertEqual(providers, {PROVIDER_CODEX, PROVIDER_CLAUDE})


if __name__ == "__main__":
    unittest.main()
