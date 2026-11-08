"""#597: the local coding worker corrects its patch over at most three rounds."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.experiment_handover import HANDOVER_HEADING
from modules.experiment_service.prompts import _build_direct_implementation_prompt
from modules.local_coding_worker import (
    FEEDBACK_ERROR_CHARS,
    MAX_PATCH_ROUNDS,
    LocalCodingWorker,
    patch_paths,
)

_GOOD = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n"
# Hunk header claims more lines than follow: git reports "corrupt patch".
_CORRUPT = "--- a/value.txt\n+++ b/value.txt\n@@ -1,3 +1,3 @@\n-original\n+patched\n"
_NOT_APPLYING = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-not-there\n+x\n"
# Applies cleanly, but leaves mod.py with a syntax error.
_BROKEN_PY = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = (\n"
_GOOD_PY = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
# Creates a new package file (and directory) that does not compile.
_NEW_BROKEN = (
    "diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/pkg/new.py\n"
    "@@ -0,0 +1 @@\n+def broken(:\n"
)
# Renames mod.py and breaks it, plus deletes value.txt.
_RENAME_BROKEN = (
    "diff --git a/mod.py b/renamed.py\nsimilarity index 50%\nrename from mod.py\nrename to renamed.py\n"
    "--- a/mod.py\n+++ b/renamed.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = (\n"
    "diff --git a/value.txt b/value.txt\ndeleted file mode 100644\n--- a/value.txt\n+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-original\n"
)


class _Server:
    """Fake llama-server: fixed n_ctx, a 4-bytes-per-token tokenizer, scripted answers."""

    def __init__(self, responses: list[str], *, n_ctx: int = 8192):
        self.responses = list(responses)
        self.n_ctx = n_ctx
        self.props_calls = 0
        self.calls: list[dict] = []

    async def context_size(self) -> int:
        self.props_calls += 1
        return self.n_ctx

    async def count_tokens(self, text: str) -> int:
        return self.tokens(text)

    @staticmethod
    def tokens(text: str) -> int:
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 0, **_kw) -> str:
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def rounds(self) -> list[dict]:
        return [fields for event, fields in self.events if event == "local_patch_round"]


class Issue597CorrectionLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "value.txt").write_text("original\n")
        (self.repo / "mod.py").write_text("VALUE = 1\n")
        self._git("add", ".")
        self._git("commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def _status(self) -> str:
        return self._git("status", "--porcelain", "--untracked-files=all", "--ignored")

    def _run(self, server: _Server, task: str = "Edit value.txt and mod.py"):
        log = _Logger()
        worker = LocalCodingWorker(client=server, max_tokens=1024)
        result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                            experiment_logger=log)
        return worker, result, log

    def _assert_fits(self, server: _Server, n_ctx: int):
        for call in server.calls:
            sent = server.tokens(call["system"]) + server.tokens(call["prompt"]) + call["max_tokens"]
            self.assertLessEqual(sent, n_ctx)

    def test_corrupt_diff_then_valid_diff_succeeds_in_two_rounds(self):
        server = _Server([_CORRUPT, _GOOD])
        _worker, result, log = self._run(server)

        self.assertTrue(result.success, result.error)
        self.assertEqual("patched\n", (self.repo / "value.txt").read_text())
        self.assertEqual(2, len(server.calls))
        second = server.calls[1]["prompt"]
        self.assertIn("corrupt patch", second)  # the error was fed back
        self.assertIn("-original\n+patched", second)  # with the previous diff
        self.assertIn("Goal and instructions:", second)  # on top of the same fitted prompt
        self.assertIn("--- value.txt ---\noriginal", second)
        rounds = log.rounds()
        self.assertEqual([1, 2], [r["round"] for r in rounds])
        self.assertEqual(["does_not_apply", "applied"], [r["outcome"] for r in rounds])
        self.assertIn("corrupt patch", rounds[0]["reason"])
        self.assertEqual(1, server.props_calls)  # n_ctx is not rediscovered per round

    def test_applied_diff_that_breaks_py_compile_is_reverted_and_retried(self):
        server = _Server([_BROKEN_PY, _GOOD_PY])
        _worker, result, log = self._run(server)

        self.assertTrue(result.success, result.error)
        self.assertEqual("VALUE = 2\n", (self.repo / "mod.py").read_text())
        self.assertEqual(["compile_failed", "applied"], [r["outcome"] for r in log.rounds()])
        self.assertIn("SyntaxError", server.calls[1]["prompt"])
        self.assertIn("+VALUE = (", server.calls[1]["prompt"])
        self.assertNotIn("__pycache__", self._status())  # bytecode never lands in the tree

    def test_three_bad_answers_fail_with_last_reason_and_leave_tree_clean(self):
        server = _Server([_CORRUPT, _BROKEN_PY, _NEW_BROKEN])
        _worker, result, log = self._run(server)

        self.assertFalse(result.success)
        self.assertEqual(MAX_PATCH_ROUNDS, len(server.calls))
        self.assertIn(f"after {MAX_PATCH_ROUNDS} rounds", result.error)
        self.assertIn("py_compile", result.error)  # the last reason, not the first
        self.assertNotIn("corrupt patch", result.error)
        self.assertEqual(["does_not_apply", "compile_failed", "compile_failed"],
                         [r["outcome"] for r in log.rounds()])
        self.assertEqual("", self._status())
        self.assertFalse((self.repo / "pkg").exists())  # new file and its directory removed

    def test_feedback_is_not_accumulated_across_rounds(self):
        server = _Server([_CORRUPT, _NOT_APPLYING, _GOOD])
        _worker, result, _log = self._run(server)
        self.assertTrue(result.success, result.error)
        third = server.calls[2]["prompt"]
        self.assertIn("-not-there", third)
        self.assertNotIn("corrupt patch", third)
        self.assertEqual(1, third.count("Correction (round"))

    def test_rollback_keeps_pre_existing_changes_and_undoes_rename_and_delete(self):
        (self.repo / "mod.py").write_text("VALUE = 1\n")
        (self.repo / "value.txt").write_text("original\n")
        (self.repo / "notes.txt").write_text("untracked work\n")
        (self.repo / "other.py").write_text("X = 1\n")
        self._git("add", "other.py")
        (self.repo / "other.py").write_text("X = 2  # uncommitted\n")
        before = self._status()

        server = _Server([_RENAME_BROKEN, _NEW_BROKEN, _BROKEN_PY])
        _worker, result, log = self._run(server)

        self.assertFalse(result.success)
        self.assertEqual(["compile_failed"] * 3, [r["outcome"] for r in log.rounds()])
        self.assertEqual(before, self._status())
        self.assertEqual("X = 2  # uncommitted\n", (self.repo / "other.py").read_text())
        self.assertEqual("untracked work\n", (self.repo / "notes.txt").read_text())
        self.assertEqual("VALUE = 1\n", (self.repo / "mod.py").read_text())
        self.assertEqual("original\n", (self.repo / "value.txt").read_text())
        self.assertFalse((self.repo / "renamed.py").exists())

    def test_answer_without_diff_is_retried(self):
        server = _Server(["I think you should edit value.txt.", _GOOD])
        _worker, result, log = self._run(server)
        self.assertTrue(result.success, result.error)
        self.assertEqual(["no_diff", "applied"], [r["outcome"] for r in log.rounds()])
        self.assertIn("did not return a unified diff", server.calls[1]["prompt"])

    def test_feedback_prompt_stays_within_n_ctx_and_cuts_handover_first(self):
        n_ctx = 4096
        handover = "FIRST-FINDING\n" + "".join(f"finding line {i} about the code\n" for i in range(3000))
        task = _build_direct_implementation_prompt(f"Edit value.txt\n\n{HANDOVER_HEADING}\n\n{handover}")
        # A huge corrupt diff and therefore a long error message.
        huge = _CORRUPT + "".join(f"+line {i} {'x' * 60}\n" for i in range(4000))
        server = _Server([huge, huge, _GOOD], n_ctx=n_ctx)
        worker, result, log = self._run(server, task)

        self.assertTrue(result.success, result.error)
        self._assert_fits(server, n_ctx)
        second = server.calls[1]["prompt"]
        self.assertIn("previous diff cut", second)
        self.assertIn("corrupt patch", second)
        self.assertIn("Edit value.txt", second)  # the goal is never cut
        self.assertIn("FIRST-FINDING", second)  # the handover is cut from its end
        rounds = log.rounds()
        self.assertTrue(rounds[1]["feedback_diff_cut"])
        self.assertLessEqual(rounds[1]["feedback_error_chars"], FEEDBACK_ERROR_CHARS + 20)
        self.assertLessEqual(rounds[1]["prompt_tokens"], worker.last_plan.budget_tokens)
        # Room for the feedback came out of the handover.
        self.assertLess(rounds[1]["handover_tokens_kept"], worker.last_plan.handover_tokens_kept)

    def test_failed_rollback_stops_without_another_round(self):
        from unittest import mock

        from modules import local_coding_worker as lcw

        server = _Server([_BROKEN_PY, _GOOD_PY])
        with mock.patch.object(lcw, "restore_snapshot", side_effect=lcw.RollbackError("disk gone")):
            _worker, result, log = self._run(server)
        self.assertFalse(result.success)
        self.assertEqual(1, len(server.calls))
        self.assertIn("rollback failed in round 1", result.error)
        self.assertIn("disk gone", result.error)
        self.assertEqual(["rollback_failed"], [r["outcome"] for r in log.rounds()])

    def test_long_error_is_bounded(self):
        import asyncio

        from modules.local_coding_worker import build_feedback_prompt

        server = _Server([_GOOD])
        worker, result, _log = self._run(server)
        self.assertTrue(result.success)
        fb = asyncio.run(build_feedback_prompt(
            worker.last_plan, server, round_no=2, error="E" * 10_000, previous_diff=_GOOD,
        ))
        self.assertLessEqual(fb.error_chars, FEEDBACK_ERROR_CHARS + 20)
        self.assertNotIn("E" * (FEEDBACK_ERROR_CHARS + 1), fb.prompt)
        self.assertLessEqual(fb.prompt_tokens, worker.last_plan.budget_tokens)

    def test_patch_paths_covers_renames_creations_and_deletions(self):
        self.assertEqual({"mod.py", "renamed.py", "value.txt"}, patch_paths(_RENAME_BROKEN))
        self.assertEqual({"pkg/new.py"}, patch_paths(_NEW_BROKEN))


if __name__ == "__main__":
    unittest.main()
