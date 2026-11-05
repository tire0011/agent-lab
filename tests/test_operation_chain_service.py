"""Tests for OperationChainService."""

import json
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from pydantic import ValidationError

from models import (
    OperationChainRequest,
    OperationChainStep,
    OperationChainResult,
)
from modules.operation_chain_service import (
    OperationChainService,
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_ERRORS,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_RUNNING,
    STATUS_QUEUED,
    STATUS_INTERRUPTED,
)
from modules.operations_registry import OperationsRegistry
from modules.run_store import init_db


def _wait_for_chain(service, request_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        chain = service.get_chain(request_id)
        if chain is None or chain.overall_status not in (STATUS_QUEUED, STATUS_RUNNING):
            return True
        time.sleep(0.02)
    return False

@pytest.fixture
def temp_db():
    """Create a temporary database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = init_db(db_path)
        yield db_path
        conn.close()


@pytest.fixture
def temp_outbox():
    """Create a temporary outbox directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(tmpdir)


@pytest.fixture
def mock_registry():
    """Create a mock operations registry."""
    mock = Mock()
    mock.execute = Mock(return_value=Mock(
        operation="test_op",
        success=True,
        output_lines=["test output"],
        duration_ms=100,
        error_summary=None,
    ))
    mock._specs = {}
    return mock


@pytest.fixture
def temp_repo_root():
    """A stand-in repo root, so tests never reach into the real /agent.

    OperationChainService defaults repo_root to /agent and puts its
    outbox_publisher.lock there. On a machine without /agent the lock cannot be
    created, `_write_outbox_result` raises, and the exception is swallowed by a
    bare `except Exception` - so outbox files silently never appeared. That is
    why the outbox tests passed on the VM and failed on CI runners.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def service(temp_db, mock_registry, temp_repo_root):
    """Create a service instance."""
    outbox = Path(temp_repo_root) / "communication" / "outbox"
    svc = OperationChainService(
        db_path=temp_db,
        outbox_dir=str(outbox),
        operations_registry=mock_registry,
        repo_root=temp_repo_root,
    )
    yield svc
    svc.close()


class TestOperationChainService:
    """Tests for OperationChainService."""

    def test_start_chain_queued(self, service):
        """Test starting a chain returns queued status."""
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )
        result = service.start_chain(request)
        assert result.overall_status == STATUS_QUEUED
        assert result.request_id is not None
        assert len(result.steps) == 0  # Not yet executed

    def test_start_chain_with_explicit_request_id(self, service):
        """Test starting chain with explicit request_id."""
        request_id = "my-chain-123"
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
            request_id=request_id,
        )
        result = service.start_chain(request)
        assert result.request_id == request_id

    def test_duplicate_step_ids_rejected(self, service):
        """Test chain with duplicate step IDs is rejected."""
        request = OperationChainRequest(
            steps=[
                OperationChainStep(id="step1", action="git_status"),
                OperationChainStep(id="step1", action="git_log"),
            ]
        )
        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED
        assert result.rejection_reason is not None

    def test_future_operation_name_is_not_pre_rejected(self, service, mock_registry):
        request = OperationChainRequest(steps=[OperationChainStep(id="step1", action="local_model_status")])
        result = service.start_chain(request)
        assert result.overall_status == STATUS_QUEUED
        assert _wait_for_chain(service, result.request_id)
        current = service.get_chain(result.request_id)
        assert current is not None
        assert current.overall_status in (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS)
        mock_registry.execute.assert_called_with(
            "local_model_status",
            {},
            invocation_id=f"operation-chain:{result.request_id}:step1",
        )

    def test_max_ten_steps_limit_rejected(self):
        """A chain with >10 steps cannot even be constructed."""
        # The cap lives on OperationChainRequest, so it is enforced before
        # start_chain() is ever reached. This test used to call start_chain()
        # and assert STATUS_REJECTED, which was unreachable - the constructor
        # raised first. Nobody saw it: CI collected zero tests from this file.
        steps = [OperationChainStep(id=f"step{i}", action="git_status") for i in range(11)]
        with pytest.raises(ValidationError):
            OperationChainRequest(steps=steps)

    def test_forbidden_pattern_in_argument_rejected(self, service):
        """Test chain with forbidden pattern in arguments is rejected."""
        request = OperationChainRequest(
            steps=[OperationChainStep(
                id="step1",
                action="git_log",
                arguments={"limit": "10; rm -rf /"},  # Forbidden pattern
            )]
        )
        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED

    def test_idempotent_same_request_id_same_content(self, service):
        """Test idempotency: same request_id with same content."""
        request_id = "unique-123"
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
            request_id=request_id,
        )
        # Start first time
        result1 = service.start_chain(request)
        assert result1.request_id == request_id

        # Start again with same content
        result2 = service.start_chain(request)
        assert result2.request_id == request_id

    def test_conflict_same_request_id_different_content(self, service):
        """Test conflict: same request_id with different content."""
        request_id = "conflict-123"
        request1 = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
            request_id=request_id,
        )
        request2 = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_log")],  # Different
            request_id=request_id,
        )
        
        # Start first
        result1 = service.start_chain(request1)
        assert result1.overall_status in (STATUS_QUEUED, STATUS_RUNNING)

        # Try to start conflicting
        result2 = service.start_chain(request2)
        assert result2.overall_status == STATUS_REJECTED
        assert "different content" in result2.rejection_reason.lower()

    def test_get_chain_not_found(self, service):
        """Test getting non-existent chain."""
        result = service.get_chain("nonexistent")
        assert result is None

    def test_list_chains(self, service):
        """Test listing chains."""
        request1 = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )
        request2 = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_log")],
        )
        
        result1 = service.start_chain(request1)
        result2 = service.start_chain(request2)
        
        chains = service.list_chains(limit=10)
        assert len(chains) >= 2

    def test_recover_after_restart(self, temp_db, temp_outbox, mock_registry):
        """Test recovering interrupted chains."""
        service = OperationChainService(
            db_path=temp_db,
            outbox_dir=temp_outbox,
            operations_registry=mock_registry,
        )
        
        # Manually insert a running chain
        conn = init_db(temp_db)
        conn.execute(
            """
            INSERT INTO operation_chains
            (id, request_id, status, created_at, request_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("chain1", "req1", "running", "2024-01-01T00:00:00Z",
             '{"steps": []}', "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        
        # Recover
        service.recover_after_restart()
        
        # Check it's marked interrupted
        conn = init_db(temp_db)
        row = conn.execute("SELECT status FROM operation_chains WHERE id = ?", ("chain1",)).fetchone()
        assert row[0] == STATUS_INTERRUPTED
        conn.close()
        service.close()

    def test_stop_on_error_skips_remaining_steps(self, service):
        """Test stop_on_error stops execution."""
        # Mock registry to fail on second step
        call_count = [0]
        def side_effect(action, args, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                result = Mock()
                result.success = False
                result.output_lines = ["error"]
                result.duration_ms = 100
                result.error_summary = "step 2 failed"
                return result
            result = Mock()
            result.success = True
            result.output_lines = ["ok"]
            result.duration_ms = 100
            result.error_summary = None
            return result
        
        service.operations_registry.execute = side_effect
        
        request = OperationChainRequest(
            steps=[
                OperationChainStep(id="step1", action="git_status"),
                OperationChainStep(id="step2", action="git_log"),
                OperationChainStep(id="step3", action="memory"),
            ],
            stop_on_error=True,
        )
        
        result = service.start_chain(request)
        assert _wait_for_chain(service, result.request_id)

        result = service.get_chain(result.request_id)
        # Step 3 must be there and must be skipped. `if step3:` used to guard
        # this, so a chain that had not got that far yet passed silently.
        step3 = next((s for s in result.steps if s.id == "step3"), None)
        assert step3 is not None, "step3 missing from a finished chain"
        assert step3.status == "skipped"

    def test_successful_chain_execution(self, service):
        """Test successful chain execution."""
        request = OperationChainRequest(
            steps=[
                OperationChainStep(id="step1", action="git_status"),
                OperationChainStep(id="step2", action="memory"),
            ],
        )
        
        result = service.start_chain(request)
        request_id = result.request_id

        assert _wait_for_chain(service, request_id)

        final = service.get_chain(request_id)
        assert final is not None
        # STATUS_RUNNING used to be accepted here, which made the assertion
        # hold whether or not the chain had run at all.
        assert final.overall_status in (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS)

    def test_timeout_handled(self, service):
        """Test operation timeout is handled."""
        import subprocess
        service.operations_registry.execute = Mock(
            side_effect=subprocess.TimeoutExpired("test", 30)
        )
        
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )
        
        result = service.start_chain(request)
        assert _wait_for_chain(service, result.request_id)

        final = service.get_chain(result.request_id)
        # Should complete with error despite timeout. STATUS_RUNNING is no
        # longer accepted: the chain has finished by the time we look.
        assert final.overall_status in (STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED)

    def test_output_truncation(self, service):
        """Test output is truncated in results."""
        # Mock registry to return huge output
        service.operations_registry.execute = Mock(return_value=Mock(
            operation="test",
            success=True,
            output_lines=["x" * 1000 for _ in range(200)],  # More than 100 lines, >500 chars
            duration_ms=100,
            error_summary=None,
        ))
        
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )
        
        result = service.start_chain(request)
        assert _wait_for_chain(service, result.request_id)

        final = service.get_chain(result.request_id)
        # `if final.steps:` used to guard this, so a chain that had not
        # produced a step yet passed without checking any truncation.
        assert final.steps, "a finished chain produced no steps"
        step = final.steps[0]
        assert len(step.output) <= 100
        assert all(len(line) <= 500 for line in step.output)

    def test_outbox_file_written(self, service):
        """Test that outbox file is written."""
        outbox_dir = Path(service.outbox_dir)

        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )

        result = service.start_chain(request)

        # The chain runs in a background thread. A flat sleep(1) was not enough
        # on a loaded host, which is why this failed intermittently once the
        # file started being collected at all.
        deadline = time.time() + 15
        files: list[Path] = []
        while time.time() < deadline:
            files = list(outbox_dir.glob("*operation-chain-result*"))
            if files:
                break
            time.sleep(0.05)
        assert files, f"no outbox file for chain {result.request_id}"

    def test_rejected_chain_has_outbox_file(self, service):
        """Test rejected chains also write outbox files."""
        outbox_dir = Path(service.outbox_dir)

        # A forbidden argument pattern is what actually produces a rejection.
        # This test used to use an unknown action name and assert REJECTED,
        # which contradicted test_start_chain_queued in this same file: the
        # mock registry's _specs is empty, so "git_status" there is exactly as
        # unknown as "git_commit" was here. Unknown actions are not rejected up
        # front - they fail during execution.
        request = OperationChainRequest(
            steps=[
                OperationChainStep(id="s1", action="git_log", arguments={"limit": "1; rm -rf /"}),
            ]
        )

        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED

        # Check file was written
        files = list(outbox_dir.glob("*operation-chain-result*"))
        assert len(files) > 0


def _fake_subprocess_runner(command, **kwargs):
    """Deterministic stand-in for subprocess.run: every command succeeds
    without touching the real system (no git/systemctl/journalctl needed)."""
    return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok\n", stderr="")


@pytest.fixture
def real_registry():
    """Build a real OperationsRegistry (no mocking of `execute`/`_specs`) so
    the regression test below exercises the actual name resolution and
    handler dispatch used by OperationChainService in production."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        meminfo = root / "meminfo"
        meminfo.write_text(
            "MemTotal:       1000 kB\nMemFree:         500 kB\nMemAvailable:    700 kB\n"
            "SwapTotal:       0 kB\nSwapFree:        0 kB\n",
            encoding="utf-8",
        )
        log_path = root / "exp.jsonl"
        log_path.write_text(
            json.dumps({
                "timestamp": "2026-08-14T12:00:00Z",
                "level": "INFO",
                "phase": "adoption",
                "event": "detail",
                "message": "ok",
            }) + "\n",
            encoding="utf-8",
        )
        experiment = SimpleNamespace(
            id="abc12345def67890",
            status="succeeded",
            created_at="2026-08-14T12:00:00Z",
            ended_at="2026-08-14T12:10:00Z",
            test_command=["python", "-m", "unittest"],
            test_exit_code=0,
            git_status=" M modules/demo.py\n",
            error=None,
        )
        adoption = SimpleNamespace(
            id="adoption-1",
            experiment_id=experiment.id,
            status="succeeded",
            started_at="2026-08-14T12:00:00Z",
            ended_at="2026-08-14T12:10:00Z",
            target_branch="main",
            test_exit_code=0,
        )
        experiment_service = MagicMock()
        experiment_service.resolve_experiment.return_value = experiment
        experiment_service.experiment_log_path.return_value = log_path
        experiment_service.get_adoption.return_value = adoption

        registry = OperationsRegistry(
            experiment_service=experiment_service,
            db_path=str(root / "agent.db"),
            repo_root=str(root),
            mailbox_lock_path=str(root / "mailbox.lock"),
            subprocess_runner=_fake_subprocess_runner,
            meminfo_path=str(meminfo),
        )
        try:
            yield registry, experiment, adoption
        finally:
            registry.close()


class TestOperationChainServiceRealRegistryIntegration:
    """Regression test for the canonical-name mismatch that made every chain
    action except `disk` and `memory` fail: OperationChainService used to
    pass underscore action names (e.g. "git_status") straight into
    OperationsRegistry.execute(), while the registry's own catalog/handler
    dispatch was keyed on space-separated names (e.g. "git status"). Name
    validation could not catch this, because both sides disagreed on names
    in the same way. This test runs chain actions through the *real*
    OperationsRegistry (no mocked `execute`/`_specs`) end to end via
    OperationChainService, so a reintroduced name mismatch fails loudly here
    instead of only in production Nexus operation chains.
    """

    def _run_actions(self, real_registry, temp_outbox, actions_with_args):
        registry, _experiment, _adoption = real_registry
        with tempfile.TemporaryDirectory() as db_dir:
            service = OperationChainService(
                db_path=str(Path(db_dir) / "chains.db"),
                outbox_dir=temp_outbox,
                operations_registry=registry,
                repo_root=db_dir,
            )
            try:
                steps = [
                    OperationChainStep(id=f"step_{action}", action=action, arguments=args)
                    for action, args in actions_with_args
                ]
                request = OperationChainRequest(steps=steps)
                result = service.start_chain(request)

                assert _wait_for_chain(service, result.request_id, timeout=15)
                final = service.get_chain(result.request_id)
                return final
            finally:
                service.close()

    def test_chain_actions_reach_registry_handlers(self, real_registry, temp_outbox):
        experiment_id = real_registry[1].id[:8]
        adoption_id = real_registry[2].id

        action_args = {
            "git_status": {},
            "git_log": {"limit": 1},
            "git_worktrees": {},
            "experiment_show": {"experiment_id": experiment_id},
            "experiment_logs": {"experiment_id": experiment_id, "limit": 5},
            "adoption_show": {"adoption_id": adoption_id},
            "service_status": {},
            "mailbox_status": {},
            "service_logs": {"limit": 5},
            "disk": {},
            "memory": {},
            "problems_list": {},
            "inbox_status": {},
            "outbox_status": {},
            "help": {},
            "process_status": {},
        }

        actions = list(action_args.items())
        # OperationChainRequest caps chains at 10 steps, so split into batches.
        batch_size = 10
        for start in range(0, len(actions), batch_size):
            batch = actions[start:start + batch_size]
            final = self._run_actions(real_registry, temp_outbox, batch)

            assert final is not None, "chain did not reach a terminal status in time"
            assert final.overall_status == STATUS_COMPLETED, (
                f"chain did not complete cleanly: {final.overall_status} "
                f"steps={[(s.action, s.status, s.error) for s in final.steps]}"
            )
            for step in final.steps:
                assert step.status == "success", (step.action, step.error)
                assert step.error is None
                # The historical bug produced exactly this error for every
                # non-disk/memory action; guard against its reintroduction
                # even if some other assertion above is loosened later.
                assert step.error != "Operation ist nicht erlaubt."
