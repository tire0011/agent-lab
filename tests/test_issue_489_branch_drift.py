"""#489: how far an experiment's branch is behind main, and where both changed.

Uses a real temporary git repository with a real bare ``origin`` so fetch,
merge-base and diff run exactly as on the VM.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from modules.branch_drift import branch_drift, drift_summary, experiment_drift
from modules.experiment_service import ExperimentNotFoundError
from modules.nexus_planner import planning_prompt
from tests.test_issue_354_planning_step import ISSUES, _Planner, _answer
from tests.test_nexus_initiative import InitiativeFixture

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
BRANCH = "experiment/abc"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class DriftRepo:
    """A bare origin, the Nexus checkout, and a second clone that pushes to main."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-")
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.other = root / "other"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        commit(self.repo, {"modules/service.py": "v1\n", "README.md": "readme\n"}, "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(root, "clone", "-q", "-b", "main", str(self.origin), str(self.other))
        # The experiment branch lives in its own worktree, like on the VM.
        self.worktree = root / "worktrees" / "abc"
        git(self.repo, "worktree", "add", "-q", "-b", BRANCH, str(self.worktree), "main")
        commit(self.worktree, {"modules/service.py": "branch\n"}, "experiment work")

    def main_moves_on(self, files: dict[str, str]) -> None:
        commit(self.other, files, "main work")
        git(self.other, "push", "-q", "origin", "main")

    def drift(self, branch: str | None = BRANCH, **kwargs):
        kwargs.setdefault("worktree_path", str(self.worktree))
        return branch_drift(branch, repo_root=str(self.repo), **kwargs)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class BranchDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r = DriftRepo()
        self.addCleanup(self.r.cleanup)

    def test_no_new_commits_on_main_is_not_behind(self):
        drift = self.r.drift()
        self.assertEqual(drift["status"], "ok")
        self.assertEqual(drift["commits_behind"], 0)
        self.assertEqual(drift["commits_ahead"], 1)
        self.assertEqual(drift["files_changed_on_branch"], ["modules/service.py"])
        self.assertEqual(drift["files_changed_on_main_since_base"], [])
        self.assertEqual(drift["overlap_files"], [])
        self.assertTrue(drift["fetched"])

    def test_main_moving_on_in_unrelated_files_counts_without_overlap(self):
        self.r.main_moves_on({"docs/a.md": "a\n"})
        self.r.main_moves_on({"docs/b.md": "b\n"})
        drift = self.r.drift()
        self.assertEqual(drift["status"], "ok")
        self.assertEqual(drift["commits_behind"], 2)
        self.assertEqual(drift["files_changed_on_main_since_base"], ["docs/a.md", "docs/b.md"])
        self.assertEqual(drift["overlap_files"], [])

    def test_the_fetch_is_what_makes_new_main_commits_visible(self):
        self.r.main_moves_on({"docs/a.md": "a\n"})
        self.assertEqual(self.r.drift(fetch=False)["commits_behind"], 0)
        self.assertEqual(self.r.drift(fetch=True)["commits_behind"], 1)

    def test_a_file_both_sides_changed_is_listed_as_overlap(self):
        self.r.main_moves_on({"modules/service.py": "main\n", "docs/a.md": "a\n"})
        drift = self.r.drift()
        self.assertEqual(drift["commits_behind"], 1)
        self.assertEqual(drift["overlap_files"], ["modules/service.py"])
        self.assertEqual(drift_summary(drift), "commits_behind=1; overlap_files=[modules/service.py]")

    def test_uncommitted_work_in_the_worktree_counts_as_branch_change(self):
        (self.r.worktree / "README.md").write_text("changed but not committed\n")
        (self.r.worktree / "new_module.py").write_text("x\n")
        self.r.main_moves_on({"README.md": "main readme\n"})
        drift = self.r.drift()
        self.assertEqual(drift["overlap_files"], ["README.md"])
        self.assertIn("new_module.py", drift["files_changed_on_branch"])

    def test_a_missing_branch_is_unknown_not_an_exception(self):
        drift = self.r.drift("experiment/does-not-exist", worktree_path=None)
        self.assertEqual(drift["status"], "unknown")
        self.assertIn("branch experiment/does-not-exist not found", drift["reason"])
        self.assertIsNone(drift["commits_behind"])
        self.assertEqual(drift["overlap_files"], [])

    def test_a_missing_repository_and_worktree_is_unknown(self):
        drift = branch_drift(BRANCH, repo_root=str(self.r.repo / "gone"), worktree_path=str(self.r.repo / "also-gone"))
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "experiment worktree not found")
        self.assertFalse(drift["worktree_exists"])
        self.assertEqual(drift_summary(drift), "drift: unknown (experiment worktree not found)")

    def test_an_experiment_without_branch_is_unknown(self):
        drift = self.r.drift(None)
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "experiment has no branch")

    def test_an_unreachable_origin_is_unknown_not_stale_cached_data(self):
        git(self.r.repo, "remote", "set-url", "origin", str(self.r.origin) + "-missing")
        drift = self.r.drift()
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "git fetch origin main failed")
        self.assertIsNone(drift["commits_behind"])
        self.assertFalse(drift["fetched"])
        self.assertTrue(drift["fetch_error"])

    def test_a_timed_out_fetch_is_unknown_not_stale_cached_data(self):
        def runner(command, **kwargs):
            if command[1:3] == ["fetch", "--quiet"]:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return subprocess.run(command, **kwargs)

        drift = self.r.drift(runner=runner)
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "git fetch origin main failed")
        self.assertIsNone(drift["commits_behind"])
        self.assertFalse(drift["fetched"])

    def test_a_supplied_missing_worktree_is_unknown_even_when_branch_ref_exists(self):
        drift = self.r.drift(worktree_path=str(self.r.worktree.parent / "missing"))
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "experiment worktree not found")

    def test_a_supplied_worktree_on_the_wrong_branch_is_unknown(self):
        drift = self.r.drift(worktree_path=str(self.r.repo))
        self.assertEqual(drift["status"], "unknown")
        self.assertIn("does not have branch", drift["reason"])

    def test_a_failed_required_diff_is_unknown(self):
        def runner(command, **kwargs):
            if command[1:3] == ["diff", "--name-only"]:
                return subprocess.CompletedProcess(command, 1, "", "diff failed")
            return subprocess.run(command, **kwargs)

        drift = self.r.drift(fetch=False, runner=runner)
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "required git diff failed")
        self.assertEqual(drift["overlap_files"], [])

    def test_a_timed_out_required_diff_is_unknown(self):
        def runner(command, **kwargs):
            if command[1:3] == ["diff", "--name-only"]:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return subprocess.run(command, **kwargs)

        drift = self.r.drift(fetch=False, runner=runner)
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "required git diff failed")

    def test_a_failed_worktree_diff_is_unknown(self):
        def runner(command, **kwargs):
            if kwargs.get("cwd") == str(self.r.worktree) and command[1:3] == ["diff", "--name-only"]:
                return subprocess.CompletedProcess(command, 1, "", "diff failed")
            return subprocess.run(command, **kwargs)

        drift = self.r.drift(fetch=False, runner=runner)
        self.assertEqual(drift["status"], "unknown")
        self.assertEqual(drift["reason"], "required worktree diff failed")

    def test_experiment_drift_resolves_the_experiment(self):
        experiment = SimpleNamespace(id="e1", branch_name=BRANCH, worktree_path=str(self.r.worktree))
        service = SimpleNamespace(repo_root=str(self.r.repo), resolve_experiment=lambda _id: experiment)
        drift = experiment_drift(service, "e1")
        self.assertEqual(drift["experiment_id"], "e1")
        self.assertEqual(drift["status"], "ok")


class SurfaceTests(unittest.TestCase):
    """Operations registry, API endpoint and MCP read-only listing."""

    def setUp(self) -> None:
        self.r = DriftRepo()
        self.addCleanup(self.r.cleanup)
        self.r.main_moves_on({"modules/service.py": "main\n"})
        self.experiment = SimpleNamespace(
            id="ab" * 16, status="needs_review", branch_name=BRANCH, worktree_path=str(self.r.worktree)
        )

    def _resolve(self, experiment_id: str):
        if self.experiment.id.startswith(experiment_id):
            return self.experiment
        raise ExperimentNotFoundError(experiment_id)

    def test_registry_operation_is_read_only_and_reports_overlap(self):
        from modules.operations_registry import OperationsRegistry

        service = MagicMock()
        service.resolve_experiment.side_effect = self._resolve
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = OperationsRegistry(
            experiment_service=service, db_path=str(Path(tmp.name) / "agent.db"), repo_root=str(self.r.repo)
        )
        self.addCleanup(registry.close)
        spec = next(s for s in registry.catalog() if s.name == "experiment_drift")
        self.assertFalse(spec.mutates_state)
        result = registry.execute("experiment_drift", {"experiment_id": self.experiment.id})
        self.assertTrue(result.success, result.error_summary)
        self.assertIn("commits_behind: 1", result.output_lines)
        self.assertIn("overlap_files: modules/service.py", result.output_lines)

    def test_api_endpoint_returns_drift_and_404s_unknown_ids(self):
        from fastapi.testclient import TestClient

        import api

        service = MagicMock()
        service.resolve_experiment.side_effect = self._resolve
        service.repo_root = str(self.r.repo)
        api.app.dependency_overrides[api.get_experiment_service] = lambda: service
        self.addCleanup(api.app.dependency_overrides.clear)
        client = TestClient(api.app)
        headers = {"x-api-key": os.environ["AGENT_API_KEY"]}
        self.assertEqual(client.get(f"/experiments/{self.experiment.id}/drift").status_code, 401)
        response = client.get(f"/experiments/{self.experiment.id}/drift", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["commits_behind"], 1)
        self.assertEqual(body["overlap_files"], ["modules/service.py"])
        self.assertEqual(client.get("/experiments/ffffffffff/drift", headers=headers).status_code, 404)

    def test_mcp_tool_is_listed_as_read_only(self):
        from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS

        self.assertIn("experiment_drift", READ_ONLY_TOOLS)
        self.assertNotIn("experiment_drift", WRITE_TOOLS)


class PlannerDriftTests(unittest.TestCase):
    def test_planner_prompt_snapshot_shows_drift_for_a_needs_review_experiment(self):
        prompt = planning_prompt(
            [{"number": 351, "title": "Small fix", "body": "b"}],
            [{
                "title": "GitHub Issue #378: Stall sweep",
                "status": "needs_review",
                "error": "",
                "adoption_status": "",
                "adoption_reason": "",
                "commits_behind": "14",
                "overlap_files": "modules/experiment_service/service.py",
            }],
        )
        expected = (
            "YOUR RECENT EXPERIMENTS:\n\n"
            "- needs_review / merge: none: GitHub Issue #378: Stall sweep "
            "[branch commits_behind=14; overlap_files=[modules/experiment_service/service.py]]\n\n"
            "commits_behind says how many commits main is ahead of that experiment's "
            "branch; overlap_files are files both the branch and main changed since "
            "they split. A branch far behind main or with overlap_files will likely "
            "hit an integration conflict: prefer a fresh new_experiment over resuming "
            "or adopting it.\n\n"
            "OPEN ISSUES:\n\n#351 Small fix\nb"
        )
        self.assertIn(expected, prompt)

    def test_without_drift_the_line_and_prompt_are_unchanged(self):
        prompt = planning_prompt([{"number": 1, "title": "t", "body": ""}],
                                 [{"title": "x", "status": "failed", "error": "", "adoption_status": "", "adoption_reason": ""}])
        self.assertIn("- failed / merge: none: x\n\nOPEN ISSUES:", prompt)
        self.assertNotIn("commits_behind", prompt)

    def _fixture_with_branches(self) -> InitiativeFixture:
        fx = InitiativeFixture()
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("ALTER TABLE experiments ADD COLUMN branch_name TEXT")
            conn.execute("ALTER TABLE experiments ADD COLUMN worktree_path TEXT")
        fx.add_experiment("e1", "needs_review", ended="2026-11-01T10:00:00+00:00")
        fx.add_experiment("e2", "failed", ended="2026-11-01T09:00:00+00:00")
        fx.add_experiment("e3", "succeeded", adoption="succeeded", ended="2026-11-01T08:00:00+00:00")
        with sqlite3.connect(fx.db_path) as conn:
            for exp_id in ("e1", "e2", "e3"):
                conn.execute("UPDATE experiments SET branch_name=?, worktree_path=? WHERE id=?",
                             (f"experiment/{exp_id}", f"/wt/{exp_id}", exp_id))
        return fx

    def test_the_initiative_hands_drift_to_the_planner_and_fetches_once(self):
        fx = self._fixture_with_branches()
        calls = []

        def drift(branch, worktree, fetch=True):
            calls.append((branch, worktree, fetch))
            if branch == "experiment/e1":
                return {"status": "ok", "commits_behind": 7, "overlap_files": ["modules/experiment_service/service.py"], "fetched": True}
            return {"status": "unknown", "reason": "branch not found"}

        planner = _Planner(_answer(351))
        svc = fx.service(issue_lister=lambda: ISSUES[:2], planner=planner, branch_drift=drift)
        self.addCleanup(svc.close)
        svc.tick()
        prompt = planner.prompts[0]
        self.assertIn(
            "- needs_review / merge: none: Goal for e1 "
            "[branch commits_behind=7; overlap_files=[modules/experiment_service/service.py]]",
            prompt,
        )
        self.assertIn("- failed / merge: none: Goal for e2 [branch commits_behind=unknown; overlap_files=[]]", prompt)
        # A merged experiment's branch no longer matters.
        self.assertIn("- succeeded / merge: merged: Goal for e3\n", prompt)
        self.assertEqual(calls, [("experiment/e1", "/wt/e1", True), ("experiment/e2", "/wt/e2", False)])

    def test_an_unknown_before_fetch_does_not_make_later_drift_use_stale_main(self):
        fx = self._fixture_with_branches()
        calls = []

        def drift(branch, worktree, fetch=True):
            calls.append((branch, fetch))
            if branch == "experiment/e1":
                return {"status": "unknown", "reason": "experiment worktree not found", "fetched": False}
            return {"status": "ok", "commits_behind": 2, "overlap_files": [], "fetched": True}

        planner = _Planner(_answer(351))
        svc = fx.service(issue_lister=lambda: ISSUES[:2], planner=planner, branch_drift=drift)
        self.addCleanup(svc.close)
        svc.tick()
        self.assertEqual(calls, [("experiment/e1", True), ("experiment/e2", True)])

    def test_a_failing_drift_reader_does_not_block_planning(self):
        fx = self._fixture_with_branches()

        def broken(branch, worktree, fetch=True):
            raise RuntimeError("git exploded")

        planner = _Planner(_answer(351))
        svc = fx.service(issue_lister=lambda: ISSUES[:2], planner=planner, branch_drift=broken)
        self.addCleanup(svc.close)
        svc.tick()
        self.assertIn("- needs_review / merge: none: Goal for e1\n", planner.prompts[0])


if __name__ == "__main__":
    unittest.main()
