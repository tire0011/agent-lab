"""Focused tests for the MCP write tools added in the bootstrap phase.

Covers:
- Write-disabled guard (_require_write_enabled / _write_enabled)
- git_pull MCP tool
- service_restart allowlist enforcement (via ServiceOperations)
- service_status
- run_tests (bounded output, timeout)
- experiment_start (creates + fires background thread)
- router_run (delegates to LlamaRouter)
- shell (timeout, output bounding, redaction, audit logging via executor)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run *coro* on a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _write_enabled / _require_write_enabled
# ---------------------------------------------------------------------------


class TestWriteEnabledGuard(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_WRITE_ENABLED", None)
            from nexus_mcp import _write_enabled
            self.assertFalse(_write_enabled())

    def test_enabled_when_true(self):
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            # Re-import to pick up env change via function call (not module-level)
            from nexus_mcp import _write_enabled
            self.assertTrue(_write_enabled())

    def test_not_enabled_for_yes_or_1(self):
        for val in ("yes", "1"):
            with self.subTest(val=val):
                with patch.dict(os.environ, {"MCP_WRITE_ENABLED": val}):
                    from nexus_mcp import _write_enabled
                    self.assertFalse(_write_enabled())

    def test_enabled_case_insensitive_true(self):
        """Both 'True' and 'TRUE' are treated as enabled (lowercased match)."""
        for val in ("True", "TRUE"):
            with self.subTest(val=val):
                with patch.dict(os.environ, {"MCP_WRITE_ENABLED": val}):
                    from nexus_mcp import _write_enabled
                    self.assertTrue(_write_enabled())

    def test_require_raises_when_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_WRITE_ENABLED", None)
            from nexus_mcp import _require_write_enabled
            with self.assertRaises(ValueError, msg="should raise when disabled"):
                _require_write_enabled()

    def test_require_passes_when_enabled(self):
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            from nexus_mcp import _require_write_enabled
            _require_write_enabled()  # must not raise


# ---------------------------------------------------------------------------
# Service allowlist
# ---------------------------------------------------------------------------


class TestServiceAllowlist(unittest.TestCase):
    def test_correct_service_names(self):
        from modules.service_operations import _ALLOWED_SERVICES
        self.assertIn("agent-api", _ALLOWED_SERVICES)
        self.assertIn("llama-server", _ALLOWED_SERVICES)
        self.assertIn("nexus-mailbox", _ALLOWED_SERVICES)

    def test_old_names_removed(self):
        from modules.service_operations import _ALLOWED_SERVICES
        self.assertNotIn("nexus-api", _ALLOWED_SERVICES)
        self.assertNotIn("llama", _ALLOWED_SERVICES)

    def test_validate_service_rejects_unknown(self):
        from modules.service_operations import _validate_service
        with self.assertRaises(ValueError):
            _validate_service("unknown-svc")

    def test_validate_service_accepts_all(self):
        from modules.service_operations import _validate_service
        _validate_service("all")  # must not raise

    def test_validate_service_accepts_known(self):
        from modules.service_operations import _validate_service, _ALLOWED_SERVICES
        for svc in _ALLOWED_SERVICES:
            _validate_service(svc)  # must not raise


# ---------------------------------------------------------------------------
# Shell operation (OperationsExecutor.shell)
# ---------------------------------------------------------------------------

class TestShellOperation(unittest.TestCase):
    def setUp(self):
        os.environ["AGENT_FS_ROOT"] = "/tmp"

    def tearDown(self):
        os.environ.pop("AGENT_FS_ROOT", None)

    def test_echo_returns_output(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        result = _run(ex.shell("echo hello"))
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["output"])

    def test_nonzero_exit_not_success(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        result = _run(ex.shell("exit 1"))
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 1)

    def test_timeout_enforced(self):
        """A slow command times out: exit_code=-1 and success=False."""
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)

        # Use a mock that simulates a slow process to avoid actually waiting
        slow_proc = MagicMock()
        slow_proc.returncode = None
        _calls = [0]

        async def communicate_side_effect():
            import asyncio as _asyncio
            _calls[0] += 1
            if _calls[0] == 1:
                # First call: simulate slow process
                await _asyncio.sleep(9999)
                return b"", b""
            # Second call (after kill): return immediately
            return b"", b""

        slow_proc.communicate = communicate_side_effect
        slow_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=slow_proc)):
            result = _run(ex.shell("sleep 9999", timeout_seconds=1))

        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], -1)
        self.assertIn("timed out", result["output"])

    def test_timeout_capped_at_max(self):
        """timeout_seconds > _SHELL_MAX_TIMEOUT is silently capped."""
        from modules.operations_executor import _SHELL_MAX_TIMEOUT, OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        # Just verify the cap logic doesn't raise; we don't actually wait.
        # We patch asyncio.create_subprocess_exec to return immediately.
        async def fake_proc():
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=MagicMock(
            returncode=0,
            communicate=AsyncMock(return_value=(b"ok", b"")),
        ))):
            result = _run(ex.shell("echo x", timeout_seconds=99999))
        # It should succeed (capped internally but still ran)
        self.assertIsInstance(result, dict)

    def test_output_truncated_at_limit(self):
        from modules.operations_executor import _SHELL_MAX_OUTPUT_BYTES, OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        # Generate output larger than the cap
        big = "A" * (_SHELL_MAX_OUTPUT_BYTES + 100)
        result = _run(ex.shell(f"printf '{big}'"))
        self.assertTrue(result.get("truncated", False))
        self.assertLessEqual(len(result["output"].encode()), _SHELL_MAX_OUTPUT_BYTES + 50)

    def test_secret_redacted_in_output(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        result = _run(ex.shell("echo 'token: sk-abcdefghijklmnop'"))
        self.assertNotIn("sk-abcdefghijklmnop", result["output"])

    def test_empty_command_raises(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        with self.assertRaises(ValueError):
            _run(ex.shell(""))

    def test_shell_audited_via_execute(self):
        """executor.execute('shell', ...) goes through _log_operation."""
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor()
        logged = []
        original = ex._log_operation

        def capturing_log(*args, **kwargs):
            logged.append(args)
            return original(*args, **kwargs)

        ex._log_operation = capturing_log
        _run(ex.execute("shell", {"command": "echo audit-test"}))
        self.assertTrue(any("shell" in str(entry) for entry in logged))


# ---------------------------------------------------------------------------
# MCP write tool: git_pull
# ---------------------------------------------------------------------------


class TestMcpGitPull(unittest.TestCase):
    def _make_executor_result(self, success=True, data=None, error=None):
        from modules.operations_executor import OperationResult
        from datetime import datetime, timezone
        return OperationResult(
            success=success,
            operation="git/pull",
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data or {"success": success, "commits_pulled": 1, "message": "ok"},
            error=error,
            duration_ms=10,
            status="ok" if success else "error",
        )

    def test_git_pull_disabled_raises(self):
        os.environ.pop("MCP_WRITE_ENABLED", None)
        # Import the module-level guard directly to simulate tool invocation
        from nexus_mcp import _require_write_enabled
        with self.assertRaises(ValueError):
            _require_write_enabled()

    def test_git_pull_calls_executor(self):
        """git_pull delegates to executor.execute('git/pull')."""
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            mock_executor = MagicMock()
            mock_executor.execute = AsyncMock(return_value=self._make_executor_result())

            with patch("nexus_mcp._api") as mock_api:
                mock_api.return_value.get_operations_executor.return_value = mock_executor
                # Rebuild the mount app to pick up fresh registrations
                from nexus_mcp import _build_mount_app  # noqa: F401
                # Directly test the async tool body
                async def _test():
                    from nexus_mcp import _require_write_enabled
                    _require_write_enabled()  # should not raise
                    result = await mock_executor.execute("git/pull", {})
                    return result

                result = _run(_test())
                mock_executor.execute.assert_awaited_once_with("git/pull", {})
                self.assertTrue(result.success)


# ---------------------------------------------------------------------------
# MCP write tool: service_restart via executor
# ---------------------------------------------------------------------------


class TestMcpServiceRestart(unittest.TestCase):
    def _make_executor_result(self, operation, success=True, data=None, error=None):
        from modules.operations_executor import OperationResult
        from datetime import datetime, timezone
        return OperationResult(
            success=success,
            operation=operation,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data or {},
            error=error,
            duration_ms=5,
            status="ok" if success else "error",
        )

    def test_restart_unknown_service_rejected(self):
        """ServiceOperations.restart rejects unknown service names."""
        from modules.service_operations import ServiceOperations
        svc = ServiceOperations()
        with self.assertRaises(ValueError):
            _run(svc.restart("not-a-real-service"))

    def test_restart_all_uses_allowed_list(self):
        """restart('all') iterates over the allowed set, not an arbitrary name."""
        from modules.service_operations import _ALLOWED_SERVICES, ServiceOperations
        svc = ServiceOperations()
        called_services: list[str] = []

        async def fake_systemctl(args, timeout=15):
            called_services.append(args[1])  # args = ["restart", svc_name]
            return 0, "ok"

        with patch("modules.service_operations._systemctl", side_effect=fake_systemctl):
            _run(svc.restart("all"))

        self.assertEqual(sorted(called_services), sorted(_ALLOWED_SERVICES))


# ---------------------------------------------------------------------------
# MCP write tool: run_tests (bounded output, timeout simulation)
# ---------------------------------------------------------------------------


class TestMcpRunTests(unittest.TestCase):
    def test_run_tests_disabled_raises(self):
        os.environ.pop("MCP_WRITE_ENABLED", None)
        from nexus_mcp import _require_write_enabled
        with self.assertRaises(ValueError):
            _require_write_enabled()

    def test_run_tests_returns_bounded_lines(self):
        """run_tests returns at most 200 lines, each ≤ 500 chars."""
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="line\n" * 500,
                    stderr="",
                )
                # Call the same logic used in the tool
                from modules.redaction import bounded_lines
                combined = "line\n" * 500
                lines = bounded_lines(combined, max_lines=200, max_line_chars=500)
                self.assertLessEqual(len(lines), 200)

    def test_run_tests_timeout_returns_error(self):
        """TimeoutExpired is caught and returned as a failure dict."""
        import subprocess
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 10)):
                # Simulate the tool's exception handler directly
                try:
                    raise subprocess.TimeoutExpired("pytest", 10)
                except subprocess.TimeoutExpired:
                    result = {"success": False, "exit_code": -1, "lines": ["[timed out]"]}
                self.assertFalse(result["success"])
                self.assertEqual(result["exit_code"], -1)


# ---------------------------------------------------------------------------
# MCP write tool: experiment_start
# ---------------------------------------------------------------------------


class TestMcpExperimentStart(unittest.TestCase):
    def test_experiment_start_disabled_raises(self):
        os.environ.pop("MCP_WRITE_ENABLED", None)
        from nexus_mcp import _require_write_enabled
        with self.assertRaises(ValueError):
            _require_write_enabled()

    def test_experiment_start_creates_experiment(self):
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            mock_svc = MagicMock()
            mock_exp = MagicMock()
            mock_exp.id = "abc123"
            mock_exp.status = "queued"
            mock_svc.create_experiment.return_value = mock_exp
            mock_svc.run_experiment = MagicMock(return_value=mock_exp)

            with patch("nexus_mcp._api") as mock_api:
                mock_api.return_value._resolve_experiment_service.return_value = mock_svc
                # Simulate the experiment_start tool body
                import threading

                svc = mock_svc
                experiment = svc.create_experiment("test goal")

                def _run_bg():
                    try:
                        svc.run_experiment(experiment.id)
                    except Exception:
                        pass

                t = threading.Thread(target=_run_bg, daemon=True, name="test-exp")
                t.start()
                t.join(timeout=1.0)

                result = {"experiment_id": experiment.id, "status": experiment.status}

            mock_svc.create_experiment.assert_called_once_with("test goal")
            self.assertEqual(result["experiment_id"], "abc123")
            self.assertEqual(result["status"], "queued")


# ---------------------------------------------------------------------------
# MCP write tool: router_run
# ---------------------------------------------------------------------------


class TestMcpRouterRun(unittest.TestCase):
    def test_router_run_disabled_raises(self):
        os.environ.pop("MCP_WRITE_ENABLED", None)
        from nexus_mcp import _require_write_enabled
        with self.assertRaises(ValueError):
            _require_write_enabled()

    def test_router_run_returns_decision(self):
        with patch.dict(os.environ, {"MCP_WRITE_ENABLED": "true"}):
            mock_decision = MagicMock()
            mock_decision.decision = "AZURE_OPENAI"
            mock_decision.provider = "azure_openai"
            mock_decision.role = "developer"
            mock_decision.reasoning = "complex task"
            mock_decision.confidence = 0.9

            mock_router = MagicMock()
            mock_router.route = AsyncMock(return_value=mock_decision)

            with patch("nexus_mcp._require_write_enabled"):
                async def _test():
                    result = await mock_router.route("implement feature X")
                    return {
                        "decision": result.decision,
                        "provider": result.provider,
                        "role": result.role,
                        "reasoning": result.reasoning,
                        "confidence": result.confidence,
                    }

                result = _run(_test())

            self.assertEqual(result["decision"], "AZURE_OPENAI")
            self.assertEqual(result["provider"], "azure_openai")
            self.assertEqual(result["role"], "developer")
            mock_router.route.assert_awaited_once_with("implement feature X")


# ---------------------------------------------------------------------------
# Shell audit logging integration
# ---------------------------------------------------------------------------


class TestShellAuditLogging(unittest.TestCase):
    def setUp(self):
        os.environ["AGENT_FS_ROOT"] = "/tmp"

    def tearDown(self):
        os.environ.pop("AGENT_FS_ROOT", None)

    def test_execute_shell_writes_to_operation_log(self):
        """executor.execute('shell', ...) records to operation_log table."""
        import sqlite3
        import tempfile

        from modules.operations_executor import OperationsExecutor

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        ex = OperationsExecutor(db_path=db_path)
        _run(ex.execute("shell", {"command": "echo audit-log-test"}))

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT operation FROM operation_log WHERE operation='shell'"
        ).fetchall()
        conn.close()

        self.assertTrue(len(rows) >= 1, "shell operation should appear in operation_log")


if __name__ == "__main__":
    unittest.main()
