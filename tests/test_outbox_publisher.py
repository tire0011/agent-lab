"""Tests for modules/outbox_publisher.py.

Covers the shared persistent publish lifecycle used by both
MailboxTaskService (task-approval results) and OperationChainService
(operation-chain results).

Acceptance criteria from the issue (§ Tests und Akzeptanzkriterien):
  1.  Task result: written, committed, pushed.
  2.  Chain result: same shared lifecycle.
  3.  Recovery after crash right after write (DB=written, not committed yet).
  4.  Recovery after commit but before push.
  5.  Push failure → retry without new commit.
  6.  Successful push with missing DB update is detected.
  7.  Restart adopts existing untracked matching result file.
  8.  Two concurrent publishers produce only one file / one commit.
  9.  Foreign untracked file: never staged or committed.
  10. Code repo stays fully untouched in two-repo mode.
  11. Legacy layout is backward-compatible.
  12. Existing tests stay green (tested by running the full suite).
  13. `git diff --check` is clean.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from modules.mailbox_config import MailboxConfig
from modules.mailbox import git_is_clean
from modules.outbox_publisher import (
    PUBLISH_COMMITTED,
    PUBLISH_FAILED,
    PUBLISH_PENDING_WRITE,
    PUBLISH_PUSHED,
    PUBLISH_WRITTEN,
    OperationChainOutboxPublisher,
    PublishRecord,
    TaskApprovalOutboxPublisher,
)
from modules.run_store import init_db


# ---------------------------------------------------------------------------
# Git helpers (test-only)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("# test\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)


def _make_bare_remote(tmp: Path, name: str = "remote.git") -> Path:
    remote = tmp / name
    _git(["init", "--bare", str(remote)], tmp)
    return remote


def _make_repo_with_remote(tmp: Path) -> tuple[Path, Path]:
    remote = _make_bare_remote(tmp)
    repo = tmp / "repo"
    _make_repo(repo)
    (repo / "communication" / "outbox").mkdir(parents=True)
    (repo / "communication" / "inbox").mkdir(parents=True)
    _git(["add", "-A"], repo)
    _git(["commit", "--allow-empty", "-m", "add outbox dirs"], repo, check=False)
    _git(["remote", "add", "origin", str(remote)], repo)
    branch = _git(["branch", "--show-current"], repo).stdout.strip()
    _git(["push", "-u", "origin", branch], repo)
    return repo, remote


def _remote_head(remote: Path, rel_path: str) -> str:
    """Read a file from the remote HEAD commit (bare-repo safe)."""
    result = _git(["--git-dir", str(remote), "show", f"HEAD:{rel_path}"], remote.parent)
    return result.stdout


def _commit_count(repo: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], repo).stdout.strip())


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _insert_chain(
    db_path: str,
    request_id: str,
    outbox_rel: str,
    publish_status: str = PUBLISH_WRITTEN,
    commit_sha: str | None = None,
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO operation_chains
           (id, request_id, status, created_at, request_json, updated_at,
            outbox_file, publish_status, publish_commit_sha)
           VALUES (?,?,  'completed','2026-01-01T00:00:00Z','{}','2026-01-01T00:00:00Z',
                   ?,?,?)""",
        (request_id, request_id, outbox_rel, publish_status, commit_sha),
    )
    conn.commit()
    conn.close()


def _insert_approval(
    db_path: str,
    approval_id: str,
    outbox_rel: str,
    publish_status: str = PUBLISH_WRITTEN,
    commit_sha: str | None = None,
) -> None:
    conn = init_db(db_path)
    # mailbox_task_approvals requires a parent mailbox_messages row
    conn.execute(
        """INSERT INTO mailbox_messages
           (file_path, source_commit_sha, msg_type, created_by, status, received_at)
           VALUES ('inbox/t.md','abc','task','sender','processed','2026-01-01T00:00:00Z')"""
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO mailbox_task_approvals
           (id, mailbox_msg_id, task_body, sender, file_path, received_at,
            status, outbox_file, publish_status, publish_commit_sha,
            created_at, updated_at)
           VALUES (?,?,  'body','sender','inbox/t.md','2026-01-01T00:00:00Z',
                   'succeeded',?,?,?,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')""",
        (approval_id, msg_id, outbox_rel, publish_status, commit_sha),
    )
    conn.commit()
    conn.close()


def _get_chain(db_path: str, request_id: str) -> dict:
    conn = init_db(db_path)
    cur = conn.execute(
        "SELECT * FROM operation_chains WHERE request_id=?", (request_id,)
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    conn.close()
    return dict(zip(cols, row))


def _get_approval(db_path: str, approval_id: str) -> dict:
    conn = init_db(db_path)
    cur = conn.execute(
        "SELECT * FROM mailbox_task_approvals WHERE id=?", (approval_id,)
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    conn.close()
    return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Helpers to build MailboxConfig for both layouts
# ---------------------------------------------------------------------------


def _legacy_config(repo: Path) -> MailboxConfig:
    return MailboxConfig.legacy(repo)


def _external_config(repo: Path, mailbox: Path) -> MailboxConfig:
    (mailbox / "outbox").mkdir(parents=True, exist_ok=True)
    (mailbox / "inbox").mkdir(parents=True, exist_ok=True)
    return MailboxConfig.from_paths(repo, mailbox)


# ============================================================================
# 1. Task result: written → committed → pushed  (AC #1)
# ============================================================================


class TestTaskApprovalPublishLifecycle(unittest.TestCase):
    """AC #1: task result is written, committed, pushed."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_task_approval_written_committed_pushed(self):
        outbox_rel = "communication/outbox/task-abc.md"
        (self.repo / outbox_rel).write_text("task result\n")
        _insert_approval(self.db, "apv1", outbox_rel)

        pub = TaskApprovalOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_approval(self.db, "apv1")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertTrue(row["publish_commit_sha"])
        self.assertIsNone(row["publish_last_error"])
        self.assertEqual(_remote_head(self.remote, outbox_rel), "task result\n")
        self.assertTrue(git_is_clean(self.repo))

    def test_task_approval_external_layout(self):
        """AC #11: external layout (mailbox repo separate from code repo)."""
        ctx2 = tempfile.TemporaryDirectory()
        try:
            tmp2 = Path(ctx2.name)
            mailbox, remote2 = _make_repo_with_remote(tmp2)
            (mailbox / "outbox").mkdir(exist_ok=True)
            branch = _git(["branch", "--show-current"], mailbox).stdout.strip()
            _git(["push", "origin", branch], mailbox)

            cfg = _external_config(self.repo, mailbox)
            outbox_rel = "outbox/task-ext.md"
            (mailbox / outbox_rel).write_text("external task\n")
            _insert_approval(self.db, "apv-ext", outbox_rel)

            pub = TaskApprovalOutboxPublisher(mailbox_config=cfg, db_path=self.db)
            try:
                count = pub.publish_pending()
            finally:
                pub.close()

            self.assertEqual(count, 1)
            row = _get_approval(self.db, "apv-ext")
            self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
            self.assertEqual(_remote_head(remote2, outbox_rel), "external task\n")
        finally:
            ctx2.cleanup()


# ============================================================================
# 2. Chain result: same shared lifecycle  (AC #2)
# ============================================================================


class TestChainPublishLifecycle(unittest.TestCase):
    """AC #2: chain result uses the same shared lifecycle."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_chain_written_committed_pushed(self):
        outbox_rel = "communication/outbox/chain-r1.md"
        (self.repo / outbox_rel).write_text("chain result\n")
        _insert_chain(self.db, "r1", outbox_rel)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_chain(self.db, "r1")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "chain result\n")
        self.assertTrue(git_is_clean(self.repo))


# ============================================================================
# 3. Recovery after crash right after write  (AC #3)
# ============================================================================


class TestRecoveryAfterWrite(unittest.TestCase):
    """AC #3: DB=written (file exists, not committed) → commit + push."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_recovery_written_file_not_committed(self):
        outbox_rel = "communication/outbox/crash-after-write.md"
        (self.repo / outbox_rel).write_text("crash body\n")
        _insert_chain(self.db, "r-crash", outbox_rel, publish_status=PUBLISH_WRITTEN)
        commits_before = _commit_count(self.repo)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        self.assertGreater(_commit_count(self.repo), commits_before)
        row = _get_chain(self.db, "r-crash")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "crash body\n")

    def test_recovery_committing_state_file_not_committed(self):
        """DB=committing, file exists → treat same as written: commit + push."""
        outbox_rel = "communication/outbox/crash-committing.md"
        (self.repo / outbox_rel).write_text("committing body\n")
        _insert_chain(self.db, "r-commt", outbox_rel, publish_status="committing")

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-commt")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)


# ============================================================================
# 4. Recovery after commit, before push  (AC #4)
# ============================================================================


class TestRecoveryAfterCommit(unittest.TestCase):
    """AC #4: DB=committed + sha present → push only, no new commit."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_push_existing_commit(self):
        outbox_rel = "communication/outbox/committed.md"
        (self.repo / outbox_rel).write_text("committed body\n")
        _git(["add", "--", outbox_rel], self.repo)
        _git(["commit", "--no-verify", "-m", "outbox: publish r-committed"], self.repo)
        sha = _git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        commits_before = _commit_count(self.repo)

        _insert_chain(self.db, "r-committed", outbox_rel,
                      publish_status=PUBLISH_COMMITTED, commit_sha=sha)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        self.assertEqual(_commit_count(self.repo), commits_before)  # no extra commit
        row = _get_chain(self.db, "r-committed")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], sha)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "committed body\n")


# ============================================================================
# 5. Push failure → retry without new commit  (AC #5)
# ============================================================================


class TestPushFailureRetry(unittest.TestCase):
    """AC #5: push fails → retry only pushes the existing commit."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_push_retry_reuses_same_commit(self):
        outbox_rel = "communication/outbox/retry.md"
        (self.repo / outbox_rel).write_text("retry body\n")
        _insert_chain(self.db, "r-retry", outbox_rel)

        # Break the remote so first push fails
        _git(["remote", "set-url", "origin",
              str(Path(self.ctx.name) / "nonexistent.git")], self.repo)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 0)
            row_after_fail = _get_chain(self.db, "r-retry")
            self.assertEqual(row_after_fail["publish_status"], PUBLISH_FAILED)
            sha_after_fail = row_after_fail["publish_commit_sha"]
            self.assertTrue(sha_after_fail)
            commits_after_fail = _commit_count(self.repo)

            # Restore remote and retry: same commit, no new one
            _git(["remote", "set-url", "origin", str(self.remote)], self.repo)
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-retry")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], sha_after_fail)
        self.assertEqual(_commit_count(self.repo), commits_after_fail)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "retry body\n")


# ============================================================================
# 6. Successful push with missing DB update detected  (AC #6)
# ============================================================================


class TestCrashAfterCommitBeforeDbUpdate(unittest.TestCase):
    """AC #6: crash after git commit but before DB write → recover SHA."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_crash_after_commit_sha_recovered(self):
        outbox_rel = "communication/outbox/crash-sha.md"
        (self.repo / outbox_rel).write_text("sha recovery\n")
        # Manually commit as if the publisher committed but crashed before writing SHA to DB
        _git(["add", "--", outbox_rel], self.repo)
        _git(["commit", "--no-verify", "-m", "outbox: publish r-sha"], self.repo)
        real_sha = _git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        commits_before = _commit_count(self.repo)

        # DB still says committing (crash before DB update)
        _insert_chain(self.db, "r-sha", outbox_rel, publish_status="committing")

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        self.assertEqual(_commit_count(self.repo), commits_before)  # no duplicate
        row = _get_chain(self.db, "r-sha")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], real_sha)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "sha recovery\n")


# ============================================================================
# 7. Restart adopts existing untracked file  (AC #7)
# ============================================================================


class TestAdoptExistingUntrackedFile(unittest.TestCase):
    """AC #7: pending_write + file already exists on disk → adopt as-is."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_adopt_untracked_outbox_file(self):
        outbox_rel = "communication/outbox/existing.md"
        (self.repo / outbox_rel).write_text("pre-existing content\n")
        # DB says pending_write (file was written but DB not advanced yet)
        _insert_chain(self.db, "r-adopt", outbox_rel, publish_status=PUBLISH_PENDING_WRITE)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-adopt")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "pre-existing content\n")


# ============================================================================
# 8. Two concurrent publishers → one file / one commit  (AC #8)
# ============================================================================


class TestConcurrentPublishers(unittest.TestCase):
    """AC #8: concurrent publishers produce only one commit."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_concurrent_publishers_single_commit(self):
        outbox_rel = "communication/outbox/concurrent.md"
        (self.repo / outbox_rel).write_text("concurrent\n")
        _insert_chain(self.db, "r-conc", outbox_rel)
        commits_before = _commit_count(self.repo)

        results: list[int] = []
        errors: list[Exception] = []

        def run():
            pub = OperationChainOutboxPublisher(
                mailbox_config=self.cfg, db_path=self.db
            )
            try:
                results.append(pub.publish_pending())
            except Exception as exc:
                errors.append(exc)
            finally:
                pub.close()

        t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertFalse(errors, errors)
        # At least one publisher succeeded.
        self.assertGreaterEqual(sum(results), 1)
        # Exactly ONE commit was created (the per-repo lock prevents duplicate commits).
        self.assertEqual(_commit_count(self.repo), commits_before + 1)


# ============================================================================
# 9. Foreign untracked file: never staged or committed  (AC #9)
# ============================================================================


class TestForeignUntrackedFile(unittest.TestCase):
    """AC #9: foreign untracked files are never staged or committed."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_foreign_untracked_not_staged(self):
        outbox_rel = "communication/outbox/target.md"
        (self.repo / outbox_rel).write_text("only me\n")
        _insert_chain(self.db, "r-foreign", outbox_rel)

        foreign = self.repo / "communication" / "outbox" / "foreign.md"
        foreign.write_text("DO NOT STAGE\n")
        (self.repo / "scratch.txt").write_text("also untracked\n")

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        # Commit must contain ONLY the target file
        show = _git(["show", "--name-only", "--pretty=format:", "HEAD"], self.repo)
        committed_files = [l for l in show.stdout.splitlines() if l.strip()]
        self.assertEqual(committed_files, [outbox_rel])

        # Foreign files remain untracked
        status = _git(["status", "--porcelain"], self.repo).stdout
        self.assertIn("foreign.md", status)
        self.assertIn("scratch.txt", status)
        for line in status.splitlines():
            if "foreign.md" in line or "scratch.txt" in line:
                self.assertTrue(line.startswith("??"), f"unexpected status: {line!r}")


# ============================================================================
# 10. Code repo untouched in two-repo mode  (AC #10)
# ============================================================================


class TestCodeRepoUntouchedTwoRepoMode(unittest.TestCase):
    """AC #10: code repo stays completely unchanged when repos are separate."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.ctx.name)

    def tearDown(self):
        self.ctx.cleanup()

    def test_code_repo_unchanged(self):
        # Code repo (no outbox)
        code = self.tmp / "code"
        _make_repo(code)
        code_sha_before = _git(["rev-parse", "HEAD"], code).stdout.strip()

        # Mailbox repo with remote
        mailbox, remote = _make_repo_with_remote(self.tmp)
        (mailbox / "outbox").mkdir(exist_ok=True)
        branch = _git(["branch", "--show-current"], mailbox).stdout.strip()
        _git(["push", "-u", "origin", branch], mailbox)

        cfg = MailboxConfig.from_paths(code, mailbox)
        db = str(self.tmp / "state.db")
        outbox_rel = "outbox/chain-two-repo.md"
        (mailbox / outbox_rel).write_text("two-repo result\n")
        _insert_chain(db, "r-two", outbox_rel)

        pub = OperationChainOutboxPublisher(mailbox_config=cfg, db_path=db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        # Code repo HEAD is unchanged
        code_sha_after = _git(["rev-parse", "HEAD"], code).stdout.strip()
        self.assertEqual(code_sha_before, code_sha_after)
        self.assertTrue(git_is_clean(code))

        # Mailbox repo was updated
        self.assertEqual(_remote_head(remote, outbox_rel), "two-repo result\n")


# ============================================================================
# 11. Legacy layout backward-compatible  (AC #11)
# ============================================================================


class TestLegacyLayout(unittest.TestCase):
    """AC #11: legacy single-repo layout works via MailboxConfig.legacy()."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")

    def tearDown(self):
        self.ctx.cleanup()

    def test_legacy_config_publishes(self):
        cfg = MailboxConfig.legacy(self.repo)
        outbox_rel = "communication/outbox/legacy.md"
        (self.repo / outbox_rel).write_text("legacy content\n")
        _insert_chain(self.db, "r-legacy", outbox_rel)

        pub = OperationChainOutboxPublisher(mailbox_config=cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-legacy")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "legacy content\n")


# ============================================================================
# pending_write: re-write from content_fn when file is missing
# ============================================================================


class TestPendingWriteRecovery(unittest.TestCase):
    """pending_write + file missing → content_fn re-writes, then commits."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, self.remote = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_content_fn_recreates_missing_file(self):
        outbox_rel = "communication/outbox/recreated.md"
        _insert_chain(self.db, "r-recreate", outbox_rel, publish_status=PUBLISH_PENDING_WRITE)

        record = PublishRecord(
            record_id="r-recreate",
            outbox_file=outbox_rel,
            publish_status=PUBLISH_PENDING_WRITE,
            content_fn=lambda: "recreated content\n",
        )

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertTrue(pub.publish(record))
        finally:
            pub.close()

        row = _get_chain(self.db, "r-recreate")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(_remote_head(self.remote, outbox_rel), "recreated content\n")


# ============================================================================
# Path traversal / symlink rejection
# ============================================================================


class TestSecurityRejections(unittest.TestCase):
    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, _ = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_symlink_rejected(self):
        real = self.repo / "communication" / "outbox" / "real.md"
        real.write_text("real\n")
        link = self.repo / "communication" / "outbox" / "link.md"
        link.symlink_to(real)
        _insert_chain(self.db, "r-link", "communication/outbox/link.md")

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 0)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-link")
        self.assertEqual(row["publish_status"], PUBLISH_FAILED)

    def test_path_traversal_rejected(self):
        for bad in [
            "communication/outbox/../../README.md",
            "/etc/passwd",
            "communication/inbox/x.md",
        ]:
            rid = f"traversal-{abs(hash(bad)) % 10000}"
            _insert_chain(self.db, rid, bad)
            pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
            try:
                pub.publish_pending()
            finally:
                pub.close()
            row = _get_chain(self.db, rid)
            self.assertEqual(row["publish_status"], PUBLISH_FAILED, bad)


# ============================================================================
# Foreign staged files → abort
# ============================================================================


class TestForeignStagedFileAbort(unittest.TestCase):
    """Foreign *staged* (already-in-index) files lead to a safe error."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, _ = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_foreign_staged_file_aborts(self):
        outbox_rel = "communication/outbox/target2.md"
        (self.repo / outbox_rel).write_text("target\n")
        _insert_chain(self.db, "r-staged", outbox_rel)

        # Stage a foreign file before the publisher runs
        foreign = self.repo / "communication" / "outbox" / "foreign2.md"
        foreign.write_text("STAGED BAD\n")
        _git(["add", "--", "communication/outbox/foreign2.md"], self.repo)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            self.assertEqual(pub.publish_pending(), 0)
        finally:
            pub.close()

        row = _get_chain(self.db, "r-staged")
        self.assertEqual(row["publish_status"], PUBLISH_FAILED)
        # foreign2.md must not be in HEAD (only init commit there)
        commits = _git(["show", "--name-only", "--pretty=format:", "HEAD"], self.repo).stdout
        self.assertNotIn("foreign2.md", commits)


# ============================================================================
# git diff --check clean  (AC #13)
# ============================================================================


class TestGitDiffCheckClean(unittest.TestCase):
    """AC #13: git diff --check is clean after a successful publish."""

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, _ = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    def test_diff_check_clean(self):
        outbox_rel = "communication/outbox/clean.md"
        (self.repo / outbox_rel).write_text("clean content\n")
        _insert_chain(self.db, "r-clean", outbox_rel)

        pub = OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)
        try:
            pub.publish_pending()
        finally:
            pub.close()

        result = _git(["diff", "--check"], self.repo, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# ============================================================================
# Push-error redaction  (AC #14)
# ============================================================================


class TestPushErrorRedaction(unittest.TestCase):
    """push_last_error and log messages must never contain raw credentials."""

    # Each tuple: (error_text, secret_fragment_that_must_not_survive_redaction)
    _CASES = [
        # keyword=value token
        (
            "fatal: remote: token=ghp_abcXYZ123456789abcdef access denied",
            "ghp_abcXYZ123456789abcdef",
        ),
        # HTTPS URL with embedded credential (e.g. x-access-token:ghs_...)
        (
            "fatal: repository 'https://x-access-token:ghs_SomeSecret12345@github.com/org/repo.git/' not found",
            "ghs_SomeSecret12345",
        ),
        # OpenAI-style sk- key
        (
            "push rejected: sk-abcdefghijklmnopqrstuvwxyz1234 is invalid",
            "sk-abcdefghijklmnopqrstuvwxyz1234",
        ),
        # OAuth bearer token (older git that does not mask the value)
        (
            "Authorization: Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz12 push rejected",
            "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz12",
        ),
    ]

    def setUp(self):
        self.ctx = tempfile.TemporaryDirectory()
        tmp = Path(self.ctx.name)
        self.repo, _ = _make_repo_with_remote(tmp)
        self.db = str(tmp / "state.db")
        self.cfg = _legacy_config(self.repo)

    def tearDown(self):
        self.ctx.cleanup()

    # ---- helpers --------------------------------------------------------

    def _make_chain_pub(self) -> OperationChainOutboxPublisher:
        return OperationChainOutboxPublisher(mailbox_config=self.cfg, db_path=self.db)

    def _make_dummy_record(self, record_id: str, outbox_rel: str) -> "PublishRecord":
        return PublishRecord(
            record_id=record_id,
            outbox_file=outbox_rel,
            publish_status=PUBLISH_WRITTEN,
            publish_commit_sha=None,
            publish_retry_count=0,
            content_fn=None,
        )

    def _check_no_secret(self, text: str | None, secret_fragment: str, label: str) -> None:
        if text is None:
            return
        self.assertNotIn(
            secret_fragment,
            text,
            f"{label} must not contain raw secret fragment '{secret_fragment}'",
        )

    # ---- DB persistence tests -------------------------------------------

    def test_on_error_redacts_credential_in_db(self):
        """publish_last_error must not contain raw credentials for any error shape."""
        import logging as _logging
        from unittest.mock import patch

        for i, (error_text, secret) in enumerate(self._CASES):
            record_id = f"r-cred-{i}"
            outbox_rel = f"communication/outbox/cred{i}.md"
            _insert_chain(self.db, record_id, outbox_rel, publish_status=PUBLISH_WRITTEN)

            pub = self._make_chain_pub()
            try:
                record = self._make_dummy_record(record_id, outbox_rel)
                # Silence the logger.error() call so test output stays clean
                with patch.object(
                    _logging.getLogger("modules.outbox_publisher"), "error"
                ):
                    pub._on_error(record, error_text)
            finally:
                pub.close()

            row = _get_chain(self.db, record_id)
            stored = row.get("publish_last_error", "") or ""
            with self.subTest(case=i, secret=secret):
                self._check_no_secret(stored, secret, "DB publish_last_error")
                self.assertIn("[REDACTED]", stored, "Redacted placeholder absent from DB")

    # ---- Log message tests ----------------------------------------------

    def test_on_error_redacts_credential_in_log(self):
        """logger.error() must not emit raw credentials for any push error shape."""
        import logging as _logging
        from unittest.mock import patch

        for i, (error_text, secret) in enumerate(self._CASES):
            record_id = f"r-log-{i}"
            outbox_rel = f"communication/outbox/log{i}.md"
            _insert_chain(self.db, record_id, outbox_rel, publish_status=PUBLISH_WRITTEN)

            pub = self._make_chain_pub()
            try:
                record = self._make_dummy_record(record_id, outbox_rel)
                with patch.object(
                    _logging.getLogger("modules.outbox_publisher"), "error"
                ) as mock_log:
                    pub._on_error(record, error_text)
                    self.assertTrue(mock_log.called)
                    log_call = mock_log.call_args
                    fmt = log_call.args[0] % log_call.args[1:]
                    with self.subTest(case=i, secret=secret):
                        self._check_no_secret(fmt, secret, "log message")
                        self.assertIn("[REDACTED]", fmt, "Redacted placeholder absent from log")
            finally:
                pub.close()

    def test_git_push_log_warning_is_generic(self):
        """git_push() WARNING must be a generic message – no raw stderr, no secrets, no URLs."""
        import logging as _logging
        from unittest.mock import patch, MagicMock

        from modules import mailbox as _mailbox

        # Include both credential cases and plain remote-URL cases.
        all_cases = self._CASES + [
            (
                "remote: https://github.com/org/private-repo.git access denied",
                "https://github.com/org/private-repo.git",
            ),
            (
                "fatal: unable to access 'ssh://git@github.com/org/private.git': could not resolve host",
                "ssh://git@github.com/org/private.git",
            ),
            (
                "ERROR: Repository not found: git@github.com:org/private-repo.git",
                "git@github.com:org/private-repo.git",
            ),
        ]

        for error_text, fragment in all_cases:
            fake_result = MagicMock()
            fake_result.returncode = 1
            fake_result.stderr = error_text

            with patch.object(
                _logging.getLogger("modules.mailbox"), "warning"
            ) as mock_warn, patch("modules.mailbox._git", return_value=fake_result):
                _mailbox.git_push(self.repo)

            self.assertTrue(mock_warn.called, "git_push should call logger.warning on failure")
            log_call = mock_warn.call_args
            fmt = log_call.args[0] % log_call.args[1:]
            with self.subTest(fragment=fragment):
                # WARNING must not contain any raw stderr fragment at all.
                self._check_no_secret(fmt, fragment, "git_push warning log")
                # The warning should contain the exit code but nothing from stderr.
                self.assertIn("exit", fmt, "WARNING should mention exit code")

    def test_git_push_debug_log_redacts_remote_urls(self):
        """git_push() DEBUG log must replace all remote URL forms with [REMOTE]."""
        import logging as _logging
        from unittest.mock import patch, MagicMock

        from modules import mailbox as _mailbox

        remote_url_cases = [
            (
                "remote: https://github.com/org/private-repo.git not found",
                "https://github.com/org/private-repo.git",
            ),
            (
                "fatal: ssh://git@github.com/org/private.git permission denied",
                "ssh://git@github.com/org/private.git",
            ),
            (
                "ERROR: git@github.com:org/repo.git: access denied",
                "git@github.com:org/repo.git",
            ),
        ]

        for error_text, fragment in remote_url_cases:
            fake_result = MagicMock()
            fake_result.returncode = 1
            fake_result.stderr = error_text

            with patch.object(
                _logging.getLogger("modules.mailbox"), "debug"
            ) as mock_debug, patch("modules.mailbox._git", return_value=fake_result):
                _mailbox.git_push(self.repo)

            self.assertTrue(mock_debug.called, "git_push should call logger.debug with redacted detail")
            log_call = mock_debug.call_args
            fmt = log_call.args[0] % log_call.args[1:]
            with self.subTest(fragment=fragment):
                self._check_no_secret(fmt, fragment, "git_push debug log")
                self.assertIn("[RE", fmt, "Redacted/Remote placeholder absent from debug log")


if __name__ == "__main__":
    unittest.main()
