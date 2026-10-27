"""#354: the initiative tick asks a planner which open issue is next.

#353 removed the label gate but still took open issues in list order.
"""

from __future__ import annotations

import json
import unittest

from modules.nexus_initiative import KIND_ISSUE
from modules.nexus_planner import IssuePlanner, parse_choice, planning_prompt
from tests.test_nexus_initiative import InitiativeFixture

ISSUES = [
    {"number": 158, "title": "Architecture: inverted model roles", "body": "big", "labels": []},
    {"number": 351, "title": "Multi-line idea input in the Web UI", "body": "small", "labels": []},
    {"number": 360, "title": "Suggested by Nexus", "body": "x", "labels": [{"name": "proposal"}]},
]


def _answer(issue, reason="small, concrete and testable", proposal=None):
    return json.dumps({"issue": issue, "reason": reason, "proposal": proposal})


class _Planner:
    def __init__(self, raw):
        self.prompts = []
        self._planner = IssuePlanner(self._complete)
        self._raw = raw

    def _complete(self, prompt):
        self.prompts.append(prompt)
        return self._raw

    def choose(self, issues, recent=None):
        return self._planner.choose(issues, recent)


class PlanningStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = InitiativeFixture()
        self.comments: list[tuple[int, str]] = []
        self.proposals: list[tuple[str, str]] = []

    def _service(self, raw, issues=ISSUES, **extra):
        planner = _Planner(raw)
        svc = self.fx.service(
            issue_lister=lambda: [dict(issue) for issue in issues],
            planner=planner,
            issue_commenter=lambda number, text: self.comments.append((number, text)),
            proposal_creator=lambda title, body: self.proposals.append((title, body)),
            **extra,
        )
        self.addCleanup(svc.close)
        return svc, planner

    def test_the_planned_issue_is_dispatched_with_its_reason(self):
        svc, _ = self._service(_answer(351))
        decision = svc.tick()
        self.assertEqual(decision.candidate.kind, KIND_ISSUE)
        self.assertEqual(self.fx.controller.issues, [351])
        self.assertEqual(decision.candidate.reason, "small, concrete and testable")
        self.assertEqual(self.comments, [(351, "Nexus picked this issue as the next one to work on: small, concrete and testable")])

    def test_proposals_are_never_offered_to_the_planner(self):
        svc, planner = self._service(_answer(360))
        decision = svc.tick()
        self.assertNotIn("#360", planner.prompts[0])
        self.assertEqual(decision.decision, "idle")
        self.assertEqual(self.fx.controller.issues, [])

    def test_no_usable_plan_starts_nothing(self):
        for raw in ("", "I think #351.", _answer(999), json.dumps({"issue": 351, "reason": ""})):
            with self.subTest(raw=raw):
                self.fx.controller.issues.clear()
                svc, _ = self._service(raw)
                self.assertEqual(svc.tick().decision, "idle")
                self.assertEqual(self.fx.controller.issues, [])

    def test_a_suggested_issue_is_opened_as_proposal(self):
        svc, _ = self._service(_answer(351, proposal={"title": "Add X", "body": "Change Y, test Z"}))
        svc.tick()
        self.assertEqual(self.proposals, [("Add X", "Change Y, test Z")])

    def test_no_new_proposal_while_three_are_open(self):
        waiting = [
            {"number": 400 + i, "title": f"P{i}", "body": "", "labels": [{"name": "proposal"}]} for i in range(3)
        ]
        svc, _ = self._service(
            _answer(351, proposal={"title": "Add X", "body": "Change Y"}), issues=ISSUES[:2] + waiting
        )
        svc.tick()
        self.assertEqual(self.fx.controller.issues, [351])
        self.assertEqual(self.proposals, [])

    def test_proposals_beyond_the_listed_issues_still_count(self):
        # Review on #355: the issue list stops at 100, older proposals must count.
        svc, _ = self._service(
            _answer(351, proposal={"title": "Add X", "body": "Change Y"}), proposal_counter=lambda: 3
        )
        svc.tick()
        self.assertEqual(self.fx.controller.issues, [351])
        self.assertEqual(self.proposals, [])

    def test_an_unknown_proposal_count_opens_no_proposal(self):
        def fail():
            raise RuntimeError("gh down")

        svc, _ = self._service(_answer(351, proposal={"title": "Add X", "body": "Change Y"}), proposal_counter=fail)
        svc.tick()
        self.assertEqual(self.fx.controller.issues, [351])
        self.assertEqual(self.proposals, [])

    def test_the_planner_is_not_asked_while_integration_work_waits(self):
        # Review on #355: the integration candidate goes first, so a planner
        # call would be a paid request whose answer is thrown away.
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc, planner = self._service(_answer(351))
        svc.candidates()
        self.assertEqual(planner.prompts, [])
        self.assertEqual(self.fx.controller.issues, [])

    def test_a_failed_comment_does_not_stop_the_work(self):
        planner = _Planner(_answer(351))

        def fail(number, text):
            raise RuntimeError("gh down")

        svc = self.fx.service(issue_lister=lambda: ISSUES, planner=planner, issue_commenter=fail)
        self.addCleanup(svc.close)
        self.assertEqual(svc.tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [351])


class PromptTests(unittest.TestCase):
    def test_the_planner_is_asked_for_its_own_idea_and_to_skip_web_ui_work(self):
        # #363: the owner wants Nexus's own ideas; the web UI is no longer a priority.
        prompt = planning_prompt(ISSUES[:2])
        self.assertIn("suggest exactly ONE new issue", prompt)
        self.assertNotIn("null or", prompt)
        self.assertIn("do not pick web UI work", prompt)


class OwnExperienceTests(unittest.TestCase):
    def test_the_planner_speaks_as_nexus_and_sees_its_recent_experiments(self):
        # The owner wants Nexus's own ideas, drawn from what went wrong for it.
        fx = InitiativeFixture()
        fx.add_experiment("e1", "timed_out")
        with __import__("sqlite3").connect(fx.db_path) as conn:
            conn.execute("UPDATE experiments SET goal=?, ended_at='2026-10-26T19:00:00+00:00' WHERE id='e1'",
                         ("GitHub Issue #235: Self-restart fallback\nmore",))
        planner = _Planner(_answer(351))
        svc = fx.service(issue_lister=lambda: ISSUES[:2], planner=planner)
        self.addCleanup(svc.close)
        svc.tick()
        prompt = planner.prompts[0]
        self.assertIn("You are Nexus, an independent agent", prompt)
        self.assertIn("- timed_out: GitHub Issue #235: Self-restart fallback", prompt)


class ParseChoiceTests(unittest.TestCase):
    def test_fenced_json_is_read(self):
        choice = parse_choice("```json\n" + _answer(351) + "\n```", {351})
        self.assertEqual(choice.issue, 351)

    def test_an_incomplete_proposal_is_ignored(self):
        choice = parse_choice(_answer(351, proposal={"title": "only a title"}), {351})
        self.assertEqual(choice.proposal_title, "")


if __name__ == "__main__":
    unittest.main()
