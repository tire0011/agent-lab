"""The #223 manual check's verdict, over scripted runs instead of a real one.

The check itself drives the real local controller, which no test suite can do.
Its *verdict* is ordinary logic and can be wrong in ways that cost a reader
hours: reporting a regression when the controller recovered correctly, or
reporting a pass when nothing was verified.

Two mistakes this pins, both of which it made before being pinned:

* a rejected first attempt and its corrected retry are the controller
  recovering, not a repeat - counting from the first *execution* rather than
  from the first *success* failed the behaviour the check wants;
* a call that never succeeded cannot answer "does the controller reuse a
  successful result", so that run is inconclusive rather than failed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_issue_196_controller_progress.py"


def _load_check():
    """Import the script by path; `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("issue_223_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines a dataclass, and
    # @dataclass resolves annotations through sys.modules.
    sys.modules["issue_223_check"] = module
    spec.loader.exec_module(module)
    return module


check = _load_check()


SUCCESS = {
    "tool": "operation",
    "operation": "github_issue",
    "args": {"issue_number": 196},
    "result": {"operation": "github_issue", "success": True, "output_lines": ["Issue #196: ..."]},
    "event_id": 158,
}
REJECTED = {
    "tool": "operation",
    "operation": "github_issue",
    "args": {"issue_number": "196"},
    "error": "invalid_tool_request",
    "reason": "issue_number muss eine positive ganze Zahl sein.",
    "event_id": 157,
}
CALL = {"action": "call_tool", "tool": "github_issue", "arguments": {"issue_number": 196}, "reason": "fetch it"}
ANSWER = {"action": "answer", "message": "done", "reason": "the issue content is enough"}


class _FakeDecision:
    def __init__(self, observations, action, reason):
        self.observations = observations
        self.action = action
        self.reason = reason
        self.session_id = "session-223"


def _run(observations, decisions, *, action="answer", reason="done", argv=None):
    """Run the check's main() over scripted decisions and observations."""

    class _FakeOrchestrator:
        def _parse(self, raw):
            return json.loads(raw)

        def _build_prompt(self, *_args, **_kwargs):
            return (
                "OBSERVATIONS FROM TOOLS USED IN THIS RUN:\n[]\nDecide the next step now.",
                {"omitted_observation_indices": []},
            )

        def run(self, *_args, **_kwargs):
            # main() has replaced _parse with its recorder by now, which is
            # how the check sees what the controller decided.
            for decision in decisions:
                self._parse(json.dumps(decision))
            return _FakeDecision(observations, action, reason)

        def close(self):
            pass

    orchestrator = _FakeOrchestrator()
    with patch.object(check, "NexusTaskOrchestrator", lambda **_k: orchestrator), \
         patch.object(check, "OperationsRegistry", lambda **_k: None), \
         patch.object(sys, "argv", ["check"] + (argv or [])):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = check.main()
    return code, captured.getvalue()


class VerdictTests(unittest.TestCase):
    def test_success_then_a_different_action_passes(self):
        code, out = _run([SUCCESS], [CALL, ANSWER])
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_a_rejected_attempt_then_a_corrected_one_is_recovery_not_a_repeat(self):
        code, out = _run([REJECTED, SUCCESS], [CALL, CALL, ANSWER])
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_asking_again_after_the_success_fails(self):
        code, out = _run([SUCCESS], [CALL, CALL, ANSWER])
        self.assertEqual(code, 1, out)
        self.assertIn("again after it succeeded", out)

    def test_executing_it_again_after_the_success_fails(self):
        code, out = _run([SUCCESS, dict(SUCCESS, event_id=170)], [CALL, CALL, ANSWER])
        self.assertEqual(code, 1, out)

    def test_a_call_that_never_succeeded_is_inconclusive_not_failed(self):
        code, out = _run([REJECTED], [CALL, ANSWER])
        self.assertEqual(code, 2, out)
        self.assertIn("INCONCLUSIVE", out)

    def test_looping_on_a_failing_call_is_inconclusive_but_reported(self):
        code, out = _run(
            [REJECTED, dict(REJECTED, event_id=160)],
            [CALL, CALL, ANSWER],
            action="clarification",
            reason="tool loop limit reached after duplicate_tool_call",
        )
        self.assertEqual(code, 2, out)
        self.assertIn("INCONCLUSIVE", out)
        self.assertIn("requested", out)  # the loop is still surfaced

    def test_never_reaching_the_operation_is_inconclusive(self):
        code, out = _run([], [ANSWER])
        self.assertEqual(code, 2, out)

    def test_a_substitute_operation_watches_that_call_instead(self):
        git_status = {
            "tool": "operation",
            "operation": "git_status",
            "args": {},
            "result": {"operation": "git_status", "success": True, "output_lines": ["M README.md"]},
            "event_id": 12,
        }
        call = {"action": "call_tool", "tool": "git_status", "arguments": {}, "reason": "inspect"}
        code, out = _run([git_status], [call, ANSWER], argv=["--operation", "git_status"])
        self.assertEqual(code, 0, out)
        code, out = _run([git_status], [call, call, ANSWER], argv=["--operation", "git_status"])
        self.assertEqual(code, 1, out)


class RecordedSessionTests(unittest.TestCase):
    """Judging a run that already happened, from the journal.

    This is the mode that verifies the canonical #223 scenario on the VM. The
    mailbox `command` channel strips TOKEN/KEY/SECRET from the child
    environment by design, so `github_issue` cannot authenticate when the
    check drives the controller there. The `task` channel builds the
    orchestrator in-process inside a service that has the credential, so the
    canonical run happens there and this reads back what it did.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "agent.db")

    def _session(self, observations, *, kind="answer", reason="done"):
        from modules.nexus_task_store import NexusTaskStore

        store = NexusTaskStore(db_path=self.db_path)
        try:
            session = store.get_or_create(
                source_type="task", source_id="s", title="solve github issue #196"
            )
            store.claim_session(session.id, worker_id="w1")
            for observation in observations:
                store.append_event(
                    session.id,
                    role="tool",
                    kind=observation.get("tool", "tool"),
                    content=json.dumps(observation),
                    metadata={},
                )
            store.append_event(
                session.id, role="assistant", kind=kind, content="x",
                metadata={"reason": reason},
            )
        finally:
            store.close()
        return session.id

    def _judge(self, session_id, extra=None):
        argv = ["--session-id", session_id, "--db-path", self.db_path] + (extra or [])
        with patch.object(sys, "argv", ["check"] + argv):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = check.main()
        return code, captured.getvalue()

    def test_a_successful_unrepeated_call_passes(self):
        code, out = self._judge(self._session([SUCCESS]))
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_the_same_call_executed_twice_fails(self):
        session = self._session(
            [SUCCESS, dict(SUCCESS)],
            kind="clarification",
            reason="tool loop limit reached after duplicate_tool_call",
        )
        code, out = self._judge(session)
        self.assertEqual(code, 1, out)
        self.assertIn("after it succeeded", out)

    def test_a_loop_guard_refusal_of_the_same_call_is_seen(self):
        """The guard's refusal is journalled even though the request is not."""
        refusal = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 196},
            "event": "duplicate_tool_call",
            "recovery": "choose_different_tool_or_controlled_final_action",
        }
        code, out = self._judge(
            self._session(
                [SUCCESS, refusal],
                kind="clarification",
                reason="tool loop limit reached after duplicate_tool_call",
            )
        )
        self.assertEqual(code, 1, out)
        self.assertIn("loop guard", out)

    def test_a_refusal_is_not_reported_as_an_execution(self):
        """Nothing ran; saying it did overstates what happened.

        The real VM run reported "executed 1 more time(s)" for a call the
        guard had just refused, which points the reader at the wrong layer -
        the operation was never re-run, the controller merely asked.
        """
        refusal = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 196},
            "event": "non_progressing_cycle",
        }
        code, out = self._judge(
            self._session(
                [SUCCESS, refusal],
                kind="clarification",
                reason="tool loop limit reached after non_progressing_cycle",
            )
        )
        self.assertEqual(code, 1, out)
        self.assertNotIn("executed 1 more time", out)
        self.assertIn("asked for", out)
        self.assertIn("refused", out)

    def test_a_call_that_never_succeeded_is_inconclusive(self):
        code, out = self._judge(self._session([REJECTED]))
        self.assertEqual(code, 2, out)
        self.assertIn("INCONCLUSIVE", out)

    def test_a_refused_argument_is_not_blamed_on_the_credential(self):
        """Naming the wrong cause sends the next reader to the wrong place.

        The canonical VM run failed with `invalid_tool_request` because the
        deployed checkout predated the integer-coercion fix, and the check
        said a missing token looks exactly like this. It does not.
        """
        code, out = self._judge(self._session([REJECTED]))
        self.assertEqual(code, 2, out)
        self.assertIn("not a credential problem", out)
        self.assertNotIn("missing or expired", out)

    def test_a_capability_failure_does_point_at_the_credential(self):
        failed = {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 196},
            "result": {
                "operation": "github_issue",
                "success": False,
                "output_lines": [],
                "error_summary": "GitHub lifecycle is required",
            },
        }
        code, out = self._judge(self._session([failed]))
        self.assertEqual(code, 2, out)
        self.assertIn("GITHUB_TOKEN", out)
        self.assertNotIn("not a credential problem", out)

    def test_an_unknown_session_is_inconclusive_not_a_crash(self):
        code, out = self._judge("no-such-session")
        self.assertEqual(code, 2, out)
        self.assertIn("no session", out)

    def test_a_session_with_no_tool_events_is_inconclusive(self):
        code, out = self._judge(self._session([]))
        self.assertEqual(code, 2, out)
        self.assertIn("no tool observations", out)

    def test_a_failure_shows_what_the_controller_actually_read(self):
        """Without this the mode cannot show the thing #223 is about.

        The first canonical run reported that the controller had asked again
        and showed nothing of the prompt, so it could not show that the
        successful result had arrived as a bare stub with the issue body
        compacted away. Prompts are not journalled, but they are a function
        of the observations, which are.
        """
        session = self._session(
            [SUCCESS, dict(SUCCESS, event_id=180)],
            kind="clarification",
            reason="tool loop limit reached after duplicate_tool_call",
        )
        code, out = self._judge(session)
        self.assertEqual(code, 1, out)
        self.assertIn("compact observations given to the controller", out)
        # The replayed section must carry the real observation, not a header.
        self.assertIn('"operation":"github_issue"', out)

    def test_show_prompts_works_on_a_passing_session_too(self):
        code, out = self._judge(self._session([SUCCESS]), extra=["--show-prompts"])
        self.assertEqual(code, 0, out)
        self.assertIn("compact observations given to the controller", out)

    def test_a_replayed_prompt_is_not_labelled_as_a_measured_one(self):
        """It is reconstructed at this host's budget, and says so."""
        code, out = self._judge(self._session([SUCCESS]), extra=["--show-prompts"])
        self.assertIn("replayed at a", out)
        self.assertNotIn("prompt None chars", out)

    def test_the_budget_that_bounded_the_prompt_is_reported(self):
        """The number that decided whether the result arrived at all.

        Finding this cost a journal grep for `controller_prompt_budget`: the
        canonical run showed a controller asking again, and not that a
        150-character observation budget had stubbed a 3005-character result
        away. A check about what the controller reads has to show what
        bounded it.
        """
        code, out = self._judge(self._session([SUCCESS]))
        self.assertEqual(code, 0, out)
        self.assertIn("observation_chars", out)
        self.assertIn("low_memory", out)

    def test_a_squeezed_budget_says_what_it_costs(self):
        """150 characters is not a number a reader can price on sight."""
        plan = check._live_budget()
        plan.observation_chars = 150

        with patch.object(check, "_live_budget", return_value=plan):
            _code, out = self._judge(self._session([SUCCESS]))

        self.assertIn("stubbed away", out)
        self.assertIn("not what it returned", out)

    def test_a_healthy_budget_carries_no_warning(self):
        plan = check._live_budget()
        plan.observation_chars = 9000

        with patch.object(check, "_live_budget", return_value=plan):
            _code, out = self._judge(self._session([SUCCESS]))

        self.assertNotIn("stubbed away", out)

    def test_the_production_database_is_never_deleted(self):
        """The driving mode unlinks its scratch db; this one must not."""
        session = self._session([SUCCESS])
        self._judge(session)
        self.assertTrue(Path(self.db_path).exists())


class TargetMatchingTests(unittest.TestCase):
    def setUp(self):
        self.target = check._Target("github_issue", {"issue_number": 196})

    def test_an_integer_and_its_string_spelling_are_the_same_call(self):
        self.assertTrue(check._is_target_operation("github_issue", {"issue_number": 196}, self.target))
        self.assertTrue(check._is_target_operation("github_issue", {"issue_number": "196"}, self.target))

    def test_extra_arguments_do_not_stop_it_matching(self):
        self.assertTrue(
            check._is_target_operation("github_issue", {"issue_number": 196, "verbose": True}, self.target)
        )

    def test_a_different_issue_or_operation_does_not_match(self):
        self.assertFalse(check._is_target_operation("github_issue", {"issue_number": 199}, self.target))
        self.assertFalse(check._is_target_operation("git_status", {}, self.target))

    def test_an_empty_filter_matches_any_arguments(self):
        target = check._Target("git_status", {})
        self.assertTrue(check._is_target_operation("git_status", {}, target))
        self.assertTrue(check._is_target_operation("git_status", {"anything": 1}, target))

    def test_a_malformed_number_does_not_raise(self):
        # "--196" is not something int() accepts; stripping signs and
        # converting anyway used to raise mid-run.
        self.assertFalse(check._is_target_operation("github_issue", {"issue_number": "--196"}, self.target))


if __name__ == "__main__":
    unittest.main()
