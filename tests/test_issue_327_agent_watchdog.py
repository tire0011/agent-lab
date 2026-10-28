"""#327: the coding agent is progress-watched instead of cut off after 20 minutes.

In #319 the first experiment had finished its work (PR #321 was green) and was
still reported ``timed_out`` after exactly 20 minutes.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from modules.claude_worker import ClaudeWorker
from modules.codex_worker import CodexWorker
from modules.coding_worker import (
    STOP_MAX_RUNTIME,
    STOP_STALLED,
    agent_watchdog_limits,
    run_watched,
)
from modules.experiment_service import ExperimentService
from tests.test_issue_193_coding_provider_wiring import _ImplementingWorker, _init_git_repo

# Writes one line every 0.3s for about 2.5s, then exits 0.
CHATTY = "import time\nfor i in range(8):\n    print('step', i, flush=True)\n    time.sleep(0.3)\n"
SILENT = "import time\ntime.sleep(30)\n"
FOREVER = "import time\nwhile True:\n    print('.', flush=True)\n    time.sleep(0.2)\n"


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        # The process can vanish between opening and reading its stat file.
        return False


def _run(script: str, *, stall: float, cap: float):
    return run_watched(
        [sys.executable, "-c", script], cwd=tempfile.gettempdir(), env={},
        stall_seconds=stall, max_seconds=cap, poll_seconds=0.1,
    )


class RunWatchedTests(unittest.TestCase):
    def test_an_agent_that_keeps_writing_runs_past_the_stall_window(self):
        run = _run(CHATTY, stall=1.0, cap=20)
        self.assertIsNone(run.stop_reason)
        self.assertEqual(run.exit_code, 0)
        self.assertIn("step 7", run.stdout)

    def test_a_silent_agent_is_stopped_after_the_stall_window(self):
        started = time.monotonic()
        run = _run(SILENT, stall=1.0, cap=20)
        self.assertEqual(run.stop_reason, STOP_STALLED)
        self.assertLess(time.monotonic() - started, 10)

    def test_the_absolute_cap_still_holds(self):
        run = _run(FOREVER, stall=5.0, cap=1.5)
        self.assertEqual(run.stop_reason, STOP_MAX_RUNTIME)

    def test_a_stopped_agent_takes_its_child_processes_with_it(self):
        # Review on #328: a tool command the agent started must not keep
        # changing the worktree after the experiment has timed out.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            run = run_watched(
                [sys.executable, "-c", script], cwd=tmp, env={},
                stall_seconds=1.0, max_seconds=20, poll_seconds=0.1, grace_seconds=1.0,
            )
            self.assertEqual(run.stop_reason, STOP_STALLED)
            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(_alive(child_pid), "the agent's child process is still running")

    def test_captured_output_stays_bounded_and_keeps_start_and_end(self):
        # Review on #328: memory must not grow with the run; the parsers need
        # the first and the last events.
        script = (
            "print('FIRST', flush=True)\n"
            "line = 'x' * 99\n"
            "for _ in range(40000):\n"
            "    print(line)\n"
            "print('LAST', flush=True)\n"
        )
        run = _run(script, stall=5.0, cap=60)
        self.assertIsNone(run.stop_reason)
        self.assertLess(len(run.stdout), 2 * 1024 * 1024)
        self.assertTrue(run.stdout.startswith("FIRST"))
        self.assertTrue(run.stdout.rstrip().endswith("LAST"))
        self.assertIn("[... output truncated]", run.stdout)

    def test_limits_come_from_the_environment_with_floors(self):
        self.assertEqual(agent_watchdog_limits({}), (1800, 3 * 60 * 60))
        self.assertEqual(
            agent_watchdog_limits({"NEXUS_AGENT_STALL_SECONDS": "900", "NEXUS_AGENT_MAX_SECONDS": "7200"}),
            (900, 7200),
        )
        # A typo must not bring back a short fixed timeout.
        self.assertEqual(agent_watchdog_limits({"NEXUS_AGENT_STALL_SECONDS": "5"})[0], 120)


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def _agent(self, body: str) -> str:
        path = Path(self.dir) / "agent"
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(0o755)
        return str(path)

    def test_codex_reports_a_stall(self):
        worker = CodexWorker(codex_bin=self._agent(SILENT))
        result = worker.run(task="t", working_dir=self.dir, timeout_seconds=20, stall_seconds=1)
        self.assertTrue(result.timed_out)
        self.assertIn("stalled", result.error)

    def test_codex_finishes_a_slow_but_active_run(self):
        agent = self._agent(
            "import json, time\n"
            "for i in range(5):\n"
            "    print(json.dumps({'type': 'item.started'}), flush=True)\n"
            "    time.sleep(0.4)\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}), flush=True)\n"
        )
        result = CodexWorker(codex_bin=agent).run(
            task="t", working_dir=self.dir, timeout_seconds=20, stall_seconds=1
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.final_message, "done")

    def test_claude_streams_when_watched(self):
        worker = ClaudeWorker(claude_bin="claude")
        streamed = worker._build_command("workspace-write", stream=True)
        self.assertIn("stream-json", streamed)
        self.assertIn("--verbose", streamed)
        self.assertNotIn("stream-json", worker._build_command("workspace-write"))

    def test_claude_reads_its_result_from_the_stream(self):
        agent = self._agent(
            "import json, sys, time\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'assistant'}), flush=True)\n"
            "time.sleep(0.4)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'session_id': 's'}), flush=True)\n"
        )
        result = ClaudeWorker(claude_bin=agent).run(
            task="t", working_dir=self.dir, sandbox="workspace-write",
            timeout_seconds=20, stall_seconds=1,
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.final_message, "done")


class ExperimentUsesTheWatchdogTests(unittest.TestCase):
    def test_implementation_is_progress_watched(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        worker = _ImplementingWorker("codex")
        service = ExperimentService(
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=str(repo),
            worktrees_root=str(Path(tmp.name) / "worktrees"),
            worker_factory=lambda worktree: worker,
        )
        self.addCleanup(service.close)
        service.run_experiment(service.create_experiment(goal="Add a small helper.").id)
        implementation = [c for c in worker.calls if c.get("phase") == "implementation"]
        self.assertEqual(implementation[0]["stall_seconds"], 1800)
        self.assertEqual(implementation[0]["timeout_seconds"], 3 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
