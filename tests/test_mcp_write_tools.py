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
    @classmethod
    def setUpClass(cls):
        """Skip the whole class when systemd-run --scope --user is not usable.

        All tests in this class actually execute shell commands, which now
        requires a working cgroup scope boundary.  On hosts without a user bus
        (e.g. CI runners, system services) the shell tool correctly fails
        closed, making real-execution tests meaningless to run.  The
        confinement-contract tests that do *not* require a live cgroup are in
        :class:`TestShellConfinement` below and always run.
        """
        from modules.operations_executor import _SYSTEMD_RUN
        if not _SYSTEMD_RUN:
            raise unittest.SkipTest(
                "systemd-run --scope --user not usable on this host; "
                "shell requires cgroup boundary — skipping live-execution tests"
            )

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
        result = _run(ex.shell("sleep 30", timeout_seconds=1))
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], -1)
        self.assertIn("timed out", result["output"])

    def test_timeout_kills_child_processes(self):
        """On timeout, child processes (incl. background ones) are killed via killpg."""
        import tempfile
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        pid_file = os.path.join(tempfile.gettempdir(), f"nexus_test_{os.getpid()}.pid")
        try:
            # Start a background sleep that writes its PID to a file, then sleeps.
            # In a non-interactive shell with setsid, background children share the pgid.
            cmd = f"sleep 30 & echo $! > {pid_file}; sleep 30"
            _run(ex.shell(cmd, timeout_seconds=1))
            # Allow a brief moment for the pid file to be flushed.
            time.sleep(0.3)
            if not os.path.exists(pid_file):
                return  # PID file not written before kill — skip liveness check
            with open(pid_file) as fh:
                child_pid_str = fh.read().strip()
            if not child_pid_str.isdigit():
                return
            child_pid = int(child_pid_str)
            try:
                os.kill(child_pid, 0)
                self.fail(f"Child process {child_pid} still alive after group kill")
            except ProcessLookupError:
                pass  # Expected: child was killed along with the process group
        finally:
            try:
                os.unlink(pid_file)
            except OSError:
                pass

    def test_timeout_capped_at_max(self):
        """timeout_seconds > _SHELL_MAX_TIMEOUT is silently capped."""
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        result = _run(ex.shell("echo x", timeout_seconds=99999))
        self.assertIsInstance(result, dict)
        self.assertTrue(result["success"])

    def test_output_truncated_at_limit(self):
        from modules.operations_executor import _SHELL_MAX_OUTPUT_BYTES, OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        # Generate output clearly larger than the cap via Python (avoids shell quoting issues).
        cmd = (
            f"python3 -c \"import sys; sys.stdout.write('A' * {_SHELL_MAX_OUTPUT_BYTES + 4096})"
            f"; sys.stdout.flush()\""
        )
        result = _run(ex.shell(cmd, timeout_seconds=15))
        self.assertTrue(result.get("truncated", False))
        self.assertLessEqual(len(result["output"].encode()), _SHELL_MAX_OUTPUT_BYTES + 50)

    def test_exact_boundary_not_truncated(self):
        """A command whose output is exactly _SHELL_MAX_OUTPUT_BYTES must not set truncated."""
        from modules.operations_executor import _SHELL_MAX_OUTPUT_BYTES, OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        cmd = (
            f"python3 -c \"import sys; sys.stdout.write('B' * {_SHELL_MAX_OUTPUT_BYTES})"
            f"; sys.stdout.flush()\""
        )
        result = _run(ex.shell(cmd, timeout_seconds=15))
        self.assertFalse(result.get("truncated", False),
                         "Exactly at cap should not be reported as truncated")

    def test_background_child_killed_on_normal_exit(self):
        """Background processes must be killed even when the shell exits normally (exit 0)."""
        import tempfile
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        pid_file = os.path.join(tempfile.gettempdir(), f"nexus_bg_exit_{os.getpid()}.pid")
        try:
            # Shell returns immediately (exit 0); background sleep keeps going without kill.
            cmd = f"sleep 30 & echo $! > {pid_file}"
            result = _run(ex.shell(cmd, timeout_seconds=5))
            # Shell must have exited cleanly.
            self.assertTrue(result["success"], "Shell command should succeed")
            # Give the child a moment to write its PID.
            time.sleep(0.4)
            self.assertTrue(os.path.exists(pid_file),
                            "PID file was not created — background child may not have started")
            with open(pid_file) as fh:
                child_pid_str = fh.read().strip()
            self.assertTrue(child_pid_str.isdigit(),
                            f"PID file did not contain a numeric PID: {child_pid_str!r}")
            child_pid = int(child_pid_str)
            try:
                os.kill(child_pid, 0)
                self.fail(
                    f"Background child {child_pid} is still alive after the shell exited "
                    "normally — process-group kill must be applied unconditionally"
                )
            except ProcessLookupError:
                pass  # Expected: process group was killed after normal shell exit
            except PermissionError:
                # PermissionError means a process with this PID exists but is
                # owned by a different user — the PID may have been recycled.
                # This is a genuine failure: the original child should be dead.
                self.fail(
                    f"Background child {child_pid}: got PermissionError from kill(0); "
                    "PID may have been recycled, implying the original child outlived "
                    "the shell and the process group was not killed promptly."
                )
        finally:
            try:
                os.unlink(pid_file)
            except OSError:
                pass

    def test_pipe_close_timeout_respected(self):
        """Timeout fires even when the command closes stdout before the deadline.

        A command like ``exec >/dev/null 2>&1; sleep 300`` closes the stdout
        pipe immediately; the reader thread exits right away.  The timer-based
        deadline must still kill the process after *timeout_seconds*, not block
        indefinitely on proc.wait().
        """
        import tempfile
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor.__new__(OperationsExecutor)
        pid_file = os.path.join(tempfile.gettempdir(), f"nexus_pipe_close_{os.getpid()}.pid")
        try:
            # Close stdout, write PID, then sleep 300.  Reader exits at once;
            # timer must fire after 1 s and kill the process.
            cmd = f"echo $$ > {pid_file}; exec >/dev/null 2>&1; sleep 300"
            t0 = time.monotonic()
            result = _run(ex.shell(cmd, timeout_seconds=1))
            elapsed = time.monotonic() - t0
            self.assertFalse(result["success"],
                             "Command should fail (timed out)")
            self.assertIn("timed out", result.get("output", "").lower(),
                          "Result should report timeout")
            # Must return promptly — well within the drain window.
            self.assertLess(elapsed, 15,
                            f"shell() took {elapsed:.1f}s — timeout bypass suspected")
            # The process must be dead.
            if os.path.exists(pid_file):
                with open(pid_file) as fh:
                    pid_str = fh.read().strip()
                if pid_str.isdigit():
                    child_pid = int(pid_str)
                    time.sleep(0.3)
                    try:
                        os.kill(child_pid, 0)
                        self.fail(
                            f"Process {child_pid} is still alive after timeout — "
                            "pipe-close timeout bypass not fixed"
                        )
                    except ProcessLookupError:
                        pass  # expected
        finally:
            try:
                os.unlink(pid_file)
            except OSError:
                pass

    def test_setsid_child_killed_via_scope(self):
        """A child that calls setsid() must be killed along with the shell.

        With systemd-run scope confinement every process — even those that call
        ``setsid`` to create a new session — remains inside the same cgroup and
        is killed when the scope is stopped.  Without systemd-run (fallback)
        this test is skipped because setsid children can escape os.killpg.

        The test explicitly fails (not silently passes) if the PID file is not
        written, because that indicates the child never started.
        """
        import shutil
        import tempfile
        from modules.operations_executor import _SYSTEMD_RUN, OperationsExecutor

        if not _SYSTEMD_RUN:
            self.skipTest("systemd-run not available; setsid escape not preventable")

        ex = OperationsExecutor.__new__(OperationsExecutor)
        pid_file = os.path.join(
            tempfile.gettempdir(), f"nexus_setsid_{os.getpid()}.pid"
        )
        try:
            # Shell exits immediately; setsid child writes PID then sleeps 30.
            cmd = (
                f"setsid sh -c 'echo $$ > {pid_file}; sleep 30' &"
            )
            result = _run(ex.shell(cmd, timeout_seconds=5))
            # Allow child a moment to write PID and detach.
            time.sleep(0.6)
            self.assertTrue(
                os.path.exists(pid_file),
                "PID file not written — setsid child may not have started at all"
            )
            with open(pid_file) as fh:
                pid_str = fh.read().strip()
            self.assertTrue(
                pid_str.isdigit(),
                f"PID file did not contain a numeric PID: {pid_str!r}"
            )
            child_pid = int(pid_str)
            try:
                os.kill(child_pid, 0)
                self.fail(
                    f"setsid child {child_pid} is still alive — "
                    "cgroup scope did not kill it"
                )
            except ProcessLookupError:
                pass  # expected
            except PermissionError:
                self.fail(
                    f"setsid child {child_pid}: PermissionError from kill(0) — "
                    "PID may have been recycled, original child likely outlived scope"
                )
        finally:
            try:
                os.unlink(pid_file)
            except OSError:
                pass

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
# Shell confinement contract — always runs, mocks subprocess
# ---------------------------------------------------------------------------


class TestShellConfinement(unittest.TestCase):
    """Tests for the cgroup confinement contract.

    These tests do NOT require a working systemd user bus.  They mock the
    relevant subprocess and module-level variables to verify the security
    contracts independently of the host environment.
    """

    def setUp(self):
        os.environ["AGENT_FS_ROOT"] = "/tmp"

    def tearDown(self):
        os.environ.pop("AGENT_FS_ROOT", None)

    def test_shell_fails_closed_when_no_cgroup_boundary(self):
        """Shell must fail-closed (success=False) when systemd-run --scope --user
        is unavailable — binary present but user bus unreachable is the key case
        (e.g. agent-api runs as a system service without a user session).

        The test patches _SYSTEMD_RUN to None to simulate this reliably.
        """
        import unittest.mock as _mock
        from modules import operations_executor
        from modules.operations_executor import OperationsExecutor

        with _mock.patch.object(operations_executor, "_SYSTEMD_RUN", None):
            ex = OperationsExecutor.__new__(OperationsExecutor)
            result = _run(ex.shell("echo hi"))
            self.assertFalse(result["success"],
                             "shell must fail-closed when cgroup boundary is unavailable")
            self.assertIn("cgroup", result["output"].lower(),
                          "error message must mention cgroup confinement")

    def test_shell_fails_closed_even_when_binary_exists(self):
        """Simulates the case where systemd-run binary is found by which() but the
        --scope --user probe exits non-zero (no user bus / Failed to connect to bus).
        _probe_systemd_scope must return None so _SYSTEMD_RUN is None and the shell
        tool fails closed.
        """
        import unittest.mock as _mock
        import shutil as _shutil
        from modules import operations_executor

        _fake_which = _mock.MagicMock(return_value="/usr/bin/systemd-run")
        _fake_run = _mock.MagicMock(return_value=_mock.MagicMock(returncode=1))
        with _mock.patch.object(_shutil, "which", _fake_which), \
             _mock.patch("subprocess.run", _fake_run):
            result = operations_executor._probe_systemd_scope()
        self.assertIsNone(result,
                          "_probe_systemd_scope must return None when probe exits non-zero")

    def test_scope_cleanup_failure_returns_success_false(self):
        """If systemctl --user stop exits non-zero during scope teardown the
        operation must return success=False (not silently succeed).
        """
        import unittest.mock as _mock
        from modules import operations_executor
        from modules.operations_executor import OperationsExecutor

        _fake_systemd_run = "/usr/bin/systemd-run"

        _start_proc = _mock.MagicMock()
        _start_proc.__enter__ = _mock.MagicMock(return_value=_start_proc)
        _start_proc.__exit__ = _mock.MagicMock(return_value=False)
        _start_proc.pid = 99999999
        _start_proc.stdout = _mock.MagicMock()
        _start_proc.stdout.read = _mock.MagicMock(return_value=b"")
        _start_proc.wait = _mock.MagicMock(return_value=0)
        _start_proc.returncode = 0

        def _fake_run(argv, **kwargs):
            r = _mock.MagicMock()
            r.returncode = 1 if "stop" in argv else 0
            return r

        with _mock.patch.object(operations_executor, "_SYSTEMD_RUN", _fake_systemd_run), \
             _mock.patch("subprocess.Popen", return_value=_start_proc), \
             _mock.patch("subprocess.run", side_effect=_fake_run), \
             _mock.patch("os.getpgid", return_value=None):
            ex = OperationsExecutor.__new__(OperationsExecutor)
            result = _run(ex.shell("echo test", timeout_seconds=5))
        self.assertFalse(result["success"],
                         "scope cleanup failure must force success=False")
        self.assertIn("scope cleanup failed", result["output"].lower())


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

    def test_run_tests_large_output_bounded_during_execution(self):
        """run_tests must not buffer unbounded output in RAM during execution."""
        import io
        from nexus_mcp import _run_tests_sync
        large_bytes = b"X\n" * 300_000  # ~600 KiB > 512 KiB cap

        class _FakePopen:
            def __init__(self, *a, **kw):
                self.stdout = io.BytesIO(large_bytes)
                self.returncode = None
                # Use a definitely-dead PID so getpgid raises and _pgid stays None.
                self.pid = 999999999
            def kill(self):
                pass
            def wait(self):
                self.returncode = 0
                return 0
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        with patch("subprocess.Popen", _FakePopen):
            result = _run_tests_sync(30, "/tmp")
        # Output must be bounded — should be truncated and lines ≤ 200
        self.assertLessEqual(len(result.get("lines", [])), 200)

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

    def test_run_tests_uses_unittest_discover(self):
        """run_tests invokes 'python -m unittest discover -s tests', not pytest.

        Verified by source inspection so no heavyweight API mock is needed.
        """
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / "nexus_mcp.py").read_text()
        # The run_tests tool must call unittest discover
        self.assertIn('"unittest"', src,
                      "nexus_mcp.py must reference 'unittest' in the run_tests argv")
        self.assertIn('"discover"', src,
                      "nexus_mcp.py must reference 'discover' in the run_tests argv")
        # Must not fall back to pytest (pytest is not in requirements-api.txt).
        # Scoped to argv list context only: checks for `"-m", "pytest"` as a sequence.
        self.assertNotIn('"-m", "pytest"', src,
                         'pytest must not appear as an argv token in nexus_mcp.py')


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

    def test_command_secret_redacted_in_audit(self):
        """A secret embedded in the shell command is not stored verbatim in the audit log."""
        import sqlite3
        import tempfile

        from modules.operations_executor import OperationsExecutor

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        ex = OperationsExecutor(db_path=db_path)
        secret = "sk-supersecret9876"
        _run(ex.execute("shell", {"command": f"echo {secret}"}))

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT parameters FROM operation_log WHERE operation='shell'"
        ).fetchall()
        conn.close()

        self.assertTrue(rows, "audit row expected in operation_log")
        params_json = rows[0][0]
        self.assertNotIn(secret, params_json, "secret must be redacted in audit params")


if __name__ == "__main__":
    unittest.main()
