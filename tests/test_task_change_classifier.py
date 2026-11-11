import unittest

from modules.task_change_classifier import classify_command, classify_task


class CommandClassifierTests(unittest.TestCase):
    def test_command_table(self):
        cases = {
            "git status": "read_only",
            "git log --oneline -5": "read_only",
            "git diff HEAD~1": "read_only",
            "git show 'HEAD:file with spaces.txt'": "read_only",
            "git branch": "read_only",
            "git branch --list": "read_only",
            "ls -la 'directory with spaces'": "read_only",
            "cat 'file with spaces.txt'": "read_only",
            "cat '>'": "read_only",
            "df -h": "read_only",
            "free -m": "read_only",
            "gh api repos/example/project": "read_only",
            "gh api -X GET repos/example/project": "read_only",
            "gh api --method=GET repos/example/project": "read_only",
            # Cases specifically responsible for the earlier review block.
            "git branch newbranch": "mutating",
            "git worktree add /tmp/new-worktree": "mutating",
            "gh api repos/example/project -f name=value": "mutating",
            "gh api repos/example/project -F name=value": "mutating",
            "gh api -X POST repos/example/project": "mutating",
            "gh api --method DELETE repos/example/project": "mutating",
            "cat input.txt > output.txt": "mutating",
            "cat input.txt>>output.txt": "mutating",
            "git status && git branch newbranch": "mutating",
            "git status && git log -1": "read_only",
            "cat file | wc -l": "mutating",
            "cat file | git status": "read_only",
            "sudo git status": "mutating",
            "unknown-command": "mutating",
            "cat $(touch changed)": "mutating",
            "cat 'unterminated": "mutating",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(expected, classify_command(command))


class TaskClassifierTests(unittest.TestCase):
    def test_task_table(self):
        cases = {
            "What does this module do?": "question",
            "Explain why the tests are failing": "question",
            "Check the current git status": "question",
            "Implement the classifier": "change",
            "Can you fix this bug?": "change",
            "The classifier is broken": "change",
            "": "change",
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                self.assertEqual(expected, classify_task(task))


if __name__ == "__main__":
    unittest.main()
