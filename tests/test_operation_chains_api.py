"""API tests for Operation Chain endpoints."""

import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from api import app


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    """Ensure dependency_overrides set by one test never leak into another.

    ``app`` is a module-level FastAPI singleton shared by every test file in
    the suite, so leftover overrides (or a lingering absence of an override,
    which lets a Depends() call fall through to the real, non-test service)
    can silently change behavior for unrelated tests that run later in the
    same pytest process.
    """
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def override_chain_service():
    """Override chain service in tests."""
    from modules.operation_chain_service import OperationChainService
    from models import OperationChainResult, OperationChainStepResult
    
    mock = Mock(spec=OperationChainService)
    mock.start_chain = Mock(return_value=OperationChainResult(
        request_id="test-123",
        overall_status="queued",
        created_at="2024-01-01T00:00:00Z",
        steps_successful=0,
        steps_failed=0,
    ))
    mock.get_chain = Mock(return_value=OperationChainResult(
        request_id="test-123",
        overall_status="completed",
        created_at="2024-01-01T00:00:00Z",
        started_at="2024-01-01T00:00:00Z",
        ended_at="2024-01-01T00:00:10Z",
        duration_ms=10000,
        steps_successful=1,
        steps_failed=0,
        steps=[
            OperationChainStepResult(
                id="step1",
                action="git_status",
                status="success",
                started_at="2024-01-01T00:00:00Z",
                duration_ms=100,
                output=["M file.txt"],
                error=None,
                truncated=False,
            )
        ],
    ))
    mock.list_chains = Mock(return_value=[])
    return mock


class TestOperationChainAPI:
    """Tests for operation chain API endpoints."""

    def test_start_chain_unauthorized(self, client):
        """Test POST /operations/chains without API key."""
        response = client.post(
            "/operations/chains",
            json={
                "steps": [
                    {"id": "step1", "action": "git_status"}
                ]
            }
        )
        assert response.status_code == 401

    def test_start_chain_authorized(self, client, override_chain_service, monkeypatch):
        """Test POST /operations/chains with API key."""
        from api import get_operation_chain_service
        monkeypatch.setenv("AGENT_API_KEY", "test-key")
        app.dependency_overrides[get_operation_chain_service] = lambda: override_chain_service
        
        response = client.post(
            "/operations/chains",
            json={
                "steps": [
                    {"id": "step1", "action": "git_status"}
                ]
            },
            headers={"x-api-key": "test-key"}
        )
        assert response.status_code == 202
        data = response.json()
        assert data["request_id"] == "test-123"
        assert data["overall_status"] == "queued"

    def test_get_chain_unauthorized(self, client):
        """Test GET /operations/chains/{request_id} without API key."""
        response = client.get("/operations/chains/test-123")
        assert response.status_code == 401

    def test_get_chain_not_found(self, client, override_chain_service, monkeypatch):
        """Test GET /operations/chains/{request_id} when not found."""
        from api import get_operation_chain_service
        monkeypatch.setenv("AGENT_API_KEY", "test-key")
        mock_svc = Mock()
        mock_svc.get_chain = Mock(return_value=None)
        app.dependency_overrides[get_operation_chain_service] = lambda: mock_svc
        
        response = client.get(
            "/operations/chains/nonexistent",
            headers={"x-api-key": "test-key"}
        )
        assert response.status_code == 404

    def test_get_chain_authorized(self, client, override_chain_service, monkeypatch):
        """Test GET /operations/chains/{request_id} with API key."""
        from api import get_operation_chain_service
        monkeypatch.setenv("AGENT_API_KEY", "test-key")
        app.dependency_overrides[get_operation_chain_service] = lambda: override_chain_service
        
        response = client.get(
            "/operations/chains/test-123",
            headers={"x-api-key": "test-key"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "test-123"
        assert data["overall_status"] == "completed"

    def test_list_chains_unauthorized(self, client):
        """Test GET /operations/chains without API key."""
        response = client.get("/operations/chains")
        assert response.status_code == 401

    def test_list_chains_authorized(self, client, override_chain_service, monkeypatch):
        """Test GET /operations/chains with API key."""
        from api import get_operation_chain_service
        monkeypatch.setenv("AGENT_API_KEY", "test-key")
        app.dependency_overrides[get_operation_chain_service] = lambda: override_chain_service
        
        response = client.get(
            "/operations/chains?limit=20",
            headers={"x-api-key": "test-key"}
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_operations_catalog_unauthorized(self, client):
        """Test GET /operations/catalog without API key."""
        response = client.get("/operations/catalog")
        assert response.status_code == 401

    def test_operations_catalog_authorized(self, client, monkeypatch):
        """Test GET /operations/catalog with API key uses the canonical
        (underscore) operation names shared with the chain allowlist."""
        from api import get_operations_service
        from modules.operations_registry import OperationSpec

        monkeypatch.setenv("AGENT_API_KEY", "test-key")
        mock_svc = Mock()
        mock_svc.catalog = Mock(return_value=[
            OperationSpec("git_status", "Git status"),
        ])
        app.dependency_overrides[get_operations_service] = lambda: mock_svc

        response = client.get(
            "/operations/catalog",
            headers={"x-api-key": "test-key"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "operations" in data
        assert data["operations"][0]["operation"] == "git_status"
