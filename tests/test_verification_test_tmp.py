"""Verification test runs get their own TMPDIR, and old ones are pruned:
2026-11-22 the VM's /tmp held 30,000 leftover test directories (7.3 GB)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from modules import verification_env
from modules.verification_runner import WatchdogConfig, run_verification_test_monitored
from modules.verification_env import build_verification_test_env, prune_test_tmp, verification_tmp_parent


class TestTmpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(verification_env.tempfile, "gettempdir", return_value=tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_test_run_writes_its_temp_files_below_its_own_run_dir(self):
        env = build_verification_test_env({"PATH": os.environ.get("PATH", ""), "AGENT_DB_PATH": "/prod.db"})
        run_dir = Path(env["TMPDIR"])
        self.assertEqual(run_dir.parent, verification_tmp_parent())
        self.assertTrue(env["AGENT_DB_PATH"].startswith(str(run_dir)))
        out = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.mkdtemp())"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(Path(out).parent, run_dir)

    def test_monitored_run_cleans_up_after_exit_or_watchdog_abort(self):
        for delay, exit_code in ((0, 0), (0, 1), (60, 0)):
            with self.subTest(delay=delay, exit_code=exit_code):
                result = run_verification_test_monitored(
                    [sys.executable, "-c",
                     f"import os, time; print(os.environ['TMPDIR']); time.sleep({delay}); "
                     f"raise SystemExit({exit_code})"],
                    cwd=str(Path.cwd()), run_id="tmp-cleanup",
                    config=WatchdogConfig(0.4, 0.05, 10),
                    test_log_dir=verification_tmp_parent() / "logs",
                    tests_only_progress=True,
                )
                run_dir = Path(result.stdout.strip())
                self.assertEqual(run_dir.parent, verification_tmp_parent())
                self.assertFalse(run_dir.exists())
                self.assertEqual(result.stalled, bool(delay))
                self.assertEqual(result.exit_code, None if delay else exit_code)

    def test_monitored_run_cleans_up_when_spawn_fails(self):
        result = run_verification_test_monitored(
            [str(verification_tmp_parent() / "missing-executable")],
            cwd=str(Path.cwd()), run_id="tmp-spawn-failure",
            test_log_dir=verification_tmp_parent() / "logs",
        )
        self.assertIsInstance(result.exception, FileNotFoundError)
        self.assertEqual(list(verification_tmp_parent().glob("run-*")), [])

    def test_prune_removes_only_old_run_dirs(self):
        old = Path(build_verification_test_env({})["TMPDIR"])
        (old / "leftover").mkdir()
        fresh = Path(build_verification_test_env({})["TMPDIR"])
        past = time.time() - 7 * 3600
        os.utime(old, (past, past))
        self.assertEqual(prune_test_tmp(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_prune_without_parent_is_a_no_op(self):
        self.assertEqual(prune_test_tmp(), 0)


if __name__ == "__main__":
    unittest.main()


class CodingAgentTmpTests(unittest.TestCase):
    def test_a_coding_agent_run_gets_its_own_pruned_tmpdir(self):
        """2026-11-25: tests the agents ran themselves left ~3000 /tmp entries a day."""
        from modules.coding_worker import filtered_env

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(verification_env.tempfile, "gettempdir", return_value=tmp):
            first, second = filtered_env(), filtered_env()
            self.assertEqual(Path(first["TMPDIR"]).parent, verification_tmp_parent())
            self.assertNotEqual(first["TMPDIR"], second["TMPDIR"])
            self.assertEqual(first["TMPDIR"], first["TEMP"])
