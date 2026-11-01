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


class ServiceRestartReportingTests(unittest.TestCase):
    """A deploy that did not restart is not a deploy.

    The first real refresh moved the checkout and reported
    `service agent-api: error: ` - no reason - under the heading "Refresh
    complete". Both halves were wrong: the reason was missing, and the new
    code was on disk while the old code kept serving.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _body(self, services):
        body, _ = process_refresh(
            _message(branch="main"), self.repo, self.conn, 1,
            refresher=lambda b, r: _result(branch=b, services=services),
            repo_root=self.repo,
        )
        return body

    def test_a_failed_restart_is_not_called_complete(self):
        body = self._body({"agent-api": "error: exit 1: permission denied"})
        self.assertIn("incomplete", body)
        self.assertNotIn("Refresh complete", body)

    def test_a_failed_restart_names_the_service_that_is_not_running_new_code(self):
        body = self._body({"agent-api": "error: exit 1: nope"})
        self.assertIn("Not running the new code", body)
        self.assertIn("agent-api", body)

    def test_a_scheduled_restart_is_pending_not_failed(self):
        """The mirror of the bug it replaced.

        The service does restart; this process simply does not survive to
        watch it. Reporting that as "not running the new code" is as wrong as
        reporting it as complete.
        """
        body = self._body({"agent-api": "restart scheduled (this process is being replaced)"})
        self.assertIn("Restart scheduled", body)
        self.assertNotIn("Not running the new code", body)
        self.assertNotIn("incomplete", body)

    def test_a_fallback_restart_is_reported_as_unknown_not_failed(self):
        body = self._body(
            {"agent-api": "restart scheduled via fallback; outcome unknown"}
        )
        self.assertIn("Restart outcome unknown", body)
        self.assertIn("verify the service", body)
        self.assertNotIn("Not running the new code", body)
        self.assertNotIn("incomplete", body)

    def test_a_not_restarted_result_keeps_the_refresh_incomplete(self):
        body = self._body(
            {
                "agent-api": (
                    "error: not restarted; transient restart failed: exit 1: denied; "
                    "fallback failed: exit 5: failed"
                )
            }
        )
        self.assertIn("Refresh incomplete", body)
        self.assertIn("Not running the new code", body)
        self.assertIn("not restarted", body)

    def test_an_all_restarted_refresh_is_complete(self):
        body = self._body({"agent-api": "restarted"})
        self.assertIn("Refresh complete", body)
        self.assertNotIn("incomplete", body)
        self.assertNotIn("Not running the new code", body)


class SystemdRestarterTests(unittest.TestCase):
    """The reason for a failure must survive into the report."""

    def _restart(self, returncode, stderr="", stdout=""):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        completed = SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)
        with patch.object(refresh_module.subprocess, "run", return_value=completed):
            return refresh_module.systemd_restarter(("agent-api",))()

    def test_a_successful_restart_says_so(self):
        self.assertEqual(self._restart(0), {"agent-api": "restarted"})

    def test_a_failure_carries_the_exit_code_and_the_message(self):
        result = self._restart(1, stderr="Interactive authentication required.")
        self.assertIn("exit 1", result["agent-api"])
        self.assertIn("Interactive authentication required", result["agent-api"])

    def test_a_silent_failure_still_says_something_actionable(self):
        # This is the one that happened: non-zero with both streams empty.
        result = self._restart(1)
        self.assertIn("exit 1", result["agent-api"])
        self.assertNotEqual(result["agent-api"].strip(), "error:")
        self.assertIn("run it by hand", result["agent-api"])


class SelfRestartTests(unittest.TestCase):
    """Restarting the unit you are running inside is a different operation.

    The first mailbox refresh restarted `agent-api` from inside `agent-api`.
    systemd tore the caller down while it was still waiting for its own
    `systemctl` to return, so the call came back non-zero with both streams
    empty and a restart that *worked* was reported as a failure - the journal
    shows the unit stopping and starting cleanly at the same second.
    """

    def _run(self, services, current_unit):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        seen = []

        def fake_run(command, **kwargs):
            seen.append(command)
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch.object(refresh_module.subprocess, "run", side_effect=fake_run):
            result = refresh_module.systemd_restarter(
                services, current_unit=current_unit
            )()
        return result, seen

    def test_restarting_our_own_unit_leaves_our_control_group(self):
        """--no-block was not enough, and the VM proved it.

        Stopping a unit kills its whole control group, and our `systemctl` is
        a child of the process being stopped. Returning early is still a race
        against our own teardown, and the refresh lost it: `exit -15`,
        SIGTERM, reported for a restart that had in fact succeeded. A
        transient unit is owned by systemd, so it survives us.
        """
        result, commands = self._run(("agent-api",), "agent-api")
        self.assertIn("systemd-run", commands[0])
        self.assertIn("scheduled", result["agent-api"])

    def test_the_transient_unit_is_cleaned_up_and_delayed(self):
        # --collect so it does not linger; a delay so this process can finish
        # writing its report before it is replaced.
        _result, commands = self._run(("agent-api",), "agent-api")
        self.assertIn("--collect", commands[0])
        self.assertTrue(
            any(str(part).startswith("--on-active=") for part in commands[0]),
            commands[0],
        )

    def test_restarting_another_unit_still_blocks_and_reports_the_outcome(self):
        result, commands = self._run(("nginx",), "agent-api")
        self.assertNotIn("--no-block", commands[0])
        self.assertEqual(result["nginx"], "restarted")

    def test_outside_a_unit_nothing_is_treated_as_self(self):
        result, commands = self._run(("agent-api",), None)
        self.assertNotIn("--no-block", commands[0])
        self.assertEqual(result["agent-api"], "restarted")

    def test_a_queued_restart_is_not_reported_as_done(self):
        """It returns before the outcome exists; say so, do not guess."""
        result, _ = self._run(("agent-api",), "agent-api")
        self.assertNotEqual(result["agent-api"], "restarted")

    def test_failed_transient_restart_uses_no_block_fallback(self):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        attempts = [
            SimpleNamespace(returncode=1, stderr="systemd-run unavailable", stdout=""),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
        ]
        with patch.object(refresh_module.subprocess, "run", side_effect=attempts) as run:
            result = refresh_module.systemd_restarter(
                ("agent-api",), current_unit="agent-api"
            )()

        self.assertIn("systemd-run", run.call_args_list[0].args[0])
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["sudo", "systemctl", "restart", "--no-block", "agent-api"],
        )
        self.assertIn("scheduled via fallback", result["agent-api"])
        self.assertIn("outcome unknown", result["agent-api"])

    def test_missing_systemd_run_still_uses_fallback(self):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        completed = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(
            refresh_module.subprocess, "run", side_effect=[FileNotFoundError("missing"), completed]
        ) as run:
            result = refresh_module.systemd_restarter(
                ("agent-api",), use_sudo=False, current_unit="agent-api"
            )()

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["systemctl", "restart", "--no-block", "agent-api"],
        )
        self.assertIn("outcome unknown", result["agent-api"])

    def test_fallback_sigterm_means_replaced_mid_call(self):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        attempts = [
            SimpleNamespace(returncode=1, stderr="denied", stdout=""),
            SimpleNamespace(returncode=-15, stderr="", stdout=""),
        ]
        with patch.object(refresh_module.subprocess, "run", side_effect=attempts):
            result = refresh_module.systemd_restarter(
                ("agent-api",), current_unit="agent-api"
            )()

        self.assertEqual(
            result["agent-api"],
            "restart attempted via fallback; replaced mid-call, outcome unknown",
        )
        self.assertNotIn("error", result["agent-api"])

    def test_both_restart_methods_failing_says_not_restarted(self):
        from unittest.mock import patch
        import modules.nexus_refresh as refresh_module

        attempts = [
            SimpleNamespace(returncode=1, stderr="transient denied", stdout=""),
            SimpleNamespace(returncode=5, stderr="unit failed", stdout=""),
        ]
        with patch.object(refresh_module.subprocess, "run", side_effect=attempts):
            result = refresh_module.systemd_restarter(
                ("agent-api",), current_unit="agent-api"
            )()

        self.assertIn("not restarted", result["agent-api"])
        self.assertIn("transient denied", result["agent-api"])
        self.assertIn("unit failed", result["agent-api"])


class OwnUnitTests(unittest.TestCase):
    def _own_unit(self, text):
        from modules.nexus_refresh import get_current_systemd_unit

        path = Path(self._tmp.name) / "cgroup"
        path.write_text(text, encoding="utf-8")
        return get_current_systemd_unit(str(path))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_a_systemd_service_cgroup_is_recognised(self):
        self.assertEqual(
            self._own_unit("0::/system.slice/agent-api.service\n"), "agent-api"
        )

    def test_a_nested_scope_still_finds_the_service(self):
        self.assertEqual(
            self._own_unit("0::/system.slice/agent-api.service/somescope\n"),
            "agent-api",
        )

    def test_no_service_in_the_cgroup_is_not_a_guess(self):
        self.assertIsNone(self._own_unit("0::/user.slice/user-1000.slice\n"))

    def test_an_unreadable_cgroup_is_not_an_error(self):
        from modules.nexus_refresh import get_current_systemd_unit

        self.assertIsNone(get_current_systemd_unit(str(Path(self._tmp.name) / "missing")))


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
