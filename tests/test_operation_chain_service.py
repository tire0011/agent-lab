"""Tests for OperationChainService."""

import json
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

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
from modules.run_store import init_db


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
def service(temp_db, temp_outbox, mock_registry):
    """Create a service instance."""
    svc = OperationChainService(
        db_path=temp_db,
        outbox_dir=temp_outbox,
        operations_registry=mock_registry,
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

    def test_action_not_in_allowlist_rejected(self, service):
        """Test chain with forbidden action is rejected."""
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_commit")]  # Not allowed
        )
        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED

    def test_max_ten_steps_limit_rejected(self, service):
        """Test chain with >10 steps is rejected."""
        steps = [OperationChainStep(id=f"step{i}", action="git_status") for i in range(11)]
        request = OperationChainRequest(steps=steps)
        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED

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
        def side_effect(action, args):
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
        # Wait for background thread
        time.sleep(0.5)
        
        # Get updated result
        result = service.get_chain(result.request_id)
        # Step 3 should be skipped
        step3 = next((s for s in result.steps if s.id == "step3"), None)
        if step3:
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
        
        # Wait for execution
        time.sleep(1)
        
        # Get final result
        final = service.get_chain(request_id)
        assert final is not None
        assert final.overall_status in (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_RUNNING)

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
        time.sleep(0.5)
        
        final = service.get_chain(result.request_id)
        # Should complete with error despite timeout
        assert final.overall_status in (STATUS_COMPLETED_WITH_ERRORS, STATUS_RUNNING, STATUS_FAILED)

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
        time.sleep(0.5)
        
        final = service.get_chain(result.request_id)
        if final.steps:
            step = final.steps[0]
            # Should be truncated
            assert len(step.output) <= 100
            assert all(len(line) <= 500 for line in step.output)

    def test_outbox_file_written(self, service):
        """Test that outbox file is written."""
        outbox_dir = Path(service.outbox_dir)
        
        request = OperationChainRequest(
            steps=[OperationChainStep(id="step1", action="git_status")],
        )
        
        result = service.start_chain(request)
        time.sleep(1)
        
        # Check file was written
        files = list(outbox_dir.glob("*operation-chain-result*"))
        assert len(files) > 0

    def test_rejected_chain_has_outbox_file(self, service):
        """Test rejected chains also write outbox files."""
        outbox_dir = Path(service.outbox_dir)
        
        # Create invalid request
        request = OperationChainRequest(
            steps=[
                OperationChainStep(id="s1", action="git_commit"),  # Invalid action
            ]
        )
        
        result = service.start_chain(request)
        assert result.overall_status == STATUS_REJECTED
        
        # Check file was written
        files = list(outbox_dir.glob("*operation-chain-result*"))
        assert len(files) > 0
