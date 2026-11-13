"""#679: tolerate guessed SEARCH text only when the target has no content.

Covers the conversion itself, the count's propagation through
``_response_patch`` and the worker's patch rounds into the local worker log
and the replay record, and failure modes: rejected rounds, crashes, partial
answers and running the same thing twice.
"""

import asyncio
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import local_shadow_replay as lsr
from modules.local_coding_worker import (
    LocalCodingWorker, PromptPlan, Subtask, _response_patch, apply_and_check,
)
from modules.local_edit_format import EditDiff, EditFormatError, edits_to_diff
from modules.run_store import init_db
from tests.test_issue_637_local_shadow_replay import FakeWorker, ReplayFixture

SR_ENV = {"LOCAL_EDIT_FORMAT": "search_replace", "LOCAL_MIN_USEFUL_PATCH_TOKENS": "0"}


def block(path: str, search: str, replacement: str) -> str:
    return f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replacement}>>>>>>> REPLACE\n"


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)


class LenientCreateTests(GitRepoCase):
    # (a)
    def test_missing_path_with_nonempty_search_creates_new_file(self):
        result = edits_to_diff(block("new.py", "guessed old text\n", "VALUE = 1\n"), str(self.repo))
        self.assertIsInstance(result, EditDiff)
        self.assertEqual(1, result.lenient_creates)
        self.assertIn("new file mode", result)
        self.assertIn("--- /dev/null", result)
        apply_and_check(result, str(self.repo), 10)
        self.assertEqual("VALUE = 1\n", (self.repo / "new.py").read_text())

    # (b)
    def test_existing_empty_file_with_nonempty_search_uses_replacement(self):
        self.commit({"empty.py": ""})
        result = edits_to_diff(block("empty.py", "guessed\n", "VALUE = 2\n"), str(self.repo))
        self.assertEqual(1, result.lenient_creates)
        apply_and_check(result, str(self.repo), 10)
        self.assertEqual("VALUE = 2\n", (self.repo / "empty.py").read_text())

    # (c)
    def test_nonempty_file_with_missing_search_remains_strict(self):
        self.commit({"value.txt": "actual\n"})
        with self.assertRaisesRegex(EditFormatError, "SEARCH was not found"):
            edits_to_diff(block("value.txt", "guessed\n", "new\n"), str(self.repo))

    # (d)
    def test_empty_search_on_existing_nonempty_file_remains_error(self):
        self.commit({"value.txt": "actual\n"})
        with self.assertRaisesRegex(EditFormatError, "empty SEARCH but the path already exists"):
            edits_to_diff(block("value.txt", "", "new\n"), str(self.repo))

    def test_empty_search_on_missing_file_is_a_normal_create_not_lenient(self):
        result = edits_to_diff(block("new.py", "", "VALUE = 3\n"), str(self.repo))
        self.assertEqual(0, result.lenient_creates)
        self.assertIn("new file mode", result)

    def test_rules_for_file_created_by_earlier_block_are_unchanged(self):
        created = block("new.py", "", "VALUE = 1\n")
        with self.assertRaisesRegex(EditFormatError, "empty SEARCH"):
            edits_to_diff(created + block("new.py", "", "OTHER = 2\n"), str(self.repo))
        # The earlier block gave the file content, so a guessed SEARCH is strict again.
        with self.assertRaisesRegex(EditFormatError, "SEARCH was not found"):
            edits_to_diff(created + block("new.py", "guessed\n", "OTHER = 2\n"), str(self.repo))
        # A matching follow-up edit on a lenient-created file still works and counts once.
        lenient = block("new.py", "guessed\n", "VALUE = 1\n")
        result = edits_to_diff(lenient + block("new.py", "VALUE = 1\n", "VALUE = 9\n"), str(self.repo))
        self.assertEqual(1, result.lenient_creates)
        self.assertIn("+VALUE = 9", result)

    # (e)
    def test_lenient_create_count_covers_multiple_blocks(self):
        answer = (block("one.py", "guess one\n", "ONE = 1\n")
                  + block("two.py", "guess two\n", "TWO = 2\n")
                  + block("three.py", "", "THREE = 3\n"))
        self.assertEqual(2, edits_to_diff(answer, str(self.repo)).lenient_creates)

    def test_conversion_is_side_effect_free_and_repeatable(self):
        answer = block("new.py", "guessed\n", "VALUE = 1\n")
        first = edits_to_diff(answer, str(self.repo))
        second = edits_to_diff(answer, str(self.repo))
        self.assertEqual(first, second)
        self.assertEqual(first.lenient_creates, second.lenient_creates)
        self.assertFalse((self.repo / "new.py").exists())

    def test_failing_later_block_creates_nothing(self):
        # Halfway failure: block 1 would be a lenient create, block 2 is strict and fails.
        self.commit({"value.txt": "actual\n"})
        answer = block("new.py", "guessed\n", "VALUE = 1\n") + block("value.txt", "guessed\n", "x\n")
        with self.assertRaisesRegex(EditFormatError, "SEARCH was not found"):
            edits_to_diff(answer, str(self.repo))
        self.assertFalse((self.repo / "new.py").exists())
        self.assertEqual("", subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                            capture_output=True, text=True, check=True).stdout)


class ResponsePatchTests(GitRepoCase):
    def test_search_replace_answer_keeps_count(self):
        with patch.dict(os.environ, SR_ENV):
            produced = _response_patch(block("new.py", "guessed\n", "V = 1\n"), str(self.repo), 10, None)
        self.assertIsInstance(produced, EditDiff)
        self.assertEqual(1, produced.lenient_creates)

    def test_raw_diff_and_empty_answers_carry_zero(self):
        self.commit({"a.txt": "old\n"})
        diff = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                "@@ -1 +1 @@\n-old\n+new\n")
        for fmt in ("search_replace", "diff"):
            with patch.dict(os.environ, {**SR_ENV, "LOCAL_EDIT_FORMAT": fmt}):
                produced = _response_patch(diff, str(self.repo), 10, None)
            self.assertIsInstance(produced, EditDiff)
            self.assertEqual(0, produced.lenient_creates)
            self.assertEqual(diff, produced)
        with patch.dict(os.environ, {**SR_ENV, "LOCAL_EDIT_FORMAT": "diff"}):
            empty = _response_patch("no patch here", str(self.repo), 10, None)
        self.assertEqual(("", 0), (empty, empty.lenient_creates))

    def test_plain_string_from_edits_to_diff_is_wrapped(self):
        with patch.dict(os.environ, SR_ENV), \
                patch("modules.local_coding_worker.edits_to_diff", return_value="plain"):
            produced = _response_patch(block("x.py", "a\n", "b\n"), str(self.repo), 10, None)
        self.assertEqual(("plain", 0), (produced, produced.lenient_creates))


class RecordingLogger:
    def __init__(self):
        self.events = []

    def info(self, phase, event, message, **fields):
        self.events.append((event, fields))

    warning = info

    def fields(self, event):
        return [fields for name, fields in self.events if name == event]


class ScriptedClient:
    model = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    async def context_size(self):
        return 4096

    async def count_tokens(self, text):
        return max(1, len(text) // 4)

    async def complete(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class WorkerPropagationTests(GitRepoCase):
    def setUp(self):
        super().setUp()
        self.commit({"README.md": "repo\n"})

    def run_worker(self, worker, task="Create new_module.py with VALUE = 1"):
        log = RecordingLogger()
        with patch.dict(os.environ, SR_ENV):
            result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                                timeout_seconds=30, experiment_logger=log)
        return result, log

    def test_applied_lenient_create_reaches_worker_and_log(self):
        worker = LocalCodingWorker(client=ScriptedClient(block("new_module.py", "guess\n", "VALUE = 1\n")),
                                   max_tokens=512)
        result, log = self.run_worker(worker)
        self.assertTrue(result.success, result.error)
        self.assertEqual("VALUE = 1\n", (self.repo / "new_module.py").read_text())
        self.assertEqual(1, worker.lenient_creates)
        self.assertEqual([1], [f["lenient_creates"] for f in log.fields("local_task_split_result")])
        applied = [f for f in log.fields("local_patch_round") if f.get("outcome", "applied") == "applied"
                   and f.get("lenient_creates")]
        self.assertEqual(1, len(applied))

    def test_rejected_round_is_not_counted(self):
        # Round 1: lenient create whose Python does not compile -> rolled back.
        # Round 2: a correct lenient create. Only the applied one counts.
        client = ScriptedClient(block("new_module.py", "guess\n", "VALUE = (\n"),
                                block("new_module.py", "guess\n", "VALUE = 1\n"))
        worker = LocalCodingWorker(client=client, max_tokens=512)
        result, log = self.run_worker(worker)
        self.assertTrue(result.success, result.error)
        self.assertEqual(2, len(client.prompts))
        self.assertEqual(1, worker.lenient_creates)
        self.assertEqual([1], [f["lenient_creates"] for f in log.fields("local_task_split_result")])

    def test_all_rounds_failing_reports_zero(self):
        client = ScriptedClient(block("new_module.py", "guess\n", "VALUE = (\n"))
        worker = LocalCodingWorker(client=client, max_tokens=512)
        result, log = self.run_worker(worker)
        self.assertFalse(result.success)
        self.assertFalse((self.repo / "new_module.py").exists())
        self.assertEqual(0, worker.lenient_creates)
        self.assertEqual([0], [f["lenient_creates"] for f in log.fields("local_task_split_result")])

    def test_model_crash_still_logs_count(self):
        worker = LocalCodingWorker(client=ScriptedClient(RuntimeError("server died")), max_tokens=512)
        try:
            self.run_worker(worker)
        except RuntimeError:
            pass
        self.assertEqual(0, worker.lenient_creates)

    def test_second_run_resets_count(self):
        answer = block("new_module.py", "guess\n", "VALUE = 1\n")
        worker = LocalCodingWorker(client=ScriptedClient(answer), max_tokens=512)
        self.assertTrue(self.run_worker(worker)[0].success)
        subprocess.run(["git", "clean", "-qfd"], cwd=self.repo, check=True)
        result, log = self.run_worker(worker)
        self.assertTrue(result.success, result.error)
        self.assertEqual(1, worker.lenient_creates)
        self.assertEqual([1], [f["lenient_creates"] for f in log.fields("local_task_split_result")])


class SubtaskPromptTests(GitRepoCase):
    def plan_prompt(self, files):
        class Client:
            timeout = None

        worker = LocalCodingWorker(client=Client(), max_tokens=1024)
        parent = PromptPlan(
            prompt="", n_ctx=4096, n_ctx_source="test", budget_tokens=3000,
            prompt_tokens=0, token_source="estimate", essential="overall task",
        )
        return asyncio.run(worker._subtask_plan(
            parent, Subtask("create it", tuple(files)), 1, 2, [], str(self.repo)
        )).prompt

    # (f)
    def test_missing_subtask_target_is_named_as_new_file(self):
        self.commit({"modules/existing.py": "X = 1\n"})
        prompt = self.plan_prompt(["modules/new_module.py", "modules/existing.py"])
        self.assertIn("NEW FILE: modules/new_module.py (use an empty SEARCH)", prompt)
        self.assertNotIn("NEW FILE: modules/existing.py", prompt)

    def test_existing_targets_get_no_new_file_line(self):
        self.commit({"modules/existing.py": "X = 1\n"})
        self.assertNotIn("NEW FILE:", self.plan_prompt(["modules/existing.py"]))


class LenientWorker(FakeWorker):
    def __init__(self, calc=None, *, raises=False, lenient_creates=0):
        super().__init__(calc, raises=raises)
        self.lenient_creates = lenient_creates


class ReplayRecordTests(unittest.TestCase):
    def setUp(self):
        self.fx = ReplayFixture(self)

    def test_count_is_stored_on_the_replay_record(self):
        row = self.fx.replay(LenientWorker(None, lenient_creates=2))
        self.assertEqual(2, row["lenient_creates"])
        self.assertEqual(2, self.fx.rows()[0]["lenient_creates"])

    def test_count_is_stored_when_the_worker_crashes(self):
        row = self.fx.replay(LenientWorker(None, raises=True, lenient_creates=1))
        self.assertEqual(lsr.STATUS_ERROR, row["status"])
        self.assertEqual(1, row["lenient_creates"])

    def test_worker_without_count_or_with_bad_value_stores_zero(self):
        self.assertEqual(0, self.fx.replay(FakeWorker(None))["lenient_creates"])
        self.fx.add_experiment("exp-2", merged=self.fx.merged, base=self.fx.base)
        bad = LenientWorker(None)
        bad.lenient_creates = "n/a"
        self.assertEqual(0, self.fx.replay(bad, experiment_id="exp-2")["lenient_creates"])

    def test_second_replay_of_same_case_does_not_change_record(self):
        self.fx.replay(LenientWorker(None, lenient_creates=2))
        self.assertIsNone(self.fx.replay(LenientWorker(None, lenient_creates=5)))
        rows = self.fx.rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["lenient_creates"])

    def test_schema_migration_is_idempotent(self):
        init_db(self.fx.db_path).close()
        init_db(self.fx.db_path).close()
        conn = sqlite3.connect(self.fx.db_path)
        try:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(local_replays)")]
        finally:
            conn.close()
        self.assertEqual(1, columns.count("lenient_creates"))


if __name__ == "__main__":
    unittest.main()
