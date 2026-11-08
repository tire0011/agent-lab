"""#396: a reviewer reads the change before Nexus merges it.

Claude, Codex and Azure take turns. A reviewer that cannot run is skipped; when
none can, the change is not blocked.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.experiment_adoption_service import ExperimentAdoptionService
from modules.nexus_reviewer import NexusReviewer, ReviewResult, parse_review
from tests.test_nexus_initiative import InitiativeFixture

APPROVE = json.dumps({"verdict": "approve", "summary": "Adds the cleanup and a test."})
BLOCK = json.dumps({"verdict": "block", "summary": "Deletes the policy check."})


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        self.calls = []

    def _runner(self, name, answer=APPROVE, fail=False):
        def run(prompt, worktree):
            self.calls.append(name)
            if fail:
                raise RuntimeError(f"{name} down")
            return answer
        return run

    def _review(self, reviewer):
        return reviewer.review(experiment_id="e", goal="g", worktree_path=".", base_sha="")

    def test_the_three_reviewers_take_turns(self):
        reviewer = NexusReviewer(db_path=self.db, runners={n: self._runner(n) for n in ("claude", "codex", "azure")})
        names = [self._review(reviewer).reviewer for _ in range(4)]
        self.assertEqual(names, ["claude", "codex", "azure", "claude"])

    def test_a_reviewer_that_cannot_run_is_skipped(self):
        reviewer = NexusReviewer(db_path=self.db, runners={
            "claude": self._runner("claude", fail=True),
            "codex": self._runner("codex", answer="not json"),
            "azure": self._runner("azure", answer=BLOCK),
        })
        result = self._review(reviewer)
        self.assertEqual(self.calls, ["claude", "codex", "azure"])
        self.assertEqual((result.reviewer, result.blocks), ("azure", True))

    def test_no_reviewer_does_not_block(self):
        reviewer = NexusReviewer(db_path=self.db, runners={"claude": self._runner("claude", fail=True)})
        result = self._review(reviewer)
        self.assertFalse(result.blocks)
        self.assertEqual(result.verdict, "unreviewed")

    def test_only_a_clear_verdict_counts(self):
        self.assertIsNone(parse_review('{"verdict": "maybe", "summary": "x"}'))
        self.assertIsNone(parse_review('{"verdict": "block", "summary": ""}'))
        self.assertEqual(parse_review(BLOCK)[0], "block")


class _Tracking:
    def __init__(self):
        self.comments = []

    def comment(self, number, body):
        self.comments.append((number, body))


class AdoptionReviewTests(unittest.TestCase):
    def _service(self, result):
        service = ExperimentAdoptionService.__new__(ExperimentAdoptionService)
        service.reviewer = SimpleNamespace(review=lambda **kw: result)
        service.github_work_tracking = _Tracking()
        service._integration_base_sha = lambda: "base"
        return service

    def test_a_blocking_review_stops_the_merge_and_is_posted(self):
        service = self._service(ReviewResult("codex", "block", "Deletes the policy check."))
        reason = service._review_blocks("e", "g", SimpleNamespace(pr_number=7), Path("."), None)
        self.assertIn("Review by codex blocked the merge", reason)
        self.assertEqual(service.github_work_tracking.comments[0][0], 7)
        self.assertIn("blocked", service.github_work_tracking.comments[0][1])

    def test_an_approving_review_lets_the_merge_go_on(self):
        service = self._service(ReviewResult("claude", "approve", "Fine."))
        self.assertIsNone(service._review_blocks("e", "g", SimpleNamespace(pr_number=7), Path("."), None))
        self.assertEqual(len(service.github_work_tracking.comments), 1)


class RetryAfterBlockedReviewTests(unittest.TestCase):
    def test_a_blocked_review_frees_the_issue_for_a_retry(self):
        fx = InitiativeFixture()
        fx.add_experiment("e1", "succeeded", adoption="failed")
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES ('e1', 9)")
        svc = fx.service(issue_lister=lambda: [{"number": 9, "author": {"login": "tire0011"}, "title": "x", "body": ""}])
        self.addCleanup(svc.close)
        svc.tick()
        self.assertEqual(fx.controller.issues, [9])


class EarlierAttemptsTests(unittest.TestCase):
    def test_the_retry_gets_the_review_that_blocked_the_first_attempt(self):
        fx = InitiativeFixture()
        fx.add_experiment("e1", "succeeded", adoption="failed")
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason=? WHERE experiment_id='e1'",
                         ("Review by claude blocked the merge: missing tests count as pre_existing.",))
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES ('e1', 9)")
        bodies = []
        fx.controller.handle_issue = lambda number, *, title, body: bodies.append(body)
<<<<<<< HEAD
        svc = fx.service(issue_lister=lambda: [{"number": 9, "title": "x", "body": "Do it."}])
=======
        svc = fx.service(issue_lister=lambda: [{"number": 9, "author": {"login": "tire0011"}, "title": "x", "body": "Do it."}])
>>>>>>> 52a4b01578b1b5a7d958d8a7181e3b69d18b2849
        self.addCleanup(svc.close)
        svc.tick()
        self.assertTrue(bodies[0].startswith("Do it."))
        self.assertIn("Earlier attempts at this issue", bodies[0])
        self.assertIn("missing tests count as pre_existing", bodies[0])

    def test_paths_in_earlier_attempts_are_not_required_artifacts(self):
        from modules.experiment_evaluation import extract_explicit_goal_artifacts
        from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING

<<<<<<< HEAD
        goal = f"Add modules/x.py\n\n{EARLIER_ATTEMPTS_HEADING}\n- erforderliches Artefakt git/hooks/a.sample fehlt"
        self.assertEqual(extract_explicit_goal_artifacts(goal), ["modules/x.py"])
=======
        goal = f"Add modules/web_research.py\n\n{EARLIER_ATTEMPTS_HEADING}\n- erforderliches Artefakt git/hooks/a.sample fehlt"
        self.assertEqual(extract_explicit_goal_artifacts(goal), ["modules/web_research.py"])
>>>>>>> 52a4b01578b1b5a7d958d8a7181e3b69d18b2849


if __name__ == "__main__":
    unittest.main()
