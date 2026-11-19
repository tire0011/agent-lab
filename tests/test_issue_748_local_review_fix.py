"""The initiative retry gate, with real Git worktrees and verification tests."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from modules import local_review_fix
from modules.nexus_initiative import Candidate, KIND_ISSUE, REVIEW_RETRY_REASON
from modules.nexus_reviewer import NexusReviewer
from modules.verification_runner import WatchdogConfig
from tests.test_nexus_initiative import InitiativeFixture

SUMMARY = "Wrong values. Blocking problems: (1) Fix values.py:1. (2) Fix other.py:1."
BLOCK = f"Review by codex blocked the merge: {SUMMARY}"
PATCH = """--- a/values.py
+++ b/values.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = {value}
"""
OTHER = """--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


class Llama:
    def __init__(self):
        self.answers = []
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def retry(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for key, value in {
        "NEXUS_LOCAL_REVIEW_FIX": "1",
        "NEXUS_LOCAL_REVIEW_FIX_ROUNDS": "1",
        "NEXUS_HANDOVER_FACT_CHECK": "0",
        "LOCAL_EDIT_FORMAT": "diff",
        "LOCAL_MODEL_RATES_PATH": str(tmp_path / "rates.json"),
        "NEXUS_TEST_LOG_DIR": str(tmp_path / "logs"),
    }.items():
        monkeypatch.setenv(key, value)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "remote", "add", "origin", ".")
    (root / "values.py").write_text("VALUE = 0\n")
    (root / "other.py").write_text("VALUE = 0\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_values.py").write_text(
        "from values import VALUE\n"
        "from pathlib import Path\n"
        "import os\n"
        "import unittest\n\n"
        "class ValuesTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(VALUE, 2, 'expected local fix')\n"
        "        if os.environ.get('NEXUS_TEST_CLOCK_DRIFT_MS'):\n"
        "            self.assertNotEqual(Path('other.py').read_text().strip(), 'VALUE = 4')\n"
    )
    git(root, "add", ".")
    base = git(root, "commit-tree", git(root, "write-tree"), "-m", "base")
    git(root, "update-ref", "refs/heads/main", base)
    (root / "values.py").write_text("VALUE = 1\n")
    (root / "other.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    head = git(root, "commit-tree", git(root, "write-tree"), "-p", base, "-m", "blocked")
    git(root, "update-ref", "refs/heads/issue-748", head)
    git(root, "reset", "--hard", base)

    with patch("tests.test_nexus_initiative.tempfile.mkdtemp", return_value=str(tmp_path)):
        fx = InitiativeFixture()
    fx.add_experiment("blocked", "succeeded", adoption="failed")
    with sqlite3.connect(fx.db_path) as db:
        db.execute("UPDATE experiment_adoptions SET reason=?", (BLOCK,))
        db.execute("INSERT INTO github_experiment_links VALUES ('blocked', 748)")

    llama = Llama()
    monkeypatch.setattr(local_review_fix, "LlamaCppClient", lambda **kwargs: llama)
    paid_review = Mock(return_value=json.dumps({"verdict": "approve", "summary": "Fixed."}))
    other_reviewer = Mock()
    reviewer = NexusReviewer(db_path=fx.db_path, runners={"codex": paid_review, "claude": other_reviewer})
    with sqlite3.connect(fx.db_path) as db:
        db.execute(
            "INSERT INTO nexus_pr_reviews (experiment_id, reviewer, verdict, summary, created_at) "
            "VALUES ('blocked', 'codex', 'block', ?, datetime('now'))",
            (SUMMARY,),
        )
    tracking = Mock()
    ref = SimpleNamespace(branch_name="issue-748", pr_number=17)
    tracking.for_experiment.return_value = ref
    tracking._remote_branch_sha.return_value = head
    tracking.merged_commit_sha.return_value = None
    tracking.wait_for_commit_ci.return_value = SimpleNamespace(status="passed", reason=None)
    tracking.merge_and_complete.return_value = "merged-sha"

    def publish(work_ref, *, integrated_commit_sha, worktree_path, experiment_id, expected_head_sha):
        assert work_ref is ref
        assert expected_head_sha == head
        assert Path(worktree_path) != root
        commit = git(root, "commit-tree", integrated_commit_sha, "-p", head, "-m", "local fix")
        git(root, "update-ref", "refs/heads/issue-748", commit, head)
        return commit

    tracking.publish_final_tree.side_effect = publish
    elog = Mock()
    experiments = SimpleNamespace(
        get_experiment_logger=lambda _id: elog,
        get_experiment=lambda _id: SimpleNamespace(goal="Fix values", plan=None),
        test_command=["{python}", "-m", "unittest", "discover", "-s", "tests"],
        test_python_executable=sys.executable,
        _get_test_watchdog_config=lambda: WatchdogConfig(10, 0.05, 30),
    )
    adoption = Mock(
        repo_root=root, lock_path=tmp_path / "adoption.lock", reviewer=reviewer,
        experiment_service=experiments, github_work_tracking=tracking,
    )
    adoption.get.return_value = SimpleNamespace(status="failed", reason=BLOCK, integrated_commit_sha=head)
    adoption._denied_path_reason.return_value = None
    adoption._sync_local_repo.return_value = (False, "adopted")
    fx.adoption = adoption
    fx.controller = Mock()
    initiative = fx.service()
    candidate = Candidate(KIND_ISSUE, "748", "Fix values", REVIEW_RETRY_REASON, {"body": "Issue body"})
    yield SimpleNamespace(
        root=root, head=head, initiative=initiative, candidate=candidate, tracking=tracking,
        controller=fx.controller, llama=llama, paid_review=paid_review, other_reviewer=other_reviewer,
        adoption=adoption, elog=elog,
    )
    initiative.close()
    assert git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def event(retry):
    return next(call.kwargs for call in retry.elog.info.call_args_list
                if call.args[1] == "local_review_fix")


def test_green_local_fix_goes_straight_to_same_reviewer(retry):
    retry.llama.answers = [PATCH.format(value=2) + OTHER]
    assert retry.initiative._dispatch(retry.candidate), retry.controller.handle_issue.call_args
    retry.controller.handle_issue.assert_not_called()
    retry.paid_review.assert_called_once()
    retry.other_reviewer.assert_not_called()
    assert SUMMARY in retry.paid_review.call_args.args[0]
    assert "Likely files: other.py, values.py" in retry.llama.prompts[0]
    assert "Branch diff:" in retry.llama.prompts[0]
    assert git(retry.root, "show", "issue-748:values.py") == "VALUE = 2"
    assert retry.tracking.merge_and_complete.call_args.kwargs["expected_head_sha"] == git(
        retry.root, "rev-parse", "issue-748",
    )
    assert event(retry)["outcome"] == "merged"
    assert event(retry)["paid_coder_avoided"]
    assert event(retry)["reviewer_accepted"]
    assert event(retry)["findings_covered"] == [1, 2]
    assert event(retry)["tests_passed"]
    assert event(retry)["rounds"] == 1


@pytest.mark.parametrize(("answer", "tail", "covered", "tests_passed"), [
    (PATCH.format(value=3) + OTHER, "expected local fix", [1, 2], False),
    (PATCH.format(value=2), "No changed hunk for findings [2]", [1], True),
    (ConnectionError("llama unreachable"), "llama unreachable", [], False),
])
def test_failed_local_fix_calls_paid_coder_once(retry, answer, tail, covered, tests_passed):
    retry.llama.answers = [answer]
    assert not retry.initiative._dispatch(retry.candidate)
    retry.controller.handle_issue.assert_called_once()
    body = retry.controller.handle_issue.call_args.kwargs["retry_handover"]
    assert tail in body
    if isinstance(answer, str):
        assert "+VALUE =" in body
    retry.tracking.publish_final_tree.assert_not_called()
    retry.paid_review.assert_not_called()
    assert event(retry)["outcome"] == "paid_fallback"
    assert event(retry)["findings_covered"] == covered
    assert event(retry)["tests_passed"] is tests_passed


def test_local_retry_flag_off_keeps_original_handover(retry, monkeypatch):
    monkeypatch.setenv("NEXUS_LOCAL_REVIEW_FIX", "off")
    expected = "Issue body" + retry.initiative._earlier_attempts(748)
    assert not retry.initiative._dispatch(retry.candidate)
    retry.controller.handle_issue.assert_called_once_with(748, title="Fix values", body=expected)
    retry.adoption.get.assert_not_called()
    assert retry.llama.prompts == []


def test_timing_failure_falls_back_even_when_experiment_tests_pass(retry):
    retry.llama.answers = [PATCH.format(value=2) + OTHER.replace("+VALUE = 2", "+VALUE = 4")]
    assert not retry.initiative._dispatch(retry.candidate)
    retry.controller.handle_issue.assert_called_once()
    assert "Timing check:" in retry.controller.handle_issue.call_args.kwargs["retry_handover"]
    retry.tracking.publish_final_tree.assert_not_called()
    assert event(retry)["findings_covered"] == [1, 2]
    assert not event(retry)["tests_passed"]


def test_next_round_gets_test_feedback_and_can_fix_the_patch(retry, monkeypatch):
    monkeypatch.setenv("NEXUS_LOCAL_REVIEW_FIX_ROUNDS", "2")
    retry.llama.answers = [
        PATCH.format(value=3) + OTHER,
        PATCH.format(value=2).replace("-VALUE = 1", "-VALUE = 3"),
    ]
    assert retry.initiative._dispatch(retry.candidate), retry.controller.handle_issue.call_args
    assert "expected local fix" in retry.llama.prompts[1]
    assert "Local patch so far:" in retry.llama.prompts[1]
    assert event(retry)["rounds"] == 2
    retry.controller.handle_issue.assert_not_called()


def test_reviewer_block_falls_back_and_preserves_the_new_findings(retry):
    retry.llama.answers = [PATCH.format(value=2) + OTHER]
    retry.paid_review.return_value = json.dumps({
        "verdict": "block", "summary": "There is still an edge case.",
        "problems": ["Handle the default in values.py."],
    })
    assert not retry.initiative._dispatch(retry.candidate)
    retry.tracking.merge_and_complete.assert_not_called()
    retry.controller.handle_issue.assert_called_once()
    assert "Handle the default in values.py." in retry.controller.handle_issue.call_args.kwargs["retry_handover"]
    assert event(retry)["review_requested"]
    assert not event(retry)["reviewer_accepted"]


def test_retry_diagnostics_survive_issue_body_limit():
    from modules.nexus_sources import issue_source

    diagnostics = "\n\nLocal review fix diagnostics\nA failure tail and diff"
    source = issue_source(748, title="Fix values", body="a" * 16000, retry_handover=diagnostics)
    assert source.content.endswith("a" * 16000 + diagnostics)
