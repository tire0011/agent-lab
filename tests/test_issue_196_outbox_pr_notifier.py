"""Tests for issue #196: Outbox messages are published to the communication PR.

Covers normal delivery, duplicate processing, GitHub failure + retry, closed-PR
recovery (including a deleted head branch), and fallback recreation of the
channel. GitHub is exercised through the notifier's injectable transport, so no
network call is made.
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from modules.outbox_publisher import (
    PUBLISH_WRITTEN,
    OutboxMessage,
    OutboxPublisher,
    PublishRecord,
)
from modules.outbox_pr_notifier import (
    OUTBOX_MARKER,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_PENDING,
    OutboxPRNotifier,
    build_comment_body,
)


def _status_of(notifier, message_id):
    row = notifier._row(message_id)
    return dict(row) if row is not None else None

def _not_found() -> RuntimeError:
    from modules.outbox_pr_notifier import OutboxPRNotifyError

    return OutboxPRNotifyError("GitHub HTTP 404: Not Found")


class _FakeGitHub:
    """GitHub double that models branches and file contents, not just calls.

    Branch/file state is tracked because the interesting recovery paths turn
    on it: a PR cannot be opened from a branch identical to its base, and a
    file that already exists can only be written with its blob sha.
    """

    COMM_FILE = "docs/nexus-communication.md"

    def __init__(
        self,
        *,
        pr_state: str = "open",
        pr_number: int = 194,
        branch_exists: bool = True,
        merged: bool = False,
        pr_missing: bool = False,
        fail_comments: int = 0,
        base_file: str | None = None,
    ) -> None:
        self.pr_state = pr_state
        self.pr_number = pr_number
        self.branch_exists = branch_exists
        self.merged = merged
        self.pr_missing = pr_missing
        self.fail_comments = fail_comments
        # Content of the channel file on the base branch; set when a previous
        # communication PR was merged into main.
        self.base_file = base_file
        self.branch_files: dict[str, str | None] = {}
        if branch_exists:
            self.branch_files["nexus-communication"] = base_file
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.comments: list[dict[str, Any]] = []
        self.created_pulls: list[dict[str, Any]] = []
        self._next_comment_id = 1000

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _branch_of(path: str) -> str:
        return path.rsplit("/", 1)[-1].split("?")[0]

    def __call__(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, payload))

        if method == "GET" and path.endswith(f"/pulls/{self.pr_number}"):
            if self.pr_missing:
                raise _not_found()
            return {
                "number": self.pr_number,
                "state": self.pr_state,
                "merged": self.merged,
                "head": {"ref": "nexus-communication", "sha": "headsha"},
            }

        if method == "PATCH" and path.endswith(f"/pulls/{self.pr_number}"):
            self.pr_state = "open"
            return {"number": self.pr_number, "state": "open"}

        if method == "GET" and "/git/ref/heads/" in path:
            branch = self._branch_of(path)
            if branch == "main":
                return {"object": {"sha": "basesha"}}
            if branch == "nexus-communication" and not self.branch_exists:
                raise _not_found()
            if branch != "nexus-communication" and branch not in self.branch_files:
                raise _not_found()
            return {"object": {"sha": "headsha"}}

        if method == "POST" and path.endswith("/git/refs"):
            branch = payload["ref"].removeprefix("refs/heads/")
            if branch in self.branch_files:
                raise RuntimeError("GitHub HTTP 422: Reference already exists")
            # A fresh branch starts out identical to the base branch.
            self.branch_files[branch] = self.base_file
            if branch == "nexus-communication":
                self.branch_exists = True
            return {"ref": payload["ref"]}

        if method == "GET" and "/contents/" in path:
            branch = path.split("?ref=")[1]
            content = self.branch_files.get(branch)
            if content is None:
                raise _not_found()
            return {"sha": f"blob-{branch}", "content": content}

        if method == "PUT" and "/contents/" in path:
            branch = payload["branch"]
            existing = self.branch_files.get(branch)
            if existing is not None and not payload.get("sha"):
                raise RuntimeError("GitHub HTTP 422: sha wasn't supplied")
            self.branch_files[branch] = base64.b64decode(payload["content"]).decode("utf-8")
            return {"commit": {"sha": f"commit-{branch}"}}

        if method == "POST" and path.endswith("/pulls"):
            head = payload["head"]
            if self.branch_files.get(head) == self.base_file:
                raise RuntimeError("GitHub HTTP 422: No commits between main and " + head)
            self.pr_number = 900
            self.pr_state = "open"
            self.pr_missing = False
            self.merged = False
            pull = {"number": 900, "html_url": "https://github.com/o/r/pull/900"}
            self.created_pulls.append(pull)
            return pull

        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            if self.fail_comments > 0:
                self.fail_comments -= 1
                raise RuntimeError("GitHub HTTP 502: bad gateway")
            self._next_comment_id += 1
            comment = {"id": self._next_comment_id, "body": payload["body"], "path": path}
            self.comments.append(comment)
            return comment

        raise AssertionError(f"unexpected GitHub call: {method} {path}")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class OutboxPRNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "agent.db")
        self.clock = _Clock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _notifier(self, github: _FakeGitHub, **kwargs: Any) -> OutboxPRNotifier:
        notifier = OutboxPRNotifier(
            db_path=self.db_path,
            repository="tire0011/agent-lab",
            transport=github,
            pr_number=194,
            base_backoff_seconds=10.0,
            clock=self.clock,
            **kwargs,
        )
        self.addCleanup(notifier.close)
        return notifier

    def test_delivers_comment_with_marker_content_and_id(self) -> None:
        github = _FakeGitHub()
        notifier = self._notifier(github)

        self.assertTrue(notifier.notify("msg-1", "hello outbox", created_at="2026-09-21T10:00:00Z"))

        self.assertEqual(len(github.comments), 1)
        body = github.comments[0]["body"]
        self.assertIn(OUTBOX_MARKER, body)
        self.assertIn("msg-1", body)
        self.assertIn("hello outbox", body)
        self.assertIn("2026-09-21T10:00:00Z", body)
        self.assertTrue(github.comments[0]["path"].endswith("/issues/194/comments"))

        status = _status_of(notifier, "msg-1")
        self.assertEqual(status["status"], STATUS_DELIVERED)
        self.assertEqual(status["pr_number"], 194)
        self.assertIsNotNone(status["comment_id"])

    def test_reprocessing_the_same_message_posts_once(self) -> None:
        github = _FakeGitHub()
        notifier = self._notifier(github)

        notifier.notify("msg-1", "hello")
        notifier.notify("msg-1", "hello")
        notifier.deliver_pending()

        self.assertEqual(len(github.comments), 1)

    def test_github_failure_keeps_notification_pending_and_retries(self) -> None:
        github = _FakeGitHub(fail_comments=1)
        notifier = self._notifier(github)

        self.assertFalse(notifier.notify("msg-1", "hello"))
        pending = _status_of(notifier, "msg-1")
        self.assertEqual(pending["status"], STATUS_PENDING)
        self.assertEqual(pending["attempts"], 1)
        self.assertIn("502", pending["last_error"])

        # Backoff is respected: nothing is retried before next_attempt_at.
        self.assertEqual(notifier.deliver_pending(), 0)
        self.assertEqual(len(github.comments), 0)

        self.clock.now += 60.0
        self.assertEqual(notifier.deliver_pending(), 1)
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(_status_of(notifier, "msg-1")["status"], STATUS_DELIVERED)

    def test_retries_are_bounded(self) -> None:
        github = _FakeGitHub(fail_comments=99)
        notifier = self._notifier(github, max_attempts=3)

        notifier.notify("msg-1", "hello")
        for _ in range(5):
            self.clock.now += 10_000.0
            notifier.deliver_pending()

        status = _status_of(notifier, "msg-1")
        self.assertEqual(status["status"], STATUS_FAILED)
        self.assertEqual(status["attempts"], 3)

    def test_closed_pr_is_reopened_before_posting(self) -> None:
        github = _FakeGitHub(pr_state="closed")
        notifier = self._notifier(github)

        self.assertTrue(notifier.notify("msg-1", "hello"))

        self.assertEqual(github.pr_state, "open")
        self.assertTrue(
            any(m == "PATCH" and p.endswith("/pulls/194") for m, p, _ in github.calls)
        )
        self.assertEqual(github.comments[0]["path"].split("/issues/")[1], "194/comments")

    def test_deleted_branch_is_recreated_before_reopening(self) -> None:
        github = _FakeGitHub(pr_state="closed", branch_exists=False)
        notifier = self._notifier(github)

        self.assertTrue(notifier.notify("msg-1", "hello"))

        created = [p for m, p, _ in github.calls if m == "POST" and p.endswith("/git/refs")]
        self.assertEqual(len(created), 1)
        self.assertTrue(github.branch_exists)
        self.assertEqual(_status_of(notifier, "msg-1")["pr_number"], 194)

    def test_unavailable_pr_falls_back_to_a_new_channel(self) -> None:
        github = _FakeGitHub(pr_missing=True, branch_exists=False)
        notifier = self._notifier(github)

        with self.assertLogs("modules.outbox_pr_notifier", level="ERROR") as logs:
            self.assertTrue(notifier.notify("msg-1", "hello"))

        self.assertEqual(len(github.created_pulls), 1)
        self.assertEqual(_status_of(notifier, "msg-1")["pr_number"], 900)
        self.assertTrue(any("CHANNEL CHANGED" in line for line in logs.output))
        self.assertTrue(any("#900" in line for line in logs.output))

        # The new channel is reused for later messages instead of retrying #194.
        github.calls.clear()
        self.assertTrue(notifier.notify("msg-2", "second"))
        self.assertTrue(any(p.endswith("/pulls/900") for _, p, _ in github.calls))

    def test_merged_pr_is_never_reused(self) -> None:
        github = _FakeGitHub(pr_state="closed", merged=True)
        notifier = self._notifier(github)

        with self.assertLogs("modules.outbox_pr_notifier", level="ERROR"):
            self.assertTrue(notifier.notify("msg-1", "hello"))

        self.assertEqual(len(github.created_pulls), 1)
        self.assertFalse(
            any(m == "PATCH" for m, _, _ in github.calls), "a merged PR must not be reopened"
        )

    def test_recreation_after_a_merge_gets_its_own_branch_and_commit(self) -> None:
        """The merged channel file is already in base, so reusing the old
        branch would leave nothing to open a PR from."""
        merged_file = "# Nexus communication\n\nalready merged into main\n"
        github = _FakeGitHub(pr_state="closed", merged=True, base_file=merged_file)
        notifier = self._notifier(github)

        with self.assertLogs("modules.outbox_pr_notifier", level="ERROR"):
            self.assertTrue(notifier.notify("msg-1", "hello"))

        self.assertEqual(len(github.created_pulls), 1)
        head = [p for m, p, pl in github.calls if m == "POST" and p.endswith("/pulls")]
        created = [pl for m, p, pl in github.calls if m == "POST" and p.endswith("/pulls")][0]
        self.assertTrue(head)
        self.assertNotEqual(
            created["head"], "nexus-communication", "recreation must cut a fresh branch"
        )
        self.assertTrue(created["head"].startswith("nexus-communication-"))
        # The fresh branch carries a commit, so it differs from the base.
        self.assertNotEqual(github.branch_files[created["head"]], merged_file)
        self.assertEqual(len(github.comments), 1)

    def test_reusing_the_merged_branch_would_have_been_rejected(self) -> None:
        """Guards the test above: the fake rejects a no-diff pull request,
        which is what the previous implementation would have produced."""
        merged_file = "# Nexus communication\n\nalready merged into main\n"
        github = _FakeGitHub(base_file=merged_file)

        with self.assertRaises(RuntimeError) as raised:
            github("POST", "/repos/o/r/pulls", {"head": "nexus-communication", "base": "main"})

        self.assertIn("No commits between", str(raised.exception))

    def test_unconfigured_notifier_never_raises(self) -> None:
        notifier = OutboxPRNotifier(db_path=self.db_path, repository=None, token=None)
        self.addCleanup(notifier.close)

        self.assertFalse(notifier.configured)
        self.assertFalse(notifier.notify("msg-1", "hello"))
        self.assertEqual(_status_of(notifier, "msg-1")["status"], STATUS_PENDING)


class CommentBodyTests(unittest.TestCase):
    def test_content_with_backticks_stays_lossless(self) -> None:
        content = "before\n```\ncode\n```\nafter"
        body = build_comment_body("msg-1", content, "2026-09-21T10:00:00Z")
        self.assertIn(content, body)
        self.assertIn("````", body)

    def test_oversized_content_is_truncated_but_keeps_identity(self) -> None:
        body = build_comment_body("msg-1", "x" * 200_000, "2026-09-21T10:00:00Z")
        self.assertLess(len(body), 65_536)
        self.assertIn(OUTBOX_MARKER, body)
        self.assertIn("msg-1", body)
        self.assertIn("truncated", body)


class _StubNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.enqueued: list[tuple[str, str]] = []
        self.error = error

    def enqueue(self, message_id: str, content: str, created_at: str | None = None) -> None:
        self.enqueued.append((message_id, content))

    def notify(self, message_id: str, content: str) -> bool:
        self.calls.append((message_id, content))
        if self.error is not None:
            raise self.error
        return True


class PublisherNotificationTests(unittest.TestCase):
    """The publisher announces a message only after it is safely pushed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name) / "mailbox"
        (self.repo_root / "outbox").mkdir(parents=True)
        self.db_path = str(Path(self._tmp.name) / "agent.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _publisher(self, notifier: Any, *, published: bool) -> OutboxPublisher:
        publisher = OutboxPublisher(
            repo_root=self.repo_root, db_path=self.db_path, notifier=notifier
        )
        self.addCleanup(publisher.close)
        publisher._publish_locked = lambda record: published  # type: ignore[assignment]
        return publisher

    @staticmethod
    def _record() -> PublishRecord:
        return PublishRecord(
            message=OutboxMessage(id="msg-1", content="hello"),
            publish_status=PUBLISH_WRITTEN,
        )

    def test_pushed_message_is_announced(self) -> None:
        notifier = _StubNotifier()
        self.assertTrue(self._publisher(notifier, published=True).publish(self._record()))
        self.assertEqual(notifier.calls, [("msg-1", "hello")])

    def test_unpublished_message_is_not_announced_yet(self) -> None:
        notifier = _StubNotifier()
        self.assertFalse(self._publisher(notifier, published=False).publish(self._record()))
        self.assertEqual(notifier.calls, [])
        # The intent is still durable: the publish is retried by the next
        # poll, and the pending notification is delivered either way.
        self.assertEqual(notifier.enqueued, [("msg-1", "hello")])

    def test_notification_is_enqueued_before_the_push(self) -> None:
        """A crash after `pushed` must not be able to strand the message.

        A pushed record is never selected by `fetch_pending` again, so the
        enqueue cannot wait until after the push.
        """
        order: list[str] = []
        notifier = _StubNotifier()
        notifier.enqueue = lambda *a, **k: order.append("enqueue")  # type: ignore[assignment]
        publisher = OutboxPublisher(
            repo_root=self.repo_root, db_path=self.db_path, notifier=notifier
        )
        self.addCleanup(publisher.close)

        def _publish(record: PublishRecord) -> bool:
            order.append("push")
            return True

        publisher._publish_locked = _publish  # type: ignore[assignment]
        publisher.publish(self._record())

        self.assertEqual(order, ["enqueue", "push"])

    def test_crash_after_push_still_delivers_exactly_one_comment(self) -> None:
        """The regression the review asked for: outbox reaches `pushed`, the
        process dies before delivery, and the next poll posts one comment."""
        github = _FakeGitHub()
        notifier = OutboxPRNotifier(
            db_path=self.db_path,
            repository="tire0011/agent-lab",
            transport=github,
            pr_number=194,
            base_backoff_seconds=0.0,
        )
        self.addCleanup(notifier.close)
        publisher = OutboxPublisher(
            repo_root=self.repo_root, db_path=self.db_path, notifier=notifier
        )
        self.addCleanup(publisher.close)
        publisher._publish_locked = lambda record: True  # type: ignore[assignment]
        # The process dies between the push and the delivery attempt: the
        # delivery simply never runs.
        notifier.notify = lambda *a, **k: None  # type: ignore[assignment]

        self.assertTrue(publisher.publish(self._record()))
        self.assertEqual(github.comments, [], "nothing was delivered before the crash")

        # Restart: a fresh notifier on the same database recovers the message,
        # even though the producer row will never be published again.
        restarted = OutboxPRNotifier(
            db_path=self.db_path,
            repository="tire0011/agent-lab",
            transport=github,
            pr_number=194,
        )
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.deliver_pending(), 1)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("msg-1", github.comments[0]["body"])

        # And the recovery is itself idempotent.
        self.assertEqual(restarted.deliver_pending(), 0)
        self.assertEqual(len(github.comments), 1)

    def test_notifier_failure_never_loses_the_published_message(self) -> None:
        notifier = _StubNotifier(error=RuntimeError("github down"))
        publisher = self._publisher(notifier, published=True)
        with self.assertLogs("modules.outbox_publisher", level="WARNING"):
            self.assertTrue(publisher.publish(self._record()))


if __name__ == "__main__":
    unittest.main()
