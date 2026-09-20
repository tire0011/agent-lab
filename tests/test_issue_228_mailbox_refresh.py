"""#228: refreshing the deployment needs no arbitrary shell.

A refresh used to be sent as `type: command`, i.e. a shell script - the widest
possible interface for one of the narrowest operations the VM performs. These
tests pin the narrow one: what the message can say, what it cannot say, and
that a refusal is reported rather than raised.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox.refresh import (
    ALLOWED_SERVICES,
    InvalidBranchError,
    process_refresh,
    validate_branch,
)


def _message(**meta):
    return SimpleNamespace(meta=meta, body="ignored")


def _result(**overrides):
    defaults = dict(
        success=True,
        branch="main",
        previous_head="aaaaaaa",
        new_head="bbbbbbb",
        updated=True,
        services={"agent-api": "restarted"},
        steps=("inspect", "update"),
        error=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class BranchValidationTests(unittest.TestCase):
    def test_ordinary_branch_names_are_accepted(self):
        for name in (
            "main",
            "claude/issue-228-mailbox-refresh-type",
            "release/1.2.3",
            "fix_222",
        ):
            with self.subTest(name=name):
                self.assertEqual(validate_branch(name), name)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(validate_branch("  main\n"), "main")

    def test_empty_and_missing_are_refused(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(InvalidBranchError):
                    validate_branch(value)

    def test_shell_metacharacters_are_refused(self):
        # Nothing here reaches a shell - the refresh runs in-process - but an
        # allowlist that would let these through is one refactor away from
        # mattering.
        for name in (
            "main; rm -rf /",
            "main && curl evil",
            "main | tee /etc/passwd",
            "$(whoami)",
            "`id`",
            "main\nrm -rf /",
            "main with spaces",
        ):
            with self.subTest(name=name):
                with self.assertRaises(InvalidBranchError):
                    validate_branch(name)

    def test_path_traversal_is_refused(self):
        for name in ("../main", "a/../../etc", ".."):
            with self.subTest(name=name):
                with self.assertRaises(InvalidBranchError):
                    validate_branch(name)

    def test_a_name_that_reads_as_an_option_is_refused(self):
        for name in ("-main", "--upload-pack=evil"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidBranchError):
                    validate_branch(name)

    def test_malformed_slashes_and_git_reserved_shapes_are_refused(self):
        for name in ("/main", "main/", "a//b", "main.lock"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidBranchError):
                    validate_branch(name)

    def test_an_overlong_name_is_refused(self):
        with self.assertRaises(InvalidBranchError):
            validate_branch("a" * 201)


class ProcessRefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.calls = []

    def _refresher(self, result=None, raises=None):
        def run(branch, repo_root):
            self.calls.append((branch, repo_root))
            if raises is not None:
                raise raises
            return result if result is not None else _result(branch=branch)

        return run

    def test_a_valid_message_refreshes_that_branch(self):
        body, _ = process_refresh(
            _message(branch="claude/issue-228-mailbox-refresh-type"),
            self.repo, self.conn, 1,
            refresher=self._refresher(), repo_root=self.repo,
        )
        self.assertEqual(
            self.calls, [("claude/issue-228-mailbox-refresh-type", self.repo)]
        )
        self.assertIn("Refresh complete", body)
        self.assertIn("aaaaaaa", body)
        self.assertIn("bbbbbbb", body)

    def test_the_branch_defaults_to_main(self):
        process_refresh(
            _message(), self.repo, self.conn, 1,
            refresher=self._refresher(), repo_root=self.repo,
        )
        self.assertEqual(self.calls[0][0], "main")

    def test_an_invalid_branch_is_reported_and_nothing_runs(self):
        body, _ = process_refresh(
            _message(branch="main; rm -rf /"),
            self.repo, self.conn, 1,
            refresher=self._refresher(), repo_root=self.repo,
        )
        self.assertEqual(self.calls, [], "a rejected branch must not reach the refresher")
        self.assertIn("Refresh rejected", body)
        self.assertIn("unchanged", body)

    def test_a_failed_refresh_is_reported_not_raised(self):
        body, _ = process_refresh(
            _message(branch="main"), self.repo, self.conn, 1,
            refresher=self._refresher(
                result=_result(success=False, updated=False, services=None,
                               error="could not fetch origin/main")
            ),
            repo_root=self.repo,
        )
        self.assertIn("Refresh failed", body)
        self.assertIn("could not fetch", body)

    def test_an_exception_from_the_refresher_is_reported_not_raised(self):
        body, _ = process_refresh(
            _message(branch="main"), self.repo, self.conn, 1,
            refresher=self._refresher(raises=RuntimeError("boom")),
            repo_root=self.repo,
        )
        self.assertIn("Refresh failed", body)
        self.assertIn("boom", body)

    def test_no_services_restarted_is_said_out_loud(self):
        # Otherwise "none restarted" and "the field was missing" look the same
        # in a report that says success.
        body, _ = process_refresh(
            _message(branch="main"), self.repo, self.conn, 1,
            refresher=self._refresher(result=_result(services=None)),
            repo_root=self.repo,
        )
        self.assertIn("No services were restarted", body)


class MessageCannotWidenTheOperationTests(unittest.TestCase):
    """What the front matter must NOT be able to influence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_the_service_allowlist_holds_only_expected_units(self):
        self.assertEqual(ALLOWED_SERVICES, ("agent-api",))

    def test_extra_front_matter_keys_are_ignored(self):
        seen = []

        def run(branch, repo_root):
            seen.append((branch, repo_root))
            return _result(branch=branch)

        process_refresh(
            _message(
                branch="main",
                services="nginx",
                restart_services="sshd",
                repo_root="/etc",
                remote="evil",
                command="rm -rf /",
            ),
            self.repo, self.conn, 1,
            refresher=run, repo_root=self.repo,
        )
        # Only the branch and the deployment's own root got through.
        self.assertEqual(seen, [("main", self.repo)])

    def test_the_body_is_not_executed_or_echoed(self):
        message = SimpleNamespace(meta={"branch": "main"}, body="rm -rf / # do it")
        body, _ = process_refresh(
            message, self.repo, self.conn, 1,
            refresher=lambda b, r: _result(branch=b), repo_root=self.repo,
        )
        self.assertNotIn("rm -rf", body)


class RegistrationTests(unittest.TestCase):
    def test_refresh_is_a_supported_type_with_a_processor(self):
        import modules.mailbox as mailbox

        self.assertIn("refresh", mailbox.SUPPORTED_TYPES)
        self.assertIs(mailbox._PROCESSORS["refresh"], process_refresh)


if __name__ == "__main__":
    unittest.main()
