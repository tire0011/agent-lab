"""#309: a test run never touches the Nexus database it was started next to.

The coding agent runs this suite inside an experiment worktree on the VM. A
lifespan test there opened the production database and marked the running
experiment "interrupted by service restart" (live #301 run, 2026-10-07).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class SuiteDatabaseIsolationTest(unittest.TestCase):
    def test_a_suite_run_leaves_a_running_experiment_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            production_db = str(Path(tmp) / "agent.db")
            setup = (
                "from modules.experiment_service.service import ExperimentService\n"
                "s = ExperimentService()\n"
                "e = s.create_experiment(goal='running in production')\n"
                "s._conn.execute(\"UPDATE experiments SET status='implementing'\")\n"
                "s._conn.commit()\n"
            )
            env = {**os.environ, "AGENT_DB_PATH": production_db}
            subprocess.run([sys.executable, "-c", setup], cwd=REPO_ROOT, env=env, check=True)

            # A test that runs the real API lifespan (startup recovery).
            run = subprocess.run(
                [
                    sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "tests/test_codex_api.py", "-k", "orphaned",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])

            rows = sqlite3.connect(production_db).execute(
                "SELECT status, error FROM experiments"
            ).fetchall()
            self.assertEqual(rows, [("implementing", None)])


if __name__ == "__main__":
    unittest.main()
