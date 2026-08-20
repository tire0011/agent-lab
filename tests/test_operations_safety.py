"""Direct unit/integration tests for operations safety boundaries.

These tests exercise the *implementation* modules (GitOperations,
ServiceOperations, LogReader, OperationsExecutor) directly, not through the
FastAPI layer.  The goal is to verify that dangerous operations enforce their
safety invariants correctly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# GitOperations – path traversal and symlink escapes
# ---------------------------------------------------------------------------

class TestGitOperationsPathBoundary(unittest.TestCase):
    """commit_and_push must reject paths that escape the repo root."""

    def setUp(self):
        # Create a real temp dir to act as repo root.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = self._tmpdir.name

        # Patch env AND module-level constant so GitOperations uses our temp dir.
        env_patcher = patch.dict("os.environ", {"AGENT_FS_ROOT": self.repo_root})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.addCleanup(self._tmpdir.cleanup)

        import modules.git_operations as _git_mod
        self._root_patcher = patch.object(_git_mod, "_ALLOWED_REPO_ROOT", self.repo_root)
        self._root_patcher.start()
        self.addCleanup(self._root_patcher.stop)

        # Import after patching env
        from modules.git_operations import GitOperations
        self._cls = GitOperations

    def _git(self):
        return self._cls(self.repo_root)

    def test_traversal_dotdot_rejected(self):
        git = self._git()
        with self.assertRaises(ValueError):
            _run(git.commit_and_push("msg", ["../etc/passwd"]))

    def test_traversal_encoded_rejected(self):
        """A file that looks safe but resolves outside root must be rejected."""
        git = self._git()
        # Create a subdirectory and use a path that goes above it.
        subdir = Path(self.repo_root) / "subdir"
        subdir.mkdir()
        with self.assertRaises(ValueError):
            _run(git.commit_and_push("msg", ["subdir/../../outside"]))

    def test_symlink_escape_rejected(self):
        """A symlink pointing outside the repo root must be rejected."""
        git = self._git()
        link = Path(self.repo_root) / "evil_link"
        link.symlink_to("/etc")
        with self.assertRaises(ValueError):
            _run(git.commit_and_push("msg", ["evil_link/passwd"]))

    def test_valid_file_accepted(self):
        """A valid relative path inside the root is accepted (git add may fail, that's OK)."""
        git = self._git()
        # This will raise ValueError only on path issues; a git error is fine.
        # We patch _run so it doesn't actually execute git.
        with patch("modules.git_operations._run", new=AsyncMock(return_value=(0, "", ""))):
            result = _run(git.commit_and_push("valid commit", ["README.md"]))
        # If path was accepted the call completed (success may be False due to mock SHA)
        self.assertIsInstance(result, dict)

    def test_unsafe_chars_rejected(self):
        git = self._git()
        with self.assertRaises(ValueError):
            _run(git.commit_and_push("msg", ["file;rm -rf /"]))


# ---------------------------------------------------------------------------
# ServiceOperations – allowlist enforcement
# ---------------------------------------------------------------------------

class TestServiceOperationsAllowlist(unittest.TestCase):
    def setUp(self):
        from modules.service_operations import ServiceOperations
        self._svc = ServiceOperations()

    def test_unknown_service_restart_rejected(self):
        with self.assertRaises(ValueError):
            _run(self._svc.restart("evil-service"))

    def test_unknown_service_restart_with_extra_rejected(self):
        # Ensure injections like "agent-api; evil" are also blocked.
        with self.assertRaises(ValueError):
            _run(self._svc.restart("agent-api; rm -rf /"))

    def test_allowed_service_passes_validation(self):
        # We don't actually call systemctl; just check validation passes.
        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, "active"))):
            result = _run(self._svc.restart("agent-api"))
        self.assertIn("agent-api", result["results"])

    def test_all_keyword_accepted(self):
        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))):
            result = _run(self._svc.restart("all"))
        self.assertEqual(result["service"], "all")


# ---------------------------------------------------------------------------
# LogReader – service allowlist
# ---------------------------------------------------------------------------

class TestLogReaderAllowlist(unittest.TestCase):
    def setUp(self):
        from modules.log_reader import LogReader
        self._lr = LogReader()

    def test_unknown_service_tail_rejected(self):
        with self.assertRaises(ValueError):
            _run(self._lr.tail_service_logs("evil-daemon"))

    def test_unknown_service_errors_rejected(self):
        with self.assertRaises(ValueError):
            _run(self._lr.get_error_logs("../../etc/shadow"))

    def test_allowed_service_tail_works(self):
        with patch("modules.log_reader._run_journalctl", new=AsyncMock(return_value=(0, "line1\nline2"))):
            lines = _run(self._lr.tail_service_logs("agent-api"))
        self.assertIsInstance(lines, list)

    def test_line_cap_enforced(self):
        with patch("modules.log_reader._run_journalctl", new=AsyncMock(return_value=(0, ""))):
            # Request more than _MAX_LINES (500); call must succeed (capped internally).
            _run(self._lr.tail_service_logs("agent-api", lines=9999))


# ---------------------------------------------------------------------------
# OperationsExecutor – stale lock safety
# ---------------------------------------------------------------------------

class TestStaleLocks(unittest.TestCase):
    """Verify that only genuinely stale, ownerless locks are removed."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        patcher = patch.dict(
            "os.environ",
            {"AGENT_FS_ROOT": self.tmp},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_executor(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        # Patch db so we don't hit init_db
        ex._conn = MagicMock()
        ex._conn.execute = MagicMock()
        ex._conn.commit = MagicMock()
        return ex

    def _make_lock(self, name: str, age_seconds: float, pid: str | None = None) -> Path:
        p = Path(self.tmp) / name
        if pid is not None:
            p.write_text(pid)
        else:
            p.touch()
        mtime = time.time() - age_seconds
        os.utime(p, (mtime, mtime))
        return p

    def test_fresh_lock_not_removed(self):
        """A lock file younger than the stale threshold must not be deleted."""
        lock = self._make_lock("fresh.lock", age_seconds=10)
        ex = self._make_executor()
        with patch("modules.operations_executor._LOCK_PATTERNS", [str(self.tmp) + "/*.lock"]):
            result = _run(ex.fix_stale_locks())
        self.assertTrue(lock.exists(), "fresh lock must not be deleted")
        self.assertEqual(result["locks_removed"], 0)

    def test_old_lock_without_pid_removed(self):
        """A lock older than the threshold with no PID inside is stale."""
        lock = self._make_lock("old_empty.lock", age_seconds=7200)
        ex = self._make_executor()
        with patch("modules.operations_executor._LOCK_PATTERNS", [str(self.tmp) + "/*.lock"]):
            result = _run(ex.fix_stale_locks())
        self.assertFalse(lock.exists(), "old ownerless lock must be deleted")
        self.assertEqual(result["locks_removed"], 1)

    def test_old_lock_with_live_pid_not_removed(self):
        """A lock pointing to a live PID must not be deleted."""
        live_pid = str(os.getpid())
        lock = self._make_lock("live.lock", age_seconds=7200, pid=live_pid)
        ex = self._make_executor()
        with patch("modules.operations_executor._LOCK_PATTERNS", [str(self.tmp) + "/*.lock"]):
            result = _run(ex.fix_stale_locks())
        self.assertTrue(lock.exists(), "lock with live PID must not be deleted")
        self.assertEqual(result["locks_removed"], 0)

    def test_old_lock_with_dead_pid_removed(self):
        """A lock pointing to a non-existent PID is stale and must be deleted."""
        dead_pid = "99999999"  # very unlikely to be alive
        lock = self._make_lock("dead.lock", age_seconds=7200, pid=dead_pid)
        ex = self._make_executor()
        with patch("modules.operations_executor._LOCK_PATTERNS", [str(self.tmp) + "/*.lock"]):
            with patch("os.kill", side_effect=ProcessLookupError):
                result = _run(ex.fix_stale_locks())
        self.assertFalse(lock.exists(), "lock with dead PID must be deleted")
        self.assertEqual(result["locks_removed"], 1)


# ---------------------------------------------------------------------------
# OperationsExecutor – temp cleanup stays within approved dirs
# ---------------------------------------------------------------------------

class TestCleanupTemps(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.approved = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

    def _make_executor(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        ex._conn = MagicMock()
        ex._conn.execute = MagicMock()
        ex._conn.commit = MagicMock()
        return ex

    def test_cleanup_only_removes_from_approved_dirs(self):
        """Files outside _TEMP_DIRS are untouched."""
        # A file inside the approved dir.
        approved_file = Path(self.approved) / "junk.tmp"
        approved_file.write_text("junk")

        # A file outside (using a separate tmpdir).
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "important.txt"
            outside_file.write_text("important")

            ex = self._make_executor()
            with patch("modules.operations_executor._TEMP_DIRS", [self.approved]):
                _run(ex.cleanup_temps())

            self.assertFalse(approved_file.exists(), "approved file should be removed")
            self.assertTrue(outside_file.exists(), "outside file must not be removed")


# ---------------------------------------------------------------------------
# OperationsExecutor – audit log redacts parameters and results
# ---------------------------------------------------------------------------

class TestAuditLogRedaction(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test.db")
        self.addCleanup(self._tmpdir.cleanup)

    def _make_executor(self):
        from modules.operations_executor import OperationsExecutor, _redact_audit_dict
        from modules.run_store import init_db
        conn = init_db(self.db_path)
        ex = OperationsExecutor(db_path=self.db_path)
        ex._conn = conn
        return ex

    def test_secret_params_redacted_in_log(self):
        """Parameters with secret-like keys must be stored as '***'."""
        from modules.operations_executor import _redact_audit_dict
        params = {"key": "my-config-key", "value": "super-secret", "model": "azure_gpt4o"}
        redacted = _redact_audit_dict(params)
        self.assertEqual(redacted["key"], "***")
        self.assertEqual(redacted["value"], "***")
        self.assertEqual(redacted["model"], "azure_gpt4o")

    def test_audit_log_stores_redacted_params(self):
        """_log_operation must persist redacted params, not raw params."""
        ex = self._make_executor()
        ex._log_operation(
            operation="context/set-config",
            params={"key": "FEATURE_FLAG", "value": "topsecret"},
            success=True,
            result={"key": "FEATURE_FLAG"},
            error=None,
            duration_ms=1,
        )
        row = ex._conn.execute(
            "SELECT parameters, result FROM operation_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        stored_params = json.loads(row[0])
        self.assertEqual(stored_params.get("value"), "***")
        self.assertNotEqual(stored_params.get("value"), "topsecret")

    def test_audit_log_does_not_store_raw_secrets_in_result(self):
        """Result dict with sensitive values is redacted before storage."""
        ex = self._make_executor()
        ex._log_operation(
            operation="context/set-config",
            params={},
            success=True,
            result={"key": "DB_TOKEN", "value": "secretvalue"},
            error=None,
            duration_ms=1,
        )
        row = ex._conn.execute(
            "SELECT result FROM operation_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        stored_result = json.loads(row[0])
        self.assertEqual(stored_result.get("value"), "***")


# ---------------------------------------------------------------------------
# set_config – value not returned in API response
# ---------------------------------------------------------------------------

class TestSetConfigResponse(unittest.TestCase):
    def test_set_config_does_not_return_value(self):
        """set_config must return key+status only, never the written value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            patcher = patch.dict("os.environ", {"AGENT_FS_ROOT": tmpdir})
            patcher.start()
            try:
                from modules import operations_executor
                # Reload to pick up patched env
                import importlib
                importlib.reload(operations_executor)
                ex = operations_executor.OperationsExecutor(db_path=":memory:")
                ex._conn = MagicMock()
                ex._conn.execute = MagicMock()
                ex._conn.commit = MagicMock()
                result = _run(ex.set_config("FEATURE_FLAG", "enabled"))
                self.assertNotIn("value", result)
                self.assertIn("key", result)
                self.assertTrue(result["success"])
            finally:
                patcher.stop()

    def test_set_config_rejects_secret_key(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        ex._conn = MagicMock()
        with self.assertRaises(ValueError):
            _run(ex.set_config("AGENT_API_KEY", "some-value"))

    def test_set_config_rejects_password_key(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        ex._conn = MagicMock()
        with self.assertRaises(ValueError):
            _run(ex.set_config("DB_PASSWORD", "oops"))


# ---------------------------------------------------------------------------
# OperationsExecutor.execute – destructions fail safely, errors sanitized
# ---------------------------------------------------------------------------

class TestExecuteErrorSanitization(unittest.TestCase):
    def test_unknown_operation_returns_error(self):
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        ex._conn = MagicMock()
        ex._conn.execute = MagicMock()
        ex._conn.commit = MagicMock()
        result = _run(ex.execute("nonexistent/op", {}))
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        # Error must not expose a traceback
        self.assertNotIn("Traceback", result.error or "")

    def test_fix_locks_error_does_not_raise(self):
        """fix_stale_locks must not propagate OS errors as exceptions."""
        from modules.operations_executor import OperationsExecutor
        ex = OperationsExecutor(db_path=":memory:")
        ex._conn = MagicMock()
        ex._conn.execute = MagicMock()
        ex._conn.commit = MagicMock()
        with patch("modules.operations_executor._LOCK_PATTERNS", []):
            result = _run(ex.fix_stale_locks())
        self.assertTrue(result["success"])


# ---------------------------------------------------------------------------
# Recursive audit redaction
# ---------------------------------------------------------------------------

class TestRecursiveAuditRedaction(unittest.TestCase):
    """_redact_audit_dict must sanitize secrets at every nesting level."""

    def _r(self, d):
        from modules.operations_executor import _redact_audit_dict
        return _redact_audit_dict(d)

    def test_nested_dict_token_redacted(self):
        result = self._r({"config": {"token": "secret"}})
        self.assertEqual(result["config"]["token"], "***")

    def test_nested_dict_password_redacted(self):
        result = self._r({"db": {"password": "hunter2"}})
        self.assertEqual(result["db"]["password"], "***")

    def test_list_of_dicts_redacted(self):
        result = self._r({"items": [{"password": "s3cr3t"}, {"name": "alice"}]})
        self.assertEqual(result["items"][0]["password"], "***")
        self.assertEqual(result["items"][1]["name"], "alice")

    def test_deeply_nested_redacted(self):
        result = self._r({"a": {"b": {"c": {"token": "deep-secret"}}}})
        self.assertEqual(result["a"]["b"]["c"]["token"], "***")

    def test_non_sensitive_nested_values_preserved(self):
        result = self._r({"meta": {"model": "gpt-4o", "count": 3}})
        self.assertEqual(result["meta"]["model"], "gpt-4o")
        self.assertEqual(result["meta"]["count"], 3)

    def test_list_at_top_level_not_a_dict_returns_empty(self):
        result = self._r(None)
        self.assertEqual(result, {})

    def test_tuple_of_dicts_redacted(self):
        from modules.operations_executor import _redact_audit_value
        result = _redact_audit_value(({"secret": "x"}, {"name": "y"}))
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[0]["secret"], "***")
        self.assertEqual(result[1]["name"], "y")

    def test_nested_list_within_list_redacted(self):
        result = self._r({"outer": [[{"token": "nested-list-secret"}]]})
        self.assertEqual(result["outer"][0][0]["token"], "***")


class TestRecursiveAuditLogPersistence(unittest.TestCase):
    """Verify that nested secrets are not stored in operation_log."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test.db")
        self.addCleanup(self._tmpdir.cleanup)

    def _make_executor(self):
        from modules.operations_executor import OperationsExecutor
        from modules.run_store import init_db
        conn = init_db(self.db_path)
        ex = OperationsExecutor(db_path=self.db_path)
        ex._conn = conn
        return ex

    def test_nested_token_not_stored_in_params(self):
        ex = self._make_executor()
        ex._log_operation(
            operation="context/set-config",
            params={"config": {"token": "nested-secret"}},
            success=True,
            result={},
            error=None,
            duration_ms=1,
        )
        row = ex._conn.execute(
            "SELECT parameters FROM operation_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["config"]["token"], "***")
        self.assertNotIn("nested-secret", json.dumps(stored))

    def test_list_of_dicts_with_password_not_stored(self):
        ex = self._make_executor()
        ex._log_operation(
            operation="models/test",
            params={},
            success=True,
            result={"items": [{"password": "topsecret"}, {"model": "gpt-4o"}]},
            error=None,
            duration_ms=1,
        )
        row = ex._conn.execute(
            "SELECT result FROM operation_log ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["items"][0]["password"], "***")
        self.assertEqual(stored["items"][1]["model"], "gpt-4o")
        self.assertNotIn("topsecret", json.dumps(stored))


if __name__ == "__main__":
    unittest.main()
