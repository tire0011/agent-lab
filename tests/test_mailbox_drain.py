"""Mailbox worker behaviour under lock contention and inbox drain.

Covers issue #221: normal lock contention must not look like a failed unit,
and the processor that owns the lock must keep draining the inbox so a message
arriving mid-run is not stranded until the next timer invocation.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import mailbox_processor
import modules.experiment_service as experiment_service_module
import modules.mailbox as mailbox_module
from modules.mailbox.config import MailboxConfig, MailboxDrainLimits
from modules.mailbox.core import MailboxLock
from modules.mailbox.poll_runner import (
    MailboxPollRunResult,
    _drain_inbox,
    run_mailbox_poll_once,
)


class _DrainProcessor:
    """Stand-in processor whose inbox behaves like the real one.

    Processing a message removes its inbox file, exactly as a finalized
    mailbox commit does, so the drain loop sees the queue shrink.  ``on_pass``
    models a message arriving while the worker is busy.
    """

    def __init__(
        self,
        inbox: Path,
        *,
        on_pass: Callable[[int], None] | None = None,
        consume: bool = True,
    ) -> None:
        self.inbox = inbox
        self.scans = 0
        self.passes: list[list[str]] = []
        self._on_pass = on_pass
        self._consume = consume

    def _files(self) -> list[Path]:
        return sorted(p for p in self.inbox.iterdir() if p.suffix == ".md")

    def list_inbox_files(self) -> list[Path]:
        self.scans += 1
        return self._files()

    def process_inbox(self, files: list[Path] | None = None) -> list[dict[str, Any]]:
        if files is None:
            files = self._files()
        self.passes.append([p.name for p in files])
        results = []
        for path in files:
            if self._consume:
                path.unlink()
            results.append({"file": path.name, "status": "done"})
        if self._on_pass is not None:
            self._on_pass(len(self.passes))
        return results


class _InboxTempDir:
    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        self.inbox.mkdir()
        return self.inbox

    def __exit__(self, *_: object) -> None:
        self._tmp.cleanup()


def _write(inbox: Path, name: str) -> Path:
    path = inbox / name
    path.write_text("message\n", encoding="utf-8")
    return path


def _drain(processor: _DrainProcessor, **kwargs: Any) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "processor": processor,
        "mailbox_module": mailbox_module,
        "dry_run": False,
        "limits": MailboxDrainLimits(),
        "resync": None,
        "run_name": "test",
    }
    params.update(kwargs)
    return _drain_inbox(**params)


class InboxDrainTests(unittest.TestCase):
    def test_worker_exits_when_inbox_stays_empty(self) -> None:
        with _InboxTempDir() as inbox:
            processor = _DrainProcessor(inbox)

            results = _drain(processor)

            self.assertEqual(results, [])
            self.assertEqual(processor.passes, [[]])

    def test_single_batch_run_is_one_pass(self) -> None:
        """Normal case: everything available is processed by the first pass."""
        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            _write(inbox, "b.md")
            processor = _DrainProcessor(inbox)

            results = _drain(processor)

            self.assertEqual(processor.passes, [["a.md", "b.md"]])
            self.assertEqual([r["status"] for r in results], ["done", "done"])

    def test_active_processor_rescans_after_processing_a_batch(self) -> None:
        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox)

            _drain(processor)

            # One scan for the pre-processing snapshot, at least one after it.
            self.assertGreaterEqual(processor.scans, 2)

    def test_message_arriving_between_scans_is_processed_by_same_worker(self) -> None:
        def arrive(pass_number: int) -> None:
            if pass_number == 1:
                _write(inbox, "b.md")

        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox, on_pass=arrive)

            results = _drain(processor)

            self.assertEqual(processor.passes, [["a.md"], ["b.md"]])
            self.assertEqual([r["file"] for r in results], ["a.md", "b.md"])
            self.assertEqual(list(inbox.iterdir()), [])

    def test_message_left_in_inbox_is_not_reprocessed_in_same_run(self) -> None:
        """Deferred / retrying / exhausted messages must not spin the loop."""
        with _InboxTempDir() as inbox:
            _write(inbox, "stuck.md")
            processor = _DrainProcessor(inbox, consume=False)

            _drain(processor)

            self.assertEqual(processor.passes, [["stuck.md"]])
            self.assertTrue((inbox / "stuck.md").exists())

    def test_resync_runs_between_passes(self) -> None:
        calls: list[int] = []

        def arrive(pass_number: int) -> None:
            if pass_number == 1:
                _write(inbox, "b.md")

        def resync() -> bool:
            calls.append(1)
            return True

        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox, on_pass=arrive)

            _drain(processor, resync=resync)

            self.assertEqual(len(calls), 2)

    def test_failed_resync_ends_the_drain_without_losing_earlier_work(self) -> None:
        def arrive(pass_number: int) -> None:
            _write(inbox, f"later-{pass_number}.md")

        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox, on_pass=arrive)

            results = _drain(processor, resync=lambda: False)

            self.assertEqual(processor.passes, [["a.md"]])
            self.assertEqual([r["file"] for r in results], ["a.md"])
            self.assertTrue((inbox / "later-1.md").exists())


class InboxDrainLimitTests(unittest.TestCase):
    def test_pass_limit_stops_the_loop_and_leaves_work_for_the_next_run(self) -> None:
        def always_more(pass_number: int) -> None:
            _write(inbox, f"next-{pass_number}.md")

        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox, on_pass=always_more)

            _drain(processor, limits=MailboxDrainLimits(max_passes=3, max_seconds=3600.0))

            self.assertEqual(len(processor.passes), 3)
            remaining = [p.name for p in inbox.iterdir()]
            self.assertEqual(remaining, ["next-3.md"])

    def test_runtime_limit_stops_the_loop_and_leaves_work_for_the_next_run(self) -> None:
        def always_more(pass_number: int) -> None:
            _write(inbox, f"next-{pass_number}.md")

        with _InboxTempDir() as inbox:
            _write(inbox, "a.md")
            processor = _DrainProcessor(inbox, on_pass=always_more)

            _drain(processor, limits=MailboxDrainLimits(max_passes=100, max_seconds=0.0))

            self.assertEqual(len(processor.passes), 1)
            self.assertEqual([p.name for p in inbox.iterdir()], ["next-1.md"])

    def test_limits_come_from_the_environment_with_clamped_fallbacks(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AGENT_MAILBOX_DRAIN_MAX_PASSES": "9",
                "AGENT_MAILBOX_DRAIN_MAX_SECONDS": "12.5",
            },
        ):
            limits = MailboxDrainLimits.from_env()
        self.assertEqual(limits.max_passes, 9)
        self.assertEqual(limits.max_seconds, 12.5)

        with patch.dict(
            "os.environ",
            {
                "AGENT_MAILBOX_DRAIN_MAX_PASSES": "0",
                "AGENT_MAILBOX_DRAIN_MAX_SECONDS": "not-a-number",
            },
        ):
            limits = MailboxDrainLimits.from_env()
        # A misconfiguration must never disable processing altogether.
        self.assertEqual(limits.max_passes, 1)
        self.assertEqual(limits.max_seconds, MailboxDrainLimits().max_seconds)

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(MailboxDrainLimits.from_env(), MailboxDrainLimits())


class InboxDrainIntegrationTests(unittest.TestCase):
    """The drain loop against a real processor and a real git checkout."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(
            ["git", "init", str(self.repo)], check=True, capture_output=True, text=True
        )
        for key, value in (
            ("user.name", "Mailbox Drain Test"),
            ("user.email", "mailbox-drain@example.invalid"),
        ):
            subprocess.run(
                ["git", "-C", str(self.repo), "config", key, value],
                check=True,
                capture_output=True,
                text=True,
            )
        (self.repo / "inbox").mkdir()
        (self.repo / "outbox").mkdir()
        (self.repo / "state").mkdir()
        (self.repo / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
        (self.repo / "outbox" / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "seed mailbox"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.processor = mailbox_module.MailboxProcessor(
            repo_path=self.repo,
            db_path=str(self.repo / "state" / "agent.db"),
            dry_run=False,
            task_log_dir=self.repo / "state" / "experiment-logs",
        )

    def tearDown(self) -> None:
        self.processor.close()
        self._tmp.cleanup()

    def _message(self, name: str) -> Path:
        path = self.repo / "inbox" / name
        path.write_text(
            f"---\ntype: question\ncreated_by: drain-test\n---\n\nIs {name} handled?",
            encoding="utf-8",
        )
        return path

    def test_message_arriving_mid_run_is_drained_by_the_same_worker(self) -> None:
        self._message("first.md")
        arrived = []

        def resync() -> bool:
            # Stands in for the fetch that brings a newly pushed message into
            # the checkout while this worker still owns the lock.
            if not arrived:
                arrived.append(self._message("second.md"))
            return True

        results = _drain_inbox(
            processor=self.processor,
            mailbox_module=mailbox_module,
            dry_run=False,
            limits=MailboxDrainLimits(),
            resync=resync,
            run_name="integration",
        )

        self.assertEqual([r["status"] for r in results], ["done", "done"])
        self.assertEqual(
            sorted(Path(r["file"]).name for r in results), ["first.md", "second.md"]
        )
        remaining = [p.name for p in (self.repo / "inbox").iterdir() if p.suffix == ".md"]
        self.assertEqual(remaining, [])
        rows = self.processor.conn.execute(
            "SELECT status FROM mailbox_messages ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["done", "done"])

    def test_single_message_run_is_unchanged(self) -> None:
        self._message("only.md")

        results = _drain_inbox(
            processor=self.processor,
            mailbox_module=mailbox_module,
            dry_run=False,
            limits=MailboxDrainLimits(),
            resync=lambda: True,
            run_name="integration",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        self.assertFalse((self.repo / "inbox" / "only.md").exists())


class MailboxLockContentionTests(unittest.TestCase):
    def test_second_processor_reports_busy_without_processing_or_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "mailbox.lock"
            held = MailboxLock(lock_path)
            self.assertTrue(held.acquire())
            try:
                result = run_mailbox_poll_once(
                    db_path=str(root / "agent.db"),
                    mailbox_config=MailboxConfig.from_paths(root / "code", root / "mailbox"),
                    lock_path=lock_path,
                    dry_run=False,
                )
            finally:
                held.release()

        self.assertEqual(result.status, "busy")
        self.assertIsNone(result.error)
        self.assertEqual(result.results, [])

    def test_lock_is_released_after_a_processing_exception(self) -> None:
        class _Boom:
            def __init__(self, **_kwargs: Any) -> None:
                raise RuntimeError("experiment service unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "mailbox.lock"
            with patch.object(experiment_service_module, "ExperimentService", _Boom):
                result = run_mailbox_poll_once(
                    db_path=str(root / "agent.db"),
                    mailbox_config=MailboxConfig.from_paths(root / "code", root / "mailbox"),
                    lock_path=lock_path,
                    dry_run=False,
                )

            self.assertEqual(result.status, "error")
            follower = MailboxLock(lock_path)
            self.assertTrue(follower.acquire())
            follower.release()


class MailboxProcessorExitCodeTests(unittest.TestCase):
    """A oneshot systemd unit must not be marked failed by lock contention."""

    def _run_cli(self, status: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(
                mailbox_processor,
                "run_mailbox_poll_once",
                return_value=MailboxPollRunResult(status=status),
            ):
                return mailbox_processor.main(
                    [
                        "--db-path", str(root / "agent.db"),
                        "--code-repo-path", str(root / "code"),
                        "--mailbox-repo-path", str(root / "mailbox"),
                        "--lock-path", str(root / "mailbox.lock"),
                    ]
                )

    def test_busy_exits_successfully(self) -> None:
        self.assertEqual(self._run_cli("busy"), 0)

    def test_processed_still_exits_successfully(self) -> None:
        self.assertEqual(self._run_cli("processed"), 0)

    def test_real_failures_still_exit_non_zero(self) -> None:
        for status in ("dirty_worktree", "pull_failed", "error"):
            with self.subTest(status=status):
                self.assertEqual(self._run_cli(status), 1)


if __name__ == "__main__":
    unittest.main()
