"""Tests for modules/maintenance_service.py and /maintenance/* API endpoints."""

from __future__ import annotations

import os
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

# Must be set before api.py is imported (it reads os.environ["AGENT_API_KEY"] at module level).
# conftest has already set it by the time this runs, so this is a fallback for
# importing the module directly - and it has to name the same key the rest of
# the suite uses, or it reintroduces the mismatch it looks like it is avoiding.
# The endpoint tests below do not rely on it: they monkeypatch api.API_KEY to
# _MAINT_API_KEY, deliberately a different value, to prove the override works.
os.environ.setdefault("AGENT_API_KEY", "test-key")

from modules.maintenance_service import (
    MaintenanceAuthError,
    MaintenanceDisabledError,
    MaintenanceOperationError,
    MaintenanceService,
    _redact,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_service(**kwargs) -> MaintenanceService:
    return MaintenanceService(
        repo_root="/tmp/fakerepo",
        local_model_url="http://127.0.0.1:8080/v1",
        test_cmd=["echo", "tests_done"],
        **kwargs,
    )


def _enabled_env(monkeypatch, token: str = "secret123", expiry: str | None = None) -> None:
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    monkeypatch.setenv("MAINTENANCE_TOKEN", token)
    if expiry:
        monkeypatch.setenv("MAINTENANCE_EXPIRY", expiry)
    else:
        monkeypatch.delenv("MAINTENANCE_EXPIRY", raising=False)


# ── Unit tests: is_enabled / check_auth ──────────────────────────────────────

def test_service_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    assert MaintenanceService.is_enabled() is False


def test_service_enabled_when_env_true(monkeypatch):
    _enabled_env(monkeypatch)
    assert MaintenanceService.is_enabled() is True


def test_service_disabled_when_env_not_true(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_ENABLED", "false")
    assert MaintenanceService.is_enabled() is False


def test_service_disabled_after_expiry(monkeypatch):
    _enabled_env(monkeypatch, expiry="2000-01-01T00:00:00Z")
    assert MaintenanceService.is_enabled() is False


def test_service_enabled_before_expiry(monkeypatch):
    _enabled_env(monkeypatch, expiry="2099-01-01T00:00:00Z")
    assert MaintenanceService.is_enabled() is True


def test_check_auth_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    svc = _make_service()
    with pytest.raises(MaintenanceDisabledError):
        svc.check_auth("anytoken")


def test_check_auth_raises_on_wrong_token(monkeypatch):
    _enabled_env(monkeypatch, token="correct")
    svc = _make_service()
    with pytest.raises(MaintenanceAuthError):
        svc.check_auth("wrong")


def test_check_auth_passes_with_correct_token(monkeypatch):
    _enabled_env(monkeypatch, token="correct")
    svc = _make_service()
    svc.check_auth("correct")  # must not raise


def test_check_auth_raises_when_token_not_configured(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    monkeypatch.delenv("MAINTENANCE_TOKEN", raising=False)
    svc = _make_service()
    with pytest.raises(MaintenanceDisabledError):
        svc.check_auth("anything")


def test_expired_service_raises_disabled(monkeypatch):
    _enabled_env(monkeypatch, token="tok", expiry="2000-01-01T00:00:00Z")
    svc = _make_service()
    with pytest.raises(MaintenanceDisabledError):
        svc.check_auth("tok")


# ── Unit tests: execute / dispatch ───────────────────────────────────────────

def test_execute_unknown_operation_raises(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with pytest.raises(MaintenanceOperationError, match="unknown operation"):
        svc.execute("rm_rf_slash", "tok")


def test_execute_records_audit_on_success(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="(clean)", stderr="")
        svc.execute("git_status", "tok")
    entries = svc.audit_log
    assert len(entries) == 1
    assert entries[0].operation == "git_status"
    assert entries[0].outcome == "ok"


def test_execute_records_audit_on_forbidden(monkeypatch):
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    svc = _make_service()
    with pytest.raises(MaintenanceDisabledError):
        svc.execute("git_status", "tok")
    assert svc.audit_log[0].outcome == "expired"


def test_execute_records_audit_on_bad_token(monkeypatch):
    _enabled_env(monkeypatch, token="right")
    svc = _make_service()
    with pytest.raises(MaintenanceAuthError):
        svc.execute("git_status", "wrong")
    assert svc.audit_log[0].outcome == "forbidden"


# ── Unit tests: git operations ────────────────────────────────────────────────

def test_git_pull(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Already up to date.\n", stderr="")
        result = svc.execute("git_pull", "tok")
    assert "Already up to date" in result["output"]


def test_git_status_clean(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = svc.execute("git_status", "tok")
    assert result["output"] == "(clean)"


def test_git_log(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="abc1234 Initial commit\n", stderr=""
        )
        result = svc.execute("git_log", "tok", {"lines": 5})
    assert "abc1234" in result["output"]


def test_git_log_rejects_invalid_lines(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with pytest.raises(MaintenanceOperationError, match="integer"):
        svc.execute("git_log", "tok", {"lines": "banana"})


def test_git_log_clamps_lines_to_max(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        svc.execute("git_log", "tok", {"lines": 9999})
    call_args = mock_run.call_args[0][0]
    assert "--max-count=50" in call_args


# ── Unit tests: service operations ───────────────────────────────────────────

def test_service_status_allowed(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
        result = svc.execute("service_status", "tok", {"service": "llama-server"})
    assert result["active"] is True
    assert result["service"] == "llama-server"


def test_service_status_inactive(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="inactive\n", stderr="")
        result = svc.execute("service_status", "tok", {"service": "agent-api"})
    assert result["active"] is False


def test_arbitrary_service_name_is_forwarded(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="active", stderr="")
        result = svc.execute("service_status", "tok", {"service": "sshd"})
    assert result["service"] == "sshd"



def test_service_logs(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Aug 19 12:00:00 host llama-server: started\n", stderr=""
        )
        result = svc.execute("service_logs", "tok", {"service": "llama-server", "lines": 10})
    assert "llama-server" in result["output"]


def test_service_logs_accepts_arbitrary_service(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="nginx log", stderr="")
        result = svc.execute("service_logs", "tok", {"service": "nginx"})
    assert result["service"] == "nginx"



# ── Unit tests: process_status ────────────────────────────────────────────────

def test_process_status_returns_unfiltered_output(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    ps_output = (
        "USER  PID %CPU COMMAND\n"
        "agent 1234 0.1 llama-server --port 8080\n"
        "agent 5678 0.5 python agent.py --use-router\n"
        "root  999  0.0 /sbin/init\n"
    )
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=ps_output, stderr="")
        result = svc.execute("process_status", "tok")
    output = result["output"]
    assert "llama-server" in output
    assert "agent.py" in output
    assert "/sbin/init" in output


# ── Unit tests: background task lifecycle ────────────────────────────────────

def test_run_router_test_requires_prompt(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with pytest.raises(MaintenanceOperationError, match="prompt"):
        svc.execute("run_router_test", "tok", {})


def test_run_tests_background_task_queued(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()

    started = threading.Event()
    # Held until the test has seen "running", instead of sleeping and hoping
    # the observation lands inside the window.
    may_finish = threading.Event()

    def slow_run(*args, **kwargs):
        started.set()
        assert may_finish.wait(timeout=10)
        return MagicMock(returncode=0, stdout="5 passed", stderr="")

    with patch("modules.maintenance_service.subprocess.run", side_effect=slow_run):
        result = svc.execute("run_tests", "tok")
        task_id = result["task_id"]
        assert result["status"] == "queued"

        assert started.wait(timeout=10)
        status = svc.execute("test_task_status", "tok", {"task_id": task_id})
        # Strictly "running": the fake is blocked until we release it, so it
        # cannot have finished. "succeeded" was only accepted before because
        # the fake's sleep might have elapsed first.
        assert status["status"] == "running"

        may_finish.set()
        assert svc.wait_for_task(task_id)
        final = svc.execute("test_task_result", "tok", {"task_id": task_id})
        assert final["status"] == "succeeded"
        assert "5 passed" in final["output"]


def test_task_result_unknown_id(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with pytest.raises(MaintenanceOperationError, match="not found"):
        svc.execute("test_task_result", "tok", {"task_id": "no-such-id"})


def test_run_tests_task_times_out(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    # Use a very short timeout to force a timeout.
    svc = _make_service()

    def always_timeout(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired(args[0], kwargs.get("timeout", 0))

    with patch("modules.maintenance_service.subprocess.run", side_effect=always_timeout):
        result = svc.execute("run_tests", "tok")
        task_id = result["task_id"]

    assert svc.wait_for_task(task_id)
    final = svc.execute("test_task_result", "tok", {"task_id": task_id})
    assert final["status"] == "timed_out"
    assert "timed out" in final["error"]


# ── Unit tests: redaction ─────────────────────────────────────────────────────

def test_redact_api_key_in_output():
    text = "config AZURE_OPENAI_API_KEY=sk-supersecretvalue1234"
    result = _redact(text)
    assert "sk-supersecretvalue1234" not in result


def test_redact_bearer_token():
    # The token value after "Bearer " must be redacted.
    token = "supersecrettoken123abc"
    text = "curl -H 'Authorization: Bearer " + token + "' https://example.com"
    result = _redact(text)
    assert token not in result


def test_redact_preserves_non_sensitive_text():
    text = "git log --oneline: abc1234 Add feature"
    assert _redact(text) == text


# ── Unit tests: audit log ─────────────────────────────────────────────────────

def test_audit_log_does_not_include_token_values(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="(clean)", stderr="")
        svc.execute("git_status", "tok", {"service": "llama-server", "auth_token": "mysecret"})
    entry = svc.audit_log[0]
    for v in entry.args.values():
        assert "mysecret" not in str(v)


# ── API endpoint integration tests ───────────────────────────────────────────

_MAINT_API_KEY = "test-api-key"
_MAINT_TOKEN = "maint-secret"


@pytest.fixture()
def api_client(monkeypatch):
    """Return a TestClient for api.py with env vars configured."""
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    monkeypatch.setenv("MAINTENANCE_TOKEN", _MAINT_TOKEN)
    monkeypatch.delenv("MAINTENANCE_EXPIRY", raising=False)

    from fastapi.testclient import TestClient
    import api as api_module
    from modules.maintenance_service import MaintenanceService as MS

    # api.API_KEY is set at module import time; override it so our test key works.
    monkeypatch.setattr(api_module, "API_KEY", _MAINT_API_KEY)

    svc = MS(
        repo_root="/tmp",
        test_cmd=["echo", "done"],
        agent_cmd=["echo", "agent"],
    )
    api_module.app.dependency_overrides[api_module.get_maintenance_service] = lambda: svc

    client = TestClient(api_module.app)
    yield client
    api_module.app.dependency_overrides.pop(api_module.get_maintenance_service, None)


def test_maintenance_status_enabled(api_client, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_ENABLED", "true")
    resp = api_client.get(
        "/maintenance/status",
        headers={"X-Api-Key": _MAINT_API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


def test_maintenance_status_requires_api_key(api_client):
    resp = api_client.get("/maintenance/status")
    assert resp.status_code == 401


def test_maintenance_execute_git_status(api_client):
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = api_client.post(
            "/maintenance/execute",
            json={"operation": "git_status", "args": {}},
            headers={
                "X-Api-Key": _MAINT_API_KEY,
                "X-Maintenance-Token": _MAINT_TOKEN,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["operation"] == "git_status"
    assert "(clean)" in data["result"]["output"]


def test_maintenance_execute_returns_403_when_disabled(api_client, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_ENABLED", "false")
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "git_status", "args": {}},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": _MAINT_TOKEN,
        },
    )
    assert resp.status_code == 403


def test_maintenance_execute_returns_403_on_bad_token(api_client):
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "git_status", "args": {}},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": "WRONG",
        },
    )
    assert resp.status_code == 403


def test_maintenance_execute_requires_api_key(api_client):
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "git_status"},
        headers={"X-Maintenance-Token": _MAINT_TOKEN},
    )
    assert resp.status_code == 401


def test_maintenance_execute_unknown_operation_returns_400(api_client):
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "rm_rf_slash"},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": _MAINT_TOKEN,
        },
    )
    assert resp.status_code == 400


def test_maintenance_execute_missing_service_name_returns_400(api_client):
    # Service names are deliberately *not* restricted to an allowlist: Nexus
    # has to be able to operate its own units (nexus-tick, nexus-mailbox, ...)
    # as they are added, and a hardcoded pair would block that. Only an absent
    # name is refused. This test previously asserted that "sshd" returned 400
    # while two unit tests in this same file asserted arbitrary names were
    # forwarded - a contradiction that survived because CI collected zero
    # tests from this file.
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "service_status", "args": {}},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": _MAINT_TOKEN,
        },
    )
    assert resp.status_code == 400


def test_maintenance_audit_endpoint(api_client):
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        api_client.post(
            "/maintenance/execute",
            json={"operation": "git_status"},
            headers={
                "X-Api-Key": _MAINT_API_KEY,
                "X-Maintenance-Token": _MAINT_TOKEN,
            },
        )
    resp = api_client.get(
        "/maintenance/audit",
        headers={"X-Api-Key": _MAINT_API_KEY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["entries"][0]["operation"] == "git_status"


def test_maintenance_audit_requires_api_key(api_client):
    resp = api_client.get("/maintenance/audit")
    assert resp.status_code == 401


def test_maintenance_execute_expiry_blocks_after_expiry(api_client, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_EXPIRY", "2000-01-01T00:00:00Z")
    resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "git_status"},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": _MAINT_TOKEN,
        },
    )
    assert resp.status_code == 403


def test_maintenance_run_tests_background_task(api_client):
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="3 passed", stderr="")
        resp = api_client.post(
            "/maintenance/execute",
            json={"operation": "run_tests", "args": {}},
            headers={
                "X-Api-Key": _MAINT_API_KEY,
                "X-Maintenance-Token": _MAINT_TOKEN,
            },
        )
    assert resp.status_code == 200
    task_id = resp.json()["result"]["task_id"]
    assert task_id

    # Poll status
    time.sleep(0.2)
    status_resp = api_client.post(
        "/maintenance/execute",
        json={"operation": "test_task_status", "args": {"task_id": task_id}},
        headers={
            "X-Api-Key": _MAINT_API_KEY,
            "X-Maintenance-Token": _MAINT_TOKEN,
        },
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["result"]["status"] in ("running", "succeeded", "failed")
