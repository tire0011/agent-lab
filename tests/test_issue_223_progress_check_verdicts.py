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
