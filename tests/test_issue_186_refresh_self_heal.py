"""Regression tests for #186: refresh must self-heal a dirty deployment checkout.

The live case these cover: ``/agent`` was on ``main``, nine commits behind
``origin/main``, and refresh refused to run because one generated file -
``communication/outbox/20260914170857-failure-analysis-7a2bc0c1.md`` - sat
untracked in the worktree.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from modules.checkout_rescue import (
    ArchiveIndex,
    CheckoutRescue,
    RescueError,
    classify_untracked,
    inspect_checkout,
    is_runtime_artifact,
)
from modules.nexus_refresh import NexusRefresh
from modules.nexus_refresh import main as nexus_refresh_main

OBSERVED_ARTIFACT = "communication/outbox/20260914170857-failure-analysis-7a2bc0c1.md"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")


class RefreshHarness:
    """An upstream repository plus a deployment checkout that lags behind it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.upstream = root / "upstream"
        self.checkout = root / "agent"
        self.archive = root / "runtime-archive"

        _init_repo(self.upstream)
        (self.upstream / "agent.py").write_text("VERSION = 1\n", encoding="utf-8")
        (self.upstream / "communication" / "outbox").mkdir(parents=True)
        (self.upstream / "communication" / "outbox" / ".gitkeep").write_text("", encoding="utf-8")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-m", "initial")

        subprocess.run(
            ["git", "clone", str(self.upstream), str(self.checkout)],
            capture_output=True,
            text=True,
            check=True,
        )
        _git(self.checkout, "config", "user.email", "nexus@example.com")
        _git(self.checkout, "config", "user.name", "Nexus")
        _git(self.checkout, "config", "commit.gpgsign", "false")

    def advance_upstream(self, commits: int = 1) -> str:
        for index in range(commits):
            (self.upstream / "agent.py").write_text(f"VERSION = {index + 2}\n", encoding="utf-8")
            _git(self.upstream, "add", "-A")
            _git(self.upstream, "commit", "-m", f"upstream change {index + 1}")
        return _git(self.upstream, "rev-parse", "HEAD")

    def write(self, relative: str, content: str) -> Path:
        path = self.checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def refresh(self, **kwargs) -> object:
        options = {
            "archive_root": self.archive,
            "branch": "main",
            "remote": "origin",
        }
        options.update(kwargs)
        return NexusRefresh(self.checkout, **options).run()

    def branches(self) -> list[str]:
        raw = _git(self.checkout, "branch", "--format=%(refname:short)")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def rescue_branches(self) -> list[str]:
        return [name for name in self.branches() if name.startswith("nexus/rescue/")]

    def archived_files(self) -> list[Path]:
        if not self.archive.exists():
            return []
        return sorted(
            path
            for path in self.archive.rglob("*")
            if path.is_file() and path.name != "archive-index.json"
        )

    def status(self) -> str:
        return _git(self.checkout, "status", "--porcelain", "--untracked-files=all")


class RefreshSelfHealTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = RefreshHarness(Path(self._tmp.name))


class TestArtifactClassification(RefreshSelfHealTestCase):
    def test_observed_outbox_file_is_a_runtime_artifact(self) -> None:
        self.assertTrue(is_runtime_artifact(OBSERVED_ARTIFACT))

    def test_source_files_are_never_runtime_artifacts(self) -> None:
        for path in ("agent.py", "modules/nexus_refresh.py", "docs/NEXUS.md", "notes.txt"):
            with self.subTest(path=path):
                self.assertFalse(is_runtime_artifact(path))

    def test_lockfiles_and_data_outside_state_stay_work(self) -> None:
        """A dependency lockfile is source; only state/ output is archived."""
        for path in ("poetry.lock", "fixtures/sample.jsonl", "requirements.txt"):
            with self.subTest(path=path):
                self.assertFalse(is_runtime_artifact(path))
        self.assertTrue(is_runtime_artifact("state/mailbox.lock"))
        self.assertTrue(is_runtime_artifact("state/experiment-logs/run.jsonl"))

    def test_unknown_files_are_classified_as_work(self) -> None:
        artifacts, work = classify_untracked([OBSERVED_ARTIFACT, "scratch/unknown-thing"])
        self.assertEqual(artifacts, (OBSERVED_ARTIFACT,))
        self.assertEqual(work, ("scratch/unknown-thing",))

    def test_extra_patterns_extend_the_classification(self) -> None:
        artifacts, work = classify_untracked(
            ["tmpdata/dump.bin"], ("tmpdata/*", *()),
        )
        self.assertEqual(artifacts, ("tmpdata/dump.bin",))
        self.assertEqual(work, ())


class TestCleanCheckout(RefreshSelfHealTestCase):
    def test_clean_checkout_refreshes_without_rescue(self) -> None:
        expected = self.harness.advance_upstream(2)
        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.new_head, expected)
        self.assertTrue(result.updated)
        self.assertIsNone(result.rescue)
        self.assertEqual(self.harness.rescue_branches(), [])
        self.assertEqual(self.harness.status(), "")

    def test_up_to_date_checkout_reports_no_update(self) -> None:
        result = self.harness.refresh()
        self.assertTrue(result.success, result.error)
        self.assertFalse(result.updated)
        self.assertEqual(result.previous_head, result.new_head)


class TestTrackedChangesRescue(RefreshSelfHealTestCase):
    def test_tracked_source_changes_move_to_a_rescue_branch(self) -> None:
        expected = self.harness.advance_upstream(2)
        self.harness.write("agent.py", "VERSION = 1\nLOCAL_EXPERIMENT = True\n")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.new_head, expected)
        self.assertIsNotNone(result.rescue)
        self.assertTrue(result.rescue.rescue_branch.startswith("nexus/rescue/"))
        self.assertIn("agent.py", result.rescue.rescued_paths)
        self.assertEqual(self.harness.status(), "")
        self.assertEqual(
            _git(self.harness.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "main"
        )

        rescued = _git(
            self.harness.checkout,
            "show",
            f"{result.rescue.rescue_branch}:agent.py",
        )
        self.assertIn("LOCAL_EXPERIMENT = True", rescued)
        self.assertEqual(
            _git(self.harness.checkout, "rev-parse", result.rescue.rescue_branch),
            result.rescue.rescue_commit,
        )

    def test_staged_changes_are_rescued_too(self) -> None:
        self.harness.advance_upstream()
        self.harness.write("new_module.py", "print('hello')\n")
        _git(self.harness.checkout, "add", "new_module.py")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertIn("new_module.py", result.rescue.rescued_paths)
        self.assertIn(
            "new_module.py",
            _git(
                self.harness.checkout,
                "show",
                "--name-only",
                "--format=",
                result.rescue.rescue_branch,
            ),
        )

    def test_local_work_is_never_reset_away(self) -> None:
        self.harness.advance_upstream()
        self.harness.write("agent.py", "VERSION = 1\nKEEP_ME = 1\n")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        reachable = _git(
            self.harness.checkout, "show", f"{result.rescue.rescue_branch}:agent.py"
        )
        self.assertIn("KEEP_ME = 1", reachable)


class TestNothingToCommit(RefreshSelfHealTestCase):
    def test_staged_change_reverted_in_the_worktree_needs_no_rescue_branch(self) -> None:
        """A dirty status that stages down to nothing is not a rescue failure."""
        expected = self.harness.advance_upstream(2)
        original = (self.harness.checkout / "agent.py").read_text(encoding="utf-8")
        self.harness.write("agent.py", "VERSION = 99\n")
        _git(self.harness.checkout, "add", "agent.py")
        self.harness.write("agent.py", original)
        self.assertNotEqual(self.harness.status(), "")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.new_head, expected)
        self.assertIsNone(result.rescue.rescue_branch)
        self.assertEqual(result.rescue.rescued_paths, ())
        self.assertEqual(self.harness.rescue_branches(), [])
        self.assertEqual(self.harness.status(), "")


class TestRuntimeArtifactArchive(RefreshSelfHealTestCase):
    def test_observed_outbox_artifact_is_archived_without_a_code_commit(self) -> None:
        """The exact file that blocked the live refresh."""
        expected = self.harness.advance_upstream(9)
        self.harness.write(OBSERVED_ARTIFACT, "# failure analysis\n")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.new_head, expected)
        self.assertIsNone(result.rescue.rescue_branch)
        self.assertIsNone(result.rescue.rescue_commit)
        self.assertEqual(self.harness.rescue_branches(), [])

        archived = [item for item in result.rescue.archived if item.repo_path == OBSERVED_ARTIFACT]
        self.assertEqual(len(archived), 1)
        archived_path = Path(archived[0].archived_path)
        self.assertTrue(archived_path.is_file())
        self.assertEqual(archived_path.read_text(encoding="utf-8"), "# failure analysis\n")
        self.assertFalse((self.harness.checkout / OBSERVED_ARTIFACT).exists())
        self.assertEqual(self.harness.status(), "")

    def test_archive_lives_outside_the_repository(self) -> None:
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        archived_path = Path(result.rescue.archived[0].archived_path).resolve()
        self.assertFalse(
            str(archived_path).startswith(str(self.harness.checkout.resolve()) + "/")
        )

    def test_artifacts_and_work_are_separated_in_one_run(self) -> None:
        self.harness.advance_upstream()
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        self.harness.write("experiment.py", "print('work')\n")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [item.repo_path for item in result.rescue.archived], [OBSERVED_ARTIFACT]
        )
        self.assertEqual(result.rescue.rescued_paths, ("experiment.py",))
        committed = _git(
            self.harness.checkout,
            "show",
            "--name-only",
            "--format=",
            result.rescue.rescue_branch,
        )
        self.assertIn("experiment.py", committed)
        self.assertNotIn("communication/outbox", committed)


class TestUnknownFilePreservation(RefreshSelfHealTestCase):
    def test_unknown_untracked_file_is_committed_not_deleted(self) -> None:
        self.harness.advance_upstream()
        self.harness.write("scratch/mystery.dat", "unknown but precious\n")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertIn("scratch/mystery.dat", result.rescue.rescued_paths)
        preserved = _git(
            self.harness.checkout,
            "show",
            f"{result.rescue.rescue_branch}:scratch/mystery.dat",
        )
        self.assertEqual(preserved, "unknown but precious")
        self.assertEqual([item.repo_path for item in result.rescue.archived], [])

    def test_unknown_file_is_never_silently_dropped(self) -> None:
        self.harness.write("weird.bin", "x")
        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        reported = set(result.rescue.rescued_paths) | {
            item.repo_path for item in result.rescue.archived
        }
        self.assertIn("weird.bin", reported)


class TestFailureAborts(RefreshSelfHealTestCase):
    def test_unusable_archive_aborts_refresh_and_leaves_checkout_untouched(self) -> None:
        expected_head = _git(self.harness.checkout, "rev-parse", "HEAD")
        self.harness.advance_upstream(3)
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        # A regular file where the archive root belongs: mkdir cannot succeed.
        self.harness.archive.parent.mkdir(parents=True, exist_ok=True)
        self.harness.archive.write_text("not a directory\n", encoding="utf-8")

        result = self.harness.refresh()

        self.assertFalse(result.success)
        self.assertIn("aborted", result.error)
        self.assertEqual(_git(self.harness.checkout, "rev-parse", "HEAD"), expected_head)
        self.assertTrue((self.harness.checkout / OBSERVED_ARTIFACT).is_file())
        self.assertEqual(self.harness.rescue_branches(), [])

    def test_archive_failure_restores_already_moved_files(self) -> None:
        self.harness.write("state/first.log", "one\n")
        self.harness.write("state/second.log", "two\n")

        rescue = CheckoutRescue(self.harness.checkout, archive_root=self.harness.archive)
        original_move = CheckoutRescue._unique_destination

        calls = {"count": 0}

        def exploding_destination(candidate: Path) -> Path:
            calls["count"] += 1
            if calls["count"] > 1:
                raise OSError("archive volume went away")
            return original_move(candidate)

        with mock.patch.object(
            CheckoutRescue, "_unique_destination", staticmethod(exploding_destination)
        ):
            with self.assertRaises(RescueError):
                rescue.prepare()

        self.assertTrue((self.harness.checkout / "state/first.log").is_file())
        self.assertTrue((self.harness.checkout / "state/second.log").is_file())
        self.assertEqual(self.harness.rescue_branches(), [])

    def test_failed_fetch_keeps_the_checkout_on_main(self) -> None:
        _git(self.harness.checkout, "remote", "set-url", "origin", str(self.harness.root / "gone"))
        result = self.harness.refresh()

        self.assertFalse(result.success)
        self.assertIn("fetch", result.error)
        self.assertEqual(_git(self.harness.checkout, "rev-parse", "--abbrev-ref", "HEAD"), "main")


class TestSymlinks(RefreshSelfHealTestCase):
    def test_symlinked_artifact_is_preserved_instead_of_archived(self) -> None:
        """Never move a link out of the repository, and never leave it behind."""
        target = self.harness.write("notes.txt", "real file\n")
        link = self.harness.checkout / "state" / "latest.log"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertIn("state/latest.log", result.rescue.rescued_paths)
        self.assertEqual([item.repo_path for item in result.rescue.archived], [])
        # Committed as a link (mode 120000), not as a copy of its target, and
        # the archive never saw it.
        entry = _git(
            self.harness.checkout,
            "ls-tree",
            result.rescue.rescue_branch,
            "state/latest.log",
        )
        self.assertTrue(entry.startswith("120000"), entry)
        self.assertEqual(self.harness.archived_files(), [])
        self.assertEqual(self.harness.status(), "")


class TestCliEntryPoint(RefreshSelfHealTestCase):
    def test_cli_writes_a_report_and_reports_success(self) -> None:
        expected = self.harness.advance_upstream(2)
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        report_path = self.harness.root / "report.json"

        exit_code = nexus_refresh_main(
            [
                "--repo-root",
                str(self.harness.checkout),
                "--archive-root",
                str(self.harness.archive),
                "--json-out",
                str(report_path),
            ]
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["new_head"], expected)

    def test_cli_exits_nonzero_without_a_traceback_when_the_repo_is_missing(self) -> None:
        exit_code = nexus_refresh_main(["--repo-root", str(self.harness.root / "nope")])
        self.assertEqual(exit_code, 1)


class TestIdempotency(RefreshSelfHealTestCase):
    def test_repeated_refresh_creates_no_second_rescue_commit(self) -> None:
        self.harness.advance_upstream()
        self.harness.write("agent.py", "VERSION = 1\nLOCAL = 1\n")

        first = self.harness.refresh()
        self.assertTrue(first.success, first.error)
        branches_after_first = self.harness.rescue_branches()

        second = self.harness.refresh()
        self.assertTrue(second.success, second.error)

        self.assertEqual(len(branches_after_first), 1)
        self.assertEqual(self.harness.rescue_branches(), branches_after_first)
        self.assertIsNone(second.rescue)

    def test_repeated_identical_artifact_is_not_archived_twice(self) -> None:
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        first = self.harness.refresh()
        self.assertTrue(first.success, first.error)
        after_first = self.harness.archived_files()

        # The service regenerates the very same artifact before the next refresh.
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        second = self.harness.refresh()

        self.assertTrue(second.success, second.error)
        self.assertEqual(self.harness.archived_files(), after_first)
        self.assertTrue(second.rescue.archived[0].deduplicated)
        self.assertEqual(
            second.rescue.archived[0].archived_path, first.rescue.archived[0].archived_path
        )
        self.assertFalse((self.harness.checkout / OBSERVED_ARTIFACT).exists())

    def test_changed_artifact_content_is_archived_separately(self) -> None:
        self.harness.write(OBSERVED_ARTIFACT, "# first\n")
        self.harness.refresh()
        self.harness.write(OBSERVED_ARTIFACT, "# second\n")
        second = self.harness.refresh()

        self.assertTrue(second.success, second.error)
        self.assertFalse(second.rescue.archived[0].deduplicated)
        self.assertEqual(len(self.harness.archived_files()), 2)

    def test_archive_index_survives_a_corrupt_file(self) -> None:
        self.harness.archive.mkdir(parents=True, exist_ok=True)
        (self.harness.archive / "archive-index.json").write_text("{not json", encoding="utf-8")

        index = ArchiveIndex(self.harness.archive)
        self.assertIsNone(index.lookup("a", "b"))

        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        result = self.harness.refresh()
        self.assertTrue(result.success, result.error)


class TestDivergedBranch(RefreshSelfHealTestCase):
    def test_local_commits_on_main_are_preserved_before_realignment(self) -> None:
        expected = self.harness.advance_upstream(2)
        self.harness.write("local_only.py", "print('local')\n")
        _git(self.harness.checkout, "add", "local_only.py")
        _git(self.harness.checkout, "commit", "-m", "local commit on main")
        local_head = _git(self.harness.checkout, "rev-parse", "HEAD")

        result = self.harness.refresh()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.new_head, expected)
        self.assertIsNotNone(result.diverged_branch)
        self.assertEqual(
            _git(self.harness.checkout, "rev-parse", result.diverged_branch), local_head
        )
        self.assertEqual(self.harness.status(), "")


class TestReporting(RefreshSelfHealTestCase):
    def test_result_reports_rescue_archive_heads_and_services(self) -> None:
        previous = _git(self.harness.checkout, "rev-parse", "HEAD")
        expected = self.harness.advance_upstream(2)
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")
        self.harness.write("experiment.py", "print('work')\n")

        result = self.harness.refresh(
            service_restarter=lambda: {"agent-api": "restarted", "nexus-mailbox": "restarted"}
        )

        payload = result.to_dict()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["previous_head"], previous)
        self.assertEqual(payload["new_head"], expected)
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["services"]["agent-api"], "restarted")
        self.assertTrue(payload["rescue"]["rescue_branch"].startswith("nexus/rescue/"))
        self.assertTrue(payload["rescue"]["rescue_commit"])
        self.assertEqual(
            [entry["repo_path"] for entry in payload["rescue"]["archived"]], [OBSERVED_ARTIFACT]
        )
        self.assertIn("verify", payload["steps"])

        summary = "\n".join(result.summary_lines())
        self.assertIn("refresh: ok", summary)
        self.assertIn(OBSERVED_ARTIFACT, summary)
        self.assertIn("service agent-api: restarted", summary)

    def test_services_are_not_restarted_when_the_update_fails(self) -> None:
        restarted: list[str] = []
        _git(self.harness.checkout, "remote", "set-url", "origin", str(self.harness.root / "gone"))

        result = self.harness.refresh(
            service_restarter=lambda: restarted.append("called") or {}
        )

        self.assertFalse(result.success)
        self.assertEqual(restarted, [])


class TestCheckoutInspection(RefreshSelfHealTestCase):
    def test_inspection_reports_branch_changes_and_behind_count(self) -> None:
        self.harness.advance_upstream(3)
        _git(self.harness.checkout, "fetch", "origin", "main")
        self.harness.write("agent.py", "VERSION = 1\nX = 1\n")
        self.harness.write(OBSERVED_ARTIFACT, "# generated\n")

        state = inspect_checkout(self.harness.checkout)

        self.assertEqual(state.branch, "main")
        self.assertFalse(state.clean)
        self.assertEqual(state.tracked, ("agent.py",))
        self.assertEqual(state.untracked, (OBSERVED_ARTIFACT,))
        self.assertEqual(state.behind, 3)
        self.assertEqual(state.ahead, 0)

    def test_paths_with_spaces_survive_inspection(self) -> None:
        self.harness.write("notes/my scratch file.txt", "keep\n")
        state = inspect_checkout(self.harness.checkout)
        self.assertEqual(state.untracked, ("notes/my scratch file.txt",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
