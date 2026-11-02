from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import unquote

from modules.experiment_resume import resume_experiment
from modules.experiment_service import ExperimentNotRunnableError, STATUS_VERIFYING
from modules.github_work_tracking import GitHubWorkTrackingService
from tests.test_github_work_tracking import FakeGitHub


class ExperimentResumeTests(unittest.TestCase):
    def test_timed_out_verification_reuses_id_and_worktree_without_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "existing-worktree"
            worktree.mkdir()
            experiment = SimpleNamespace(
                id="bd9720b26ec74f15a2693e5641adf141",
                status="timed_out",
                worktree_path=str(worktree),
            )
            final = SimpleNamespace(id=experiment.id, status="succeeded")
            service = MagicMock()
            service.resolve_experiment.return_value = experiment
            logger = MagicMock()
            service.get_experiment_logger.return_value = logger
            service._verify.return_value = final

            result = resume_experiment(service, experiment.id)

            self.assertIs(result, final)
            service.resolve_experiment.assert_called_once_with(experiment.id)
            service._force_status.assert_called_once_with(experiment.id, STATUS_VERIFYING)
            service._verify.assert_called_once_with(experiment.id, str(worktree), logger)
            service.run_experiment.assert_not_called()
            self.assertFalse(service.create_experiment.called)

    def test_queued_resume_starts_same_experiment(self):
        experiment = SimpleNamespace(
            id="queued-1",
            status="queued",
            worktree_path=None,
        )
        final = SimpleNamespace(id="queued-1", status="succeeded")
        service = MagicMock()
        service.resolve_experiment.return_value = experiment
        service.run_experiment.return_value = final

        result = resume_experiment(service, "queued-1")

        self.assertIs(result, final)
        service.run_experiment.assert_called_once_with("queued-1")
        self.assertFalse(service.create_experiment.called)


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


class _OriginBackedGitHub(FakeGitHub):
    """FakeGitHub whose branch refs live in a real bare origin repository."""

    def __init__(self, origin: Path):
        super().__init__()
        self.origin = origin

    def __call__(self, method, path, payload):
        if method == "GET" and "/git/ref/heads/" in path:
            self.calls.append((method, path, payload))
            branch = unquote(path.split("/git/ref/heads/", 1)[1])
            found = subprocess.run(
                ["git", f"--git-dir={self.origin}", "rev-parse", "--verify", "-q",
                 f"refs/heads/{branch}"],
                text=True, capture_output=True,
            )
            if found.returncode != 0:
                from modules.github_work_tracking import GitHubWorkTrackingError
                raise GitHubWorkTrackingError("GitHub HTTP 404: not found")
            return {"object": {"sha": found.stdout.strip()}}
        if method == "POST" and path.endswith("/git/refs"):
            self.calls.append((method, path, payload))
            subprocess.run(
                ["git", f"--git-dir={self.origin}", "update-ref", payload["ref"], payload["sha"]],
                check=True, capture_output=True,
            )
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        return super().__call__(method, path, payload)


class ResumeMergesCurrentMainTests(unittest.TestCase):
    """#486: a resume verifies today's main, not the tree it was built on."""

    EXPERIMENT_ID = "resume-exp"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.upstream = root / "upstream"
        self.worktree = root / "worktree"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.upstream)],
                       check=True, capture_output=True)
        self._identity(self.upstream)
        (self.upstream / "feature.py").write_text("value = 1\n", encoding="utf-8")
        (self.upstream / "other.py").write_text("other = 1\n", encoding="utf-8")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-m", "base")
        _git(self.upstream, "push", "origin", "HEAD:main")

        self.fake = _OriginBackedGitHub(self.origin)
        self.tracking = GitHubWorkTrackingService(
            db_path=str(root / "nexus.db"),
            repo_root=str(self.upstream),
            repository="tire0011/agent-lab",
            transport=self.fake,
        )
        self.ref = self.tracking.bind_experiment(
            self.tracking.ensure_for_source(
                SimpleNamespace(source_type="task", source_id="resume",
                                title="Resume", content="Resume")
            ),
            self.EXPERIMENT_ID,
        )
        self.tracking.ensure_for_experiment(self.EXPERIMENT_ID)
        subprocess.run(["git", "clone", "-b", self.ref.branch_name, str(self.origin),
                        str(self.worktree)], check=True, capture_output=True)
        self._identity(self.worktree)

        self.verified_shas: list[str] = []
        self.final = SimpleNamespace(id=self.EXPERIMENT_ID, status="succeeded")
        self.logger = MagicMock()
        self.service = MagicMock()
        self.service.get_experiment_logger.return_value = self.logger
        self.service._verify.side_effect = self._verify
        self.service.resolve_experiment.return_value = SimpleNamespace(
            id=self.EXPERIMENT_ID, status="needs_review", worktree_path=str(self.worktree)
        )

    def tearDown(self):
        self.tracking.close()
        self.tmp.cleanup()

    @staticmethod
    def _identity(repo: Path) -> None:
        _git(repo, "config", "user.name", "Nexus Test")
        _git(repo, "config", "user.email", "nexus@example.test")

    def _verify(self, experiment_id, worktree_path, elog, github_work_tracking=None):
        # Like ExperimentService._verify: CI runs on the published checkpoint.
        if github_work_tracking is not None:
            ref = github_work_tracking.checkpoint_experiment(experiment_id, worktree_path)
            self.verified_shas.append(ref.checkpoint_sha)
        else:
            self.verified_shas.append(_git(worktree_path, "rev-parse", "HEAD"))
        return self.final

    def _push_main(self, path: str, text: str) -> str:
        (self.upstream / path).write_text(text, encoding="utf-8")
        _git(self.upstream, "commit", "-am", f"main changes {path}")
        _git(self.upstream, "push", "origin", "HEAD:main")
        return _git(self.upstream, "rev-parse", "HEAD")

    def _previously_verified_dirty_worktree(self) -> str:
        """Uncommitted work already published once, as an earlier verify left it."""
        (self.worktree / "feature.py").write_text("value = 2\n", encoding="utf-8")
        return self.tracking.checkpoint_experiment(
            self.EXPERIMENT_ID, str(self.worktree)
        ).checkpoint_sha

    def _resume(self):
        return resume_experiment(
            self.service, self.EXPERIMENT_ID, github_work_tracking=self.tracking
        )

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.worktree, capture_output=True,
        ).returncode == 0

    def test_main_moved_in_unrelated_files_is_merged_and_verified(self):
        (self.worktree / "feature.py").write_text("value = 2\n", encoding="utf-8")
        _git(self.worktree, "commit", "-am", "experiment work")
        _git(self.worktree, "push", "origin", f"HEAD:{self.ref.branch_name}")
        old_checkpoint = _git(self.worktree, "rev-parse", "HEAD")
        main_sha = self._push_main("other.py", "other = 2\n")

        result = self._resume()

        self.assertIs(result, self.final)
        head = _git(self.worktree, "rev-parse", "HEAD")
        self.assertTrue(self._is_ancestor(main_sha, head))
        self.assertEqual(self.verified_shas, [head])
        self.assertNotEqual(head, old_checkpoint)
        self.assertEqual(
            _git(self.origin, "rev-parse", f"refs/heads/{self.ref.branch_name}"), head
        )
        self.service._force_status.assert_called_once_with(self.EXPERIMENT_ID, STATUS_VERIFYING)

    def test_conflict_with_main_refuses_without_markers_or_verification(self):
        self._previously_verified_dirty_worktree()
        head_before = _git(self.worktree, "rev-parse", "HEAD")
        self._push_main("feature.py", "value = 3\n")

        with self.assertRaises(ExperimentNotRunnableError) as ctx:
            self._resume()

        self.assertIn("feature.py", str(ctx.exception))
        self.service._verify.assert_not_called()
        self.assertEqual(
            (self.worktree / "feature.py").read_text(encoding="utf-8"), "value = 2\n"
        )
        self.assertFalse((self.worktree / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(_git(self.worktree, "diff", "--name-only", "--diff-filter=U"), "")
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), head_before)
        self.assertIn("feature.py", _git(self.worktree, "status", "--porcelain"))
        events = [call.args[1] for call in self.logger.warning.call_args_list]
        self.assertIn("resume_base_conflict", events)
        self.assertNotIn(
            ((self.EXPERIMENT_ID, STATUS_VERIFYING),),
            [(c.args,) for c in self.service._force_status.call_args_list],
        )

    def test_uncommitted_changes_survive_and_are_verified_with_main(self):
        old_checkpoint = self._previously_verified_dirty_worktree()
        main_sha = self._push_main("other.py", "other = 2\n")

        self._resume()

        self.assertEqual(
            (self.worktree / "feature.py").read_text(encoding="utf-8"), "value = 2\n"
        )
        self.assertEqual(len(self.verified_shas), 1)
        verified = self.verified_shas[0]
        self.assertNotEqual(verified, old_checkpoint)
        self.assertTrue(self._is_ancestor(main_sha, verified))
        self.assertTrue(self._is_ancestor(old_checkpoint, verified))
        self.assertEqual(_git(self.worktree, "show", f"{verified}:feature.py"), "value = 2")
        self.assertEqual(_git(self.worktree, "show", f"{verified}:other.py"), "other = 2")

    def test_main_already_contained_needs_no_merge(self):
        old_checkpoint = self._previously_verified_dirty_worktree()
        head_before = _git(self.worktree, "rev-parse", "HEAD")

        self._resume()

        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), head_before)
        self.assertEqual(self.verified_shas, [old_checkpoint])
        self.assertIn("feature.py", _git(self.worktree, "status", "--porcelain"))

    def test_without_work_tracking_nothing_is_fetched_or_merged(self):
        (self.worktree / "feature.py").write_text("value = 2\n", encoding="utf-8")
        head_before = _git(self.worktree, "rev-parse", "HEAD")
        self._push_main("other.py", "other = 2\n")

        resume_experiment(self.service, self.EXPERIMENT_ID)

        self.assertEqual(self.verified_shas, [head_before])
        self.assertFalse((self.worktree / ".git" / "FETCH_HEAD").exists())
        self.service._verify.assert_called_once_with(
            self.EXPERIMENT_ID, str(self.worktree), self.logger
        )


if __name__ == "__main__":
    unittest.main()
