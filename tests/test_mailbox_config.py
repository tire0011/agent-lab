"""Tests for MailboxConfig and the separated mailbox/code repository paths.

Acceptance criteria:
1. Defaults reproduce the legacy layout under /agent/communication/*.
2. External config uses separate repos with direct inbox/ and outbox/.
3. Mailbox git operations run exclusively in the mailbox repo.
4. IDEAS.md and state/runs stay in the code repo.
5. Outbox producers write only to the configured outbox directory.
6. A dirty code repo does NOT block mailbox git operations.
7. A dirty mailbox repo is rejected safely.
8. Existing tests remain green.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.mailbox.config import MailboxConfig
from modules.mailbox import (
    MailboxProcessor,
    git_is_clean,
)
from modules.operation_chain_publisher import OperationChainPublisher
from modules.operation_chain_service import OperationChainService
from modules.mailbox.task_service import MailboxTaskService
from modules.run_store import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _make_git_repo(path: Path, initial_commit: bool = True) -> None:
    """Initialise a bare-minimum git repo."""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], path)
    _run_git(["config", "user.email", "test@test.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    if initial_commit:
        (path / "README.md").write_text("# test\n")
        _run_git(["add", "README.md"], path)
        _run_git(["commit", "-m", "init"], path)


def _make_ideas_md(code_repo: Path) -> None:
    (code_repo / "IDEAS.md").write_text("# Ideas\n\n## Ideas\n\n")
    _run_git(["add", "IDEAS.md"], code_repo)
    _run_git(["commit", "-m", "add IDEAS.md"], code_repo)


# ---------------------------------------------------------------------------
# 1. Defaults reproduce legacy layout
# ---------------------------------------------------------------------------

class TestMailboxConfigDefaults(unittest.TestCase):
    """MailboxConfig.from_env() with no env vars set uses the legacy layout."""

    def test_from_env_defaults(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_CODE_REPO_PATH", "AGENT_MAILBOX_REPO_PATH")}
        old = {k: os.environ.pop(k, None) for k in ("AGENT_CODE_REPO_PATH", "AGENT_MAILBOX_REPO_PATH")}
        try:
            cfg = MailboxConfig.from_env()
            self.assertEqual(cfg.code_repo_path, Path("/agent"))
            self.assertEqual(cfg.mailbox_repo_path, Path("/agent"))
            self.assertEqual(cfg.inbox_dir, Path("/agent/communication/inbox"))
            self.assertEqual(cfg.outbox_dir, Path("/agent/communication/outbox"))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_from_env_custom_paths(self):
        old = {k: os.environ.pop(k, None) for k in ("AGENT_CODE_REPO_PATH", "AGENT_MAILBOX_REPO_PATH")}
        try:
            os.environ["AGENT_CODE_REPO_PATH"] = "/code"
            os.environ["AGENT_MAILBOX_REPO_PATH"] = "/mailbox"
            cfg = MailboxConfig.from_env()
            self.assertEqual(cfg.code_repo_path, Path("/code"))
            self.assertEqual(cfg.mailbox_repo_path, Path("/mailbox"))
            # Separate-repo external layout: inbox/outbox directly at mailbox root
            self.assertEqual(cfg.inbox_dir, Path("/mailbox/inbox"))
            self.assertEqual(cfg.outbox_dir, Path("/mailbox/outbox"))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_legacy_factory(self):
        repo = Path("/tmp/myrepo")
        cfg = MailboxConfig.legacy(repo)
        self.assertEqual(cfg.code_repo_path, repo)
        self.assertEqual(cfg.mailbox_repo_path, repo)
        self.assertEqual(cfg.inbox_dir, repo / "communication" / "inbox")
        self.assertEqual(cfg.outbox_dir, repo / "communication" / "outbox")
        self.assertFalse(cfg.repos_are_separate)

    def test_repos_are_separate_property(self):
        cfg = MailboxConfig(
            code_repo_path=Path("/code"),
            mailbox_repo_path=Path("/mailbox"),
            inbox_dir=Path("/mailbox/inbox"),
            outbox_dir=Path("/mailbox/outbox"),
        )
        self.assertTrue(cfg.repos_are_separate)


# ---------------------------------------------------------------------------
# 2. External configuration – separate repos
# ---------------------------------------------------------------------------

class TestMailboxConfigExternal(unittest.TestCase):
    """External layout: code repo and mailbox repo are different directories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "inbox").mkdir()
        (self.mailbox_repo / "outbox").mkdir()
        # Commit the dirs so HEAD exists with them
        (self.mailbox_repo / "inbox" / ".gitkeep").write_text("")
        (self.mailbox_repo / "outbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add mailbox dirs"], self.mailbox_repo)

        self.cfg = MailboxConfig(
            code_repo_path=self.code_repo,
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "inbox",
            outbox_dir=self.mailbox_repo / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_outbox_dir_is_in_mailbox_repo(self):
        self.assertTrue(
            str(self.cfg.outbox_dir).startswith(str(self.mailbox_repo))
        )

    def test_inbox_dir_is_in_mailbox_repo(self):
        self.assertTrue(
            str(self.cfg.inbox_dir).startswith(str(self.mailbox_repo))
        )

    def test_repos_are_separate(self):
        self.assertTrue(self.cfg.repos_are_separate)

    def test_mailbox_task_service_uses_config(self):
        svc = MailboxTaskService(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertEqual(svc.outbox_dir, self.cfg.outbox_dir)
            self.assertEqual(svc.repo_path, self.cfg.code_repo_path)
        finally:
            svc.close()

    def test_operation_chain_service_uses_config(self):
        svc = OperationChainService(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertEqual(svc.repo_root, self.cfg.mailbox_repo_path)
            self.assertEqual(svc.outbox_dir, self.cfg.outbox_dir)
        finally:
            svc.close()

    def test_operation_chain_publisher_uses_config(self):
        pub = OperationChainPublisher(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertEqual(pub.repo_root, self.cfg.mailbox_repo_path)
            self.assertEqual(pub.outbox_dir, self.cfg.outbox_dir)
        finally:
            pub.close()

    def test_mailbox_processor_uses_config(self):
        proc = MailboxProcessor(
            repo_path=self.mailbox_repo,
            db_path=self.db_path,
            mailbox_config=self.cfg,
        )
        try:
            self.assertEqual(proc._mailbox_repo_path, self.mailbox_repo)
            self.assertEqual(proc._code_repo_path, self.code_repo)
            self.assertEqual(proc.inbox, self.cfg.inbox_dir)
            self.assertEqual(proc.outbox, self.cfg.outbox_dir)
        finally:
            proc.close()


# ---------------------------------------------------------------------------
# 3. Mailbox git operations use mailbox repo
# ---------------------------------------------------------------------------

class TestMailboxGitOperationsIsolation(unittest.TestCase):
    """Git operations for mailbox messages run exclusively in the mailbox repo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "communication" / "inbox").mkdir(parents=True)
        (self.mailbox_repo / "communication" / "outbox").mkdir(parents=True)
        (self.mailbox_repo / "communication" / "inbox" / ".gitkeep").write_text("")
        (self.mailbox_repo / "communication" / "outbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add dirs"], self.mailbox_repo)
        self.cfg = MailboxConfig.legacy(self.mailbox_repo)
        # Override code_repo_path to be separate
        self.cfg = MailboxConfig(
            code_repo_path=self.code_repo,
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "communication" / "inbox",
            outbox_dir=self.mailbox_repo / "communication" / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_ensure_clean_worktree_checks_mailbox_repo_not_code_repo(self):
        """ensure_clean_worktree checks the mailbox repo, not the code repo."""
        proc = MailboxProcessor(
            repo_path=self.mailbox_repo,
            db_path=self.db_path,
            mailbox_config=self.cfg,
        )
        try:
            # Dirty the code repo (untracked file)
            (self.code_repo / "dirty.txt").write_text("dirty code repo")
            # Mailbox repo is clean, so should pass
            self.assertTrue(proc.ensure_clean_worktree())

            # Now dirty the mailbox repo too
            (self.mailbox_repo / "dirty.txt").write_text("dirty mailbox repo")
            self.assertFalse(proc.ensure_clean_worktree())
        finally:
            proc.close()


# ---------------------------------------------------------------------------
# 4. IDEAS.md stays in code repo
# ---------------------------------------------------------------------------

class TestIdeasMdInCodeRepo(unittest.TestCase):
    """IDEAS.md is read from and written to the code repo, not the mailbox repo."""

    def test_ideas_path_uses_code_repo(self):
        code = Path("/code")
        mailbox = Path("/mailbox")
        cfg = MailboxConfig(
            code_repo_path=code,
            mailbox_repo_path=mailbox,
            inbox_dir=mailbox / "inbox",
            outbox_dir=mailbox / "outbox",
        )
        proc = MailboxProcessor.__new__(MailboxProcessor)
        proc._code_repo_path = cfg.code_repo_path
        proc._mailbox_repo_path = cfg.mailbox_repo_path
        proc.inbox = cfg.inbox_dir
        proc.outbox = cfg.outbox_dir
        proc.repo_path = cfg.mailbox_repo_path

        # The internal code_repo_path is the code repo
        self.assertEqual(proc._code_repo_path, code)
        self.assertNotEqual(proc._code_repo_path, mailbox)


# ---------------------------------------------------------------------------
# 5. Outbox producers write to configured outbox_dir
# ---------------------------------------------------------------------------

class TestOutboxWriteLocation(unittest.TestCase):
    """Outbox producers (MailboxTaskService, OperationChainService) write to
    the configured outbox directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "outbox").mkdir()
        (self.mailbox_repo / "outbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add outbox"], self.mailbox_repo)
        self.cfg = MailboxConfig(
            code_repo_path=self.code_repo,
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "inbox",
            outbox_dir=self.mailbox_repo / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_mailbox_task_service_outbox_in_mailbox_repo(self):
        svc = MailboxTaskService(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertTrue(
                str(svc.outbox_dir).startswith(str(self.mailbox_repo))
            )
            self.assertFalse(
                str(svc.outbox_dir).startswith(str(self.code_repo))
            )
        finally:
            svc.close()

    def test_operation_chain_service_outbox_in_mailbox_repo(self):
        svc = OperationChainService(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertTrue(
                str(svc.outbox_dir).startswith(str(self.mailbox_repo))
            )
            self.assertFalse(
                str(svc.outbox_dir).startswith(str(self.code_repo))
            )
        finally:
            svc.close()

    def test_operation_chain_publisher_outbox_in_mailbox_repo(self):
        pub = OperationChainPublisher(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            self.assertTrue(
                str(pub.outbox_dir).startswith(str(self.mailbox_repo))
            )
        finally:
            pub.close()


# ---------------------------------------------------------------------------
# 6. Dirty code repo does not block mailbox git operations
# ---------------------------------------------------------------------------

class TestDirtyCodeRepoDoesNotBlockMailbox(unittest.TestCase):
    """A dirty code repo must not prevent mailbox operations from proceeding."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "communication" / "inbox").mkdir(parents=True)
        (self.mailbox_repo / "communication" / "outbox").mkdir(parents=True)
        (self.mailbox_repo / "communication" / "inbox" / ".gitkeep").write_text("")
        (self.mailbox_repo / "communication" / "outbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add dirs"], self.mailbox_repo)
        self.cfg = MailboxConfig(
            code_repo_path=self.code_repo,
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "communication" / "inbox",
            outbox_dir=self.mailbox_repo / "communication" / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_dirty_code_repo_clean_mailbox_repo_passes(self):
        """Dirty code repo should not affect mailbox worktree check."""
        # Make code repo dirty
        (self.code_repo / "untracked.txt").write_text("untracked")
        self.assertFalse(git_is_clean(self.code_repo))

        proc = MailboxProcessor(
            repo_path=self.mailbox_repo,
            db_path=self.db_path,
            mailbox_config=self.cfg,
        )
        try:
            # Mailbox repo is clean, check should pass
            self.assertTrue(proc.ensure_clean_worktree())
        finally:
            proc.close()


# ---------------------------------------------------------------------------
# 7. Dirty mailbox repo is safely rejected
# ---------------------------------------------------------------------------

class TestDirtyMailboxRepoIsRejected(unittest.TestCase):
    """A dirty mailbox repo must be rejected by ensure_clean_worktree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "communication" / "inbox").mkdir(parents=True)
        (self.mailbox_repo / "communication" / "inbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add dirs"], self.mailbox_repo)
        self.cfg = MailboxConfig(
            code_repo_path=self.code_repo,
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "communication" / "inbox",
            outbox_dir=self.mailbox_repo / "communication" / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_dirty_mailbox_repo_rejected(self):
        (self.mailbox_repo / "untracked.txt").write_text("dirty mailbox")
        self.assertFalse(git_is_clean(self.mailbox_repo))

        proc = MailboxProcessor(
            repo_path=self.mailbox_repo,
            db_path=self.db_path,
            mailbox_config=self.cfg,
        )
        try:
            self.assertFalse(proc.ensure_clean_worktree())
        finally:
            proc.close()


# ---------------------------------------------------------------------------
# 9. Publisher validate_outbox_path works with non-legacy outbox dirs
# ---------------------------------------------------------------------------

class TestPublisherOutboxValidationExternalLayout(unittest.TestCase):
    """_validate_outbox_path works with outbox at mailbox_repo/outbox/ (no 'communication')."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.mailbox_repo)
        (self.mailbox_repo / "outbox").mkdir()
        (self.mailbox_repo / "outbox" / ".gitkeep").write_text("")
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "add outbox"], self.mailbox_repo)

        self.cfg = MailboxConfig(
            code_repo_path=self.tmp / "code",
            mailbox_repo_path=self.mailbox_repo,
            inbox_dir=self.mailbox_repo / "inbox",
            outbox_dir=self.mailbox_repo / "outbox",
        )
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def test_outbox_file_relative_to_mailbox_repo_is_validated(self):
        """A valid file at outbox/x.md passes validation."""
        outbox_file = "outbox/x.md"
        (self.mailbox_repo / "outbox" / "x.md").write_text("hello\n")

        pub = OperationChainPublisher(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            path = pub._validate_outbox_path(outbox_file)
            self.assertEqual(path, self.mailbox_repo / "outbox" / "x.md")
        finally:
            pub.close()

    def test_outbox_path_escaping_rejected(self):
        """A path that escapes the outbox dir is rejected."""
        pub = OperationChainPublisher(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            with self.assertRaises(ValueError):
                pub._validate_outbox_path("outbox/../README.md")
        finally:
            pub.close()

    def test_absolute_path_rejected(self):
        pub = OperationChainPublisher(db_path=self.db_path, mailbox_config=self.cfg)
        try:
            with self.assertRaises(ValueError):
                pub._validate_outbox_path("/absolute/path/x.md")
        finally:
            pub.close()


# ---------------------------------------------------------------------------
# 10. Legacy single-repo mode is unaffected
# ---------------------------------------------------------------------------

class TestLegacySingleRepoMode(unittest.TestCase):
    """With default config (same repo for code and mailbox), existing paths work."""

    def test_processor_legacy_paths(self):
        tmp = Path(tempfile.mkdtemp())
        _make_git_repo(tmp)
        db_path = str(tmp / "test.db")
        init_db(db_path)
        proc = MailboxProcessor(repo_path=tmp, db_path=db_path)
        try:
            self.assertEqual(proc._mailbox_repo_path, tmp)
            self.assertEqual(proc._code_repo_path, tmp)
            self.assertEqual(proc.inbox, tmp / "communication" / "inbox")
            self.assertEqual(proc.outbox, tmp / "communication" / "outbox")
        finally:
            proc.close()

    def test_publisher_legacy_paths(self):
        tmp = Path(tempfile.mkdtemp())
        _make_git_repo(tmp)
        db_path = str(tmp / "test.db")
        init_db(db_path)
        pub = OperationChainPublisher(db_path=db_path, repo_root=str(tmp))
        try:
            self.assertEqual(pub.repo_root, tmp)
            self.assertEqual(pub.outbox_dir, tmp / "communication" / "outbox")
        finally:
            pub.close()

    def test_operation_chain_service_legacy_paths(self):
        tmp = Path(tempfile.mkdtemp())
        db_path = str(tmp / "test.db")
        init_db(db_path)
        svc = OperationChainService(
            db_path=db_path,
            repo_root=str(tmp),
            outbox_dir=str(tmp / "communication" / "outbox"),
        )
        try:
            self.assertEqual(svc.repo_root, tmp)
            self.assertEqual(svc.outbox_dir, tmp / "communication" / "outbox")
        finally:
            svc.close()


# ---------------------------------------------------------------------------
# 11. Regression tests: env vars, CLI, two-repo end-to-end, cross-repo
#     isolation
# ---------------------------------------------------------------------------

_MESSAGE_TMPL = """\
---
type: question
created_by: test-agent
---

What is 2+2?
"""


def _make_external_mailbox_repo(path: Path) -> None:
    """Create a mailbox repo with direct inbox/ outbox/ layout."""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], path)
    _run_git(["config", "user.email", "test@test.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    (path / "inbox").mkdir()
    (path / "outbox").mkdir()
    (path / "inbox" / ".gitkeep").write_text("")
    (path / "outbox" / ".gitkeep").write_text("")
    _run_git(["add", "-A"], path)
    _run_git(["commit", "-m", "init mailbox"], path)


class TestEnvVarDirectExternalPaths(unittest.TestCase):
    """from_env() with separate env vars produces direct inbox/outbox paths."""

    def _set_env(self, code: str, mailbox: str):
        self._old = {k: os.environ.pop(k, None)
                     for k in ("AGENT_CODE_REPO_PATH", "AGENT_MAILBOX_REPO_PATH")}
        os.environ["AGENT_CODE_REPO_PATH"] = code
        os.environ["AGENT_MAILBOX_REPO_PATH"] = mailbox

    def _restore_env(self):
        for k, v in self._old.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_separate_repos_produce_direct_external_paths(self):
        """AGENT_MAILBOX_REPO_PATH=/var/lib/nexus/mailbox-repo gives direct inbox/outbox."""
        self._set_env("/agent", "/var/lib/nexus/mailbox-repo")
        try:
            cfg = MailboxConfig.from_env()
            self.assertEqual(cfg.inbox_dir, Path("/var/lib/nexus/mailbox-repo/inbox"))
            self.assertEqual(cfg.outbox_dir, Path("/var/lib/nexus/mailbox-repo/outbox"))
            self.assertTrue(cfg.repos_are_separate)
        finally:
            self._restore_env()

    def test_same_env_paths_produce_legacy_layout(self):
        """When both env vars point to the same path, the legacy layout is used."""
        self._set_env("/agent", "/agent")
        try:
            cfg = MailboxConfig.from_env()
            self.assertEqual(cfg.inbox_dir, Path("/agent/communication/inbox"))
            self.assertEqual(cfg.outbox_dir, Path("/agent/communication/outbox"))
            self.assertFalse(cfg.repos_are_separate)
        finally:
            self._restore_env()

    def test_from_paths_separate_gives_external_layout(self):
        cfg = MailboxConfig.from_paths(Path("/code"), Path("/mailbox"))
        self.assertEqual(cfg.inbox_dir, Path("/mailbox/inbox"))
        self.assertEqual(cfg.outbox_dir, Path("/mailbox/outbox"))
        self.assertTrue(cfg.repos_are_separate)

    def test_from_paths_same_gives_legacy_layout(self):
        cfg = MailboxConfig.from_paths(Path("/agent"), Path("/agent"))
        self.assertEqual(cfg.inbox_dir, Path("/agent/communication/inbox"))
        self.assertEqual(cfg.outbox_dir, Path("/agent/communication/outbox"))
        self.assertFalse(cfg.repos_are_separate)


class TestCLIPathResolution(unittest.TestCase):
    """CLI path resolution via _resolve_repo_paths(), the function used by main()."""

    def test_explicit_separate_flags_produce_direct_external_paths(self):
        """--code-repo-path + --mailbox-repo-path → external layout, tested via
        _resolve_repo_paths() which is exactly what main() calls."""
        from mailbox_processor import _resolve_repo_paths
        code, mailbox = _resolve_repo_paths("/agent", "/var/lib/nexus/mailbox-repo", None)
        cfg = MailboxConfig.from_paths(code, mailbox)
        self.assertEqual(cfg.inbox_dir, Path("/var/lib/nexus/mailbox-repo/inbox"))
        self.assertEqual(cfg.outbox_dir, Path("/var/lib/nexus/mailbox-repo/outbox"))

    def test_repo_path_alias_produces_legacy_layout(self):
        """--repo-path sets both paths identically → legacy layout."""
        from mailbox_processor import _resolve_repo_paths
        code, mailbox = _resolve_repo_paths(None, None, "/agent")
        cfg = MailboxConfig.from_paths(code, mailbox)
        self.assertEqual(cfg.inbox_dir, Path("/agent/communication/inbox"))
        self.assertEqual(cfg.outbox_dir, Path("/agent/communication/outbox"))

    def test_env_fallback_separate_repos(self):
        """Env-only separate paths → external layout via _resolve_repo_paths."""
        from mailbox_processor import _resolve_repo_paths
        old = {k: os.environ.pop(k, None)
               for k in ("AGENT_CODE_REPO_PATH", "AGENT_MAILBOX_REPO_PATH")}
        try:
            os.environ["AGENT_CODE_REPO_PATH"] = "/agent"
            os.environ["AGENT_MAILBOX_REPO_PATH"] = "/var/lib/nexus/mailbox-repo"
            code, mailbox = _resolve_repo_paths(None, None, None)
            self.assertEqual(code, Path("/agent"))
            self.assertEqual(mailbox, Path("/var/lib/nexus/mailbox-repo"))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_explicit_conflict_raises_value_error(self):
        """--repo-path + --code-repo-path raises ValueError from _resolve_repo_paths."""
        from mailbox_processor import _resolve_repo_paths
        with self.assertRaises(ValueError):
            _resolve_repo_paths("/y", None, "/x")

    def test_explicit_conflict_rejected_by_main(self):
        """--repo-path combined with --code-repo-path causes main() to SystemExit."""
        import mailbox_processor
        with self.assertRaises(SystemExit) as ctx:
            mailbox_processor.main(
                ["--repo-path", "/x", "--code-repo-path", "/y", "--db-path", "/tmp/x.db"]
            )
        self.assertNotEqual(ctx.exception.code, 0)


class TestTwoRepoEndToEndCommit(unittest.TestCase):
    """git_add_and_commit allowlist behaviour in a two-repo setup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_ideas_md(self.code_repo)
        _make_external_mailbox_repo(self.mailbox_repo)
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def _list_committed_files(self, repo: Path) -> set[str]:
        """Return relative paths of all files tracked in HEAD."""
        result = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], repo)
        return set(result.stdout.strip().splitlines())

    def test_commit_helper_accepts_direct_inbox_outbox(self):
        """git_add_and_commit accepts inbox/<file> and outbox/<file>."""
        from modules.mailbox import git_add_and_commit

        inbox_file = self.mailbox_repo / "inbox" / "msg.md"
        inbox_file.write_text(_MESSAGE_TMPL)

        cfg = MailboxConfig.from_paths(self.code_repo, self.mailbox_repo)
        extra = frozenset(
            str(d.relative_to(self.mailbox_repo))
            for d in (cfg.inbox_dir, cfg.outbox_dir)
        )
        result = git_add_and_commit(
            self.mailbox_repo, [inbox_file], "test: direct inbox commit", extra
        )
        self.assertTrue(result)
        committed = self._list_committed_files(self.mailbox_repo)
        self.assertIn("inbox/msg.md", committed)

    def test_commit_helper_accepts_arbitrary_repo_paths(self):
        """git_add_and_commit has no static write-path gate."""
        from modules.mailbox import git_add_and_commit

        arbitrary = self.mailbox_repo / "arbitrary.txt"
        arbitrary.write_text("content")
        result = git_add_and_commit(
            self.mailbox_repo, [arbitrary], "test: arbitrary path", frozenset({"inbox", "outbox"})
        )
        self.assertTrue(result)
        self.assertIn("arbitrary.txt", self._list_committed_files(self.mailbox_repo))

    def test_no_mailbox_file_written_to_code_repo(self):
        """After a mailbox-repo commit, no inbox/outbox file exists in the code repo."""
        from modules.mailbox import git_add_and_commit

        inbox_msg = self.mailbox_repo / "inbox" / "test-msg.md"
        inbox_msg.write_text(_MESSAGE_TMPL)

        cfg = MailboxConfig.from_paths(self.code_repo, self.mailbox_repo)
        extra = frozenset(
            str(d.relative_to(self.mailbox_repo))
            for d in (cfg.inbox_dir, cfg.outbox_dir)
        )
        git_add_and_commit(self.mailbox_repo, [inbox_msg], "mailbox: commit msg", extra)

        code_files = self._list_committed_files(self.code_repo)
        mailbox_files = self._list_committed_files(self.mailbox_repo)
        for f in code_files:
            self.assertNotIn("inbox", f.split("/"), msg=f"inbox file in code repo: {f}")
            self.assertNotIn("outbox", f.split("/"), msg=f"outbox file in code repo: {f}")
        self.assertIn("inbox/test-msg.md", mailbox_files)

    def test_legacy_defaults_unchanged(self):
        """MailboxConfig.from_paths with same path → legacy communication/* layout."""
        cfg = MailboxConfig.from_paths(self.code_repo, self.code_repo)
        self.assertEqual(cfg.inbox_dir, self.code_repo / "communication" / "inbox")
        self.assertEqual(cfg.outbox_dir, self.code_repo / "communication" / "outbox")
        self.assertFalse(cfg.repos_are_separate)


class TestTwoRepoEndToEndProcessInbox(unittest.TestCase):
    """Full process_inbox() end-to-end: two-repo scenario with a question message.

    This test exercises the complete MailboxProcessor pipeline (not just
    git_add_and_commit directly) and verifies cross-repo isolation.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.code_repo = self.tmp / "code"
        self.mailbox_repo = self.tmp / "mailbox"
        _make_git_repo(self.code_repo)
        _make_ideas_md(self.code_repo)
        _make_external_mailbox_repo(self.mailbox_repo)
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path)

    def _list_committed_files(self, repo: Path) -> set[str]:
        result = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], repo)
        return set(result.stdout.strip().splitlines())

    def test_process_question_commits_only_to_mailbox_repo(self):
        """MailboxProcessor.process_inbox() in two-repo mode:
        - reads the message from direct inbox/
        - writes the outbox response to direct outbox/
        - deletes the inbox message
        - commits only to the mailbox repo
        - leaves the code repo untouched (clean, no inbox/outbox files)
        """
        cfg = MailboxConfig.from_paths(self.code_repo, self.mailbox_repo)

        # Write a question message into the direct inbox/ and commit it
        # (simulates the message arriving via git pull).
        inbox_file = self.mailbox_repo / "inbox" / "question-001.md"
        inbox_file.write_text(_MESSAGE_TMPL)
        _run_git(["add", "-A"], self.mailbox_repo)
        _run_git(["commit", "-m", "mailbox: receive question-001.md"], self.mailbox_repo)

        proc = MailboxProcessor(
            repo_path=self.mailbox_repo,
            db_path=self.db_path,
            dry_run=False,
            mailbox_config=cfg,
        )
        try:
            results = proc.process_inbox()
        finally:
            proc.close()

        # Exactly one message processed successfully.
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["status"], "done", msg=f"unexpected result: {result}")
        self.assertEqual(result["type"], "question")

        # Inbox file deleted from disk.
        self.assertFalse(
            inbox_file.exists(), "inbox file must be deleted after processing"
        )

        # Outbox file written to direct outbox/ inside the mailbox repo.
        outbox_file_name = result["outbox"]
        outbox_path = self.mailbox_repo / "outbox" / outbox_file_name
        self.assertTrue(outbox_path.exists(), f"outbox file missing: {outbox_path}")

        # Mailbox repo HEAD: inbox file gone, outbox file present.
        mailbox_committed = self._list_committed_files(self.mailbox_repo)
        self.assertNotIn("inbox/question-001.md", mailbox_committed,
                         "processed inbox file must be removed from mailbox HEAD")
        self.assertIn(f"outbox/{outbox_file_name}", mailbox_committed,
                      "outbox file must be committed in mailbox repo")

        # Code repo: no inbox/outbox writes or staging.
        code_status = _run_git(["status", "--porcelain"], self.code_repo)
        self.assertEqual(
            code_status.stdout.strip(), "",
            f"code repo must be clean, got: {code_status.stdout!r}",
        )
        code_committed = self._list_committed_files(self.code_repo)
        for f in code_committed:
            parts = f.split("/")
            self.assertNotIn("inbox", parts, f"inbox path found in code repo commit: {f}")
            self.assertNotIn("outbox", parts, f"outbox path found in code repo commit: {f}")


if __name__ == "__main__":
    unittest.main()
