"""Tests for modules/operation_chain_publisher.py.

Covers the persistent, idempotent publisher that commits and pushes
operation-chain outbox result files that OperationChainService has already
written to disk (see modules/operation_chain_service.py::_write_outbox_result).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.mailbox import MailboxProcessor, git_is_clean
from modules.operation_chain_publisher import (
    PUBLISH_FAILED,
    PUBLISH_PENDING,
    PUBLISH_PUSHED,
    OperationChainPublisher,
)
from modules.run_store import init_db


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], path)
    _run_git(["config", "user.email", "test@test.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("# test\n")
    (path / "outbox").mkdir(parents=True)
    (path / "inbox").mkdir(parents=True)
    _run_git(["add", "-A"], path)
    _run_git(["commit", "-m", "init"], path)


def _make_repo_with_remote(tmp: Path) -> tuple[Path, Path]:
    """Create a bare "remote" repo and a working clone pushed to it."""
    remote = tmp / "remote.git"
    _run_git(["init", "--bare", str(remote)], tmp)
    repo = tmp / "repo"
    _make_repo(repo)
    _run_git(["remote", "add", "origin", str(remote)], repo)
    branch = _run_git(["branch", "--show-current"], repo).stdout.strip()
    _run_git(["push", "-u", "origin", branch], repo)
    return repo, remote


def _insert_chain_row(
    db_path: str,
    request_id: str,
    outbox_rel_path: str,
    publish_status: str = PUBLISH_PENDING,
    publish_commit_sha: str | None = None,
) -> None:
    conn = init_db(db_path)
    conn.execute(
        """
        INSERT INTO operation_chains
            (id, request_id, status, created_at, request_json, updated_at,
             outbox_file, publish_status, publish_commit_sha)
        VALUES (?, ?, 'completed', '2026-01-01T00:00:00Z', '{}', '2026-01-01T00:00:00Z', ?, ?, ?)
        """,
        (request_id, request_id, outbox_rel_path, publish_status, publish_commit_sha),
    )
    conn.commit()
    conn.close()


def _remote_head_content(remote: Path, rel_path: str) -> str:
    result = _run_git(["--git-dir", str(remote), "show", f"HEAD:{rel_path}"], remote.parent)
    return result.stdout


def _commit_count(repo: Path) -> int:
    result = _run_git(["rev-list", "--count", "HEAD"], repo)
    return int(result.stdout.strip())


def _get_row(db_path: str, request_id: str) -> dict:
    conn = init_db(db_path)
    conn.row_factory = None
    cur = conn.execute(
        "SELECT * FROM operation_chains WHERE request_id = ?", (request_id,)
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    conn.close()
    return dict(zip(cols, row))


class PublisherCommitPushTests(unittest.TestCase):
    """End-to-end commit+push against a real, temporary bare remote."""

    def setUp(self):
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)
        self.repo, self.remote = _make_repo_with_remote(self.tmp)
        self.db = str(self.tmp / "state.db")

    def tearDown(self):
        self.tmp_ctx.cleanup()

    def test_commits_and_pushes_pending_result(self):
        outbox_rel = "outbox/x.md"
        (self.repo / outbox_rel).write_text("hello world\n")
        _insert_chain_row(self.db, "r1", outbox_rel)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_row(self.db, "r1")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertTrue(row["publish_commit_sha"])
        self.assertIsNone(row["publish_last_error"])

        # File actually reached the bare remote.
        self.assertEqual(_remote_head_content(self.remote, outbox_rel), "hello world\n")
        # Worktree is clean afterwards (nothing left dangling).
        self.assertTrue(git_is_clean(self.repo))

    def test_push_retry_reuses_same_commit_never_reruns_chain(self):
        outbox_rel = "outbox/y.md"
        (self.repo / outbox_rel).write_text("result body\n")
        _insert_chain_row(self.db, "r2", outbox_rel)

        # Break the remote so the first publish attempt commits but fails to push.
        _run_git(["remote", "set-url", "origin", str(self.tmp / "does-not-exist.git")], self.repo)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            self.assertEqual(pub.publish_pending(), 0)
            row = _get_row(self.db, "r2")
            self.assertEqual(row["publish_status"], PUBLISH_FAILED)
            sha_after_first_attempt = row["publish_commit_sha"]
            self.assertTrue(sha_after_first_attempt)
            self.assertEqual(_commit_count(self.repo), 2)  # init + this one commit only

            # Restore the remote and retry: must push the SAME commit, not
            # create a new one and must not touch the chain/DB result at all.
            _run_git(["remote", "set-url", "origin", str(self.remote)], self.repo)
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_row(self.db, "r2")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], sha_after_first_attempt)
        self.assertEqual(_commit_count(self.repo), 2)  # still only one publish commit
        self.assertEqual(_remote_head_content(self.remote, outbox_rel), "result body\n")

    def test_process_abort_after_commit_before_db_write_is_recovered(self):
        """Simulate a crash between `git commit` succeeding and the DB update.

        The publisher must detect the file is already committed and recover
        the SHA instead of creating a duplicate commit or failing forever.
        """
        outbox_rel = "outbox/z.md"
        (self.repo / outbox_rel).write_text("crash recovery body\n")
        _insert_chain_row(self.db, "r3", outbox_rel, publish_status="committing", publish_commit_sha=None)

        # Manually perform exactly what the publisher would have done, to
        # simulate the commit having already happened before the crash.
        _run_git(["add", "--", outbox_rel], self.repo)
        _run_git(["commit", "--no-verify", "-m", "operation-chain: publish r3"], self.repo)
        real_sha = _run_git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        commits_before = _commit_count(self.repo)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        self.assertEqual(_commit_count(self.repo), commits_before)  # no duplicate commit
        row = _get_row(self.db, "r3")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], real_sha)
        self.assertEqual(_remote_head_content(self.remote, outbox_rel), "crash recovery body\n")

    def test_idempotent_second_run_is_a_noop(self):
        outbox_rel = "outbox/idem.md"
        (self.repo / outbox_rel).write_text("idempotent body\n")
        _insert_chain_row(self.db, "r4", outbox_rel)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            pub.publish_pending()
            sha_first = _get_row(self.db, "r4")["publish_commit_sha"]
            commits_after_first = _commit_count(self.repo)

            # Second poll cycle: nothing should change.
            pub.publish_pending()
        finally:
            pub.close()

        row = _get_row(self.db, "r4")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertEqual(row["publish_commit_sha"], sha_first)
        self.assertEqual(_commit_count(self.repo), commits_after_first)

    def test_foreign_dirty_files_are_never_staged_or_committed(self):
        outbox_rel = "outbox/foreign.md"
        (self.repo / outbox_rel).write_text("only me please\n")
        _insert_chain_row(self.db, "r5", outbox_rel)

        # Unrelated dirty files: a modified tracked file and a new untracked file.
        (self.repo / "README.md").write_text("# test\nlocal edit not to be touched\n")
        (self.repo / "scratch.txt").write_text("untracked scratch file\n")

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            self.assertEqual(pub.publish_pending(), 1)
        finally:
            pub.close()

        row = _get_row(self.db, "r5")
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)

        # The commit must contain only the outbox file.
        show = _run_git(["show", "--name-only", "--pretty=format:", "HEAD"], self.repo)
        changed_files = [line for line in show.stdout.splitlines() if line.strip()]
        self.assertEqual(changed_files, [outbox_rel])

        # Foreign files remain dirty/unstaged, never touched.
        status = _run_git(["status", "--porcelain"], self.repo).stdout
        self.assertIn("README.md", status)
        self.assertIn("scratch.txt", status)
        # And confirm they were never staged (index empty for them): porcelain
        # marks staged changes in column 1; both entries here must show as
        # unstaged (" M" / "??"), not staged ("M "/"A ").
        for line in status.splitlines():
            if "README.md" in line:
                self.assertTrue(line.startswith(" M"), line)
            if "scratch.txt" in line:
                self.assertTrue(line.startswith("??"), line)

    def test_symlink_outbox_file_rejected(self):
        outbox_rel = "outbox/link.md"
        target = self.repo / "outbox" / "real.md"
        target.write_text("real content\n")
        link = self.repo / outbox_rel
        link.symlink_to(target)
        # The symlink itself is untracked; simulate DB pointing at it.
        _insert_chain_row(self.db, "r6", outbox_rel)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        try:
            self.assertEqual(pub.publish_pending(), 0)
        finally:
            pub.close()

        row = _get_row(self.db, "r6")
        self.assertEqual(row["publish_status"], PUBLISH_FAILED)
        self.assertIn("symlink", (row["publish_last_error"] or "").lower())
        # Nothing was staged or committed.
        self.assertEqual(_commit_count(self.repo), 1)
        status = _run_git(["status", "--porcelain"], self.repo).stdout
        self.assertNotIn("A ", status[:2])

    def test_path_traversal_outbox_file_rejected(self):
        # Ids come from the loop index, not from hash(): Python randomises
        # string hashes per process, and the same pattern in
        # test_outbox_publisher.py did collide on CI.
        for index, malicious in enumerate((
            "outbox/../../etc/passwd",
            "/etc/passwd",
            "inbox/x.md",
            "outbox/../outbox/y.md/../../../secrets.md",
        )):
            with self.subTest(outbox_file=malicious):
                request_id = f"traversal-{index}"
                _insert_chain_row(self.db, request_id, malicious)
                pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
                try:
                    self.assertEqual(pub.publish_pending(), 0)
                finally:
                    pub.close()
                row = _get_row(self.db, request_id)
                self.assertEqual(row["publish_status"], PUBLISH_FAILED)
        self.assertEqual(_commit_count(self.repo), 1)  # nothing ever committed

    def test_normal_mailbox_poll_publishes_then_clean_worktree_guard_passes(self):
        """Mirrors mailbox_processor.main(): publish_pending() runs before the
        clean-worktree guard, and after a successful publish the guard sees a
        clean tree and allows normal inbox processing to proceed."""
        outbox_rel = "outbox/poll.md"
        (self.repo / outbox_rel).write_text("poll cycle body\n")
        _insert_chain_row(self.db, "r7", outbox_rel)

        pub = OperationChainPublisher(db_path=self.db, repo_root=str(self.repo))
        processor = MailboxProcessor(repo_path=self.repo, db_path=self.db, dry_run=False)
        try:
            pub.publish_pending()
            self.assertTrue(processor.ensure_clean_worktree())
        finally:
            pub.close()
            processor.close()


if __name__ == "__main__":
    unittest.main()
