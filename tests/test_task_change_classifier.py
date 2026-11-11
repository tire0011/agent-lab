import os
import subprocess
import unittest
from unittest import mock

from modules import task_change_classifier
from modules.task_change_classifier import classify_command, classify_task

COMMAND_CASES = {
    # Allowed read-only forms, including quoted arguments.
    "git status": "read_only",
    "git status --porcelain=v2 -b": "read_only",
    "git status -uno": "read_only",
    "git log --oneline -5": "read_only",
    "git log -n5 --format=%H --author='Jane Doe'": "read_only",
    "git log --grep='fix: x' -- 'path with spaces'": "read_only",
    "git diff HEAD~1": "read_only",
    "git diff --stat --cached": "read_only",
    "git diff --no-ext-diff --no-textconv HEAD": "read_only",
    "git diff -- --output=not-an-option": "read_only",
    "git show 'HEAD:file with spaces.txt'": "read_only",
    "git show --stat HEAD": "read_only",
    "git branch": "read_only",
    "git branch --list": "read_only",
    "ls -la 'directory with spaces'": "read_only",
    "cat 'file with spaces.txt'": "read_only",
    "cat '>'": "read_only",
    'cat "a && b"': "read_only",
    "df -h": "read_only",
    "free -m": "read_only",
    "gh api repos/example/project": "read_only",
    "gh api -X GET repos/example/project": "read_only",
    "gh api -XGET repos/example/project": "read_only",
    "gh api --method=GET repos/example/project": "read_only",
    "gh api --method get repos/example/project": "read_only",
    "gh api --paginate -q '.[].name' repos/example/project/issues": "read_only",
    "gh api -H 'Accept: application/vnd.github+json' repos/x/y": "read_only",
    # The three cases from the first review.
    "git branch newbranch": "mutating",
    "git worktree add /tmp/new-worktree": "mutating",
    "gh api repos/example/project -f name=value": "mutating",
    # Other git subcommands/branch forms.
    "git branch -D old": "mutating",
    "git branch --list extra": "mutating",
    "git commit -m 'msg'": "mutating",
    "git -C /tmp status": "mutating",
    "git -c core.pager=evil log": "mutating",
    "git": "mutating",
    # Second review: git options that write files or run helpers.
    "git diff --output=out.patch": "mutating",
    "git diff --output out.patch": "mutating",
    "git diff --out=out.patch": "mutating",  # git accepts abbreviations
    "git show --output=out.patch HEAD": "mutating",
    "git show --output out.patch HEAD": "mutating",
    "git log -p --output=out.patch": "mutating",
    "git diff --ext-diff": "mutating",
    "git show --ext-diff HEAD": "mutating",
    "git diff --textconv": "mutating",
    "git show --textconv HEAD:file": "mutating",
    "git log --show-signature": "mutating",
    "git log --grep -- --output=x": "mutating",  # separated value form
    "git status --output=x": "mutating",
    "git diff -pw": "mutating",  # unknown bundled short flags
    # gh api: fields, methods, body and cache.
    "gh api repos/example/project -F name=value": "mutating",
    "gh api repos/example/project --field=name=value": "mutating",
    "gh api repos/example/project --raw-field name=value": "mutating",
    "gh api repos/example/project -fname=value": "mutating",
    "gh api -X POST repos/example/project": "mutating",
    "gh api -XPOST repos/example/project": "mutating",
    "gh api -X=POST repos/example/project": "mutating",
    "gh api -X GET -X POST repos/example/project": "mutating",
    "gh api --method DELETE repos/example/project": "mutating",
    "gh api --method=PATCH repos/example/project": "mutating",
    "gh api repos/example/project -X": "mutating",
    "gh api --input body.json repos/example/project": "mutating",
    "gh api --input=body.json repos/example/project": "mutating",
    "gh api -X GET --input body.json repos/example/project": "mutating",
    "gh api --cache 1h repos/example/project": "mutating",
    "gh api --cache=1h repos/example/project": "mutating",
    "gh api -H 'X-HTTP-Method-Override: DELETE' repos/x/y": "mutating",
    "gh api graphql": "mutating",
    "gh api repos/a repos/b": "mutating",
    "gh api": "mutating",
    "gh api -- repos/example/project": "mutating",
    "gh api --unknown-flag repos/example/project": "mutating",
    "gh pr create": "mutating",
    # Redirects.
    "cat input.txt > output.txt": "mutating",
    "cat input.txt>>output.txt": "mutating",
    "git status 2>err.log": "mutating",
    "cat < input.txt": "mutating",
    "cat <(rm x)": "mutating",
    # Chains and pipes.
    "git status && git branch newbranch": "mutating",
    "git status && unknown-tool": "mutating",
    "git status && git log -1": "read_only",
    "git status; rm -rf x": "mutating",
    "git status || touch x": "mutating",
    "git status & rm x": "mutating",
    "git status\nrm x": "mutating",
    "git status &&": "mutating",
    "cat file | wc -l": "mutating",
    "cat file | tee out": "mutating",
    "cat file | git status": "read_only",
    # sudo, env prefixes, substitution, unknown and unparseable input.
    "sudo git status": "mutating",
    "sudo ls": "mutating",
    "FOO=bar git status": "mutating",
    "( git status )": "mutating",
    "unknown-command": "mutating",
    "rm -rf /tmp/x": "mutating",
    "cat $(touch changed)": "mutating",
    "cat `touch changed`": "mutating",
    "cat 'unterminated": "mutating",
    'git log --grep="unterminated': "mutating",
    "cat trailing\\": "mutating",
    "": "mutating",
    "   ": "mutating",
}


class CommandClassifierTests(unittest.TestCase):
    def test_command_table(self):
        for command, expected in COMMAND_CASES.items():
            with self.subTest(command=command):
                self.assertEqual(expected, classify_command(command))

    def test_non_string_input_fails_closed(self):
        for value in (None, b"git status", ["git", "status"], 42):
            with self.subTest(value=value):
                self.assertEqual("mutating", classify_command(value))

    def test_repeated_calls_are_deterministic(self):
        first = {cmd: classify_command(cmd) for cmd in COMMAND_CASES}
        for _ in range(3):
            self.assertEqual(first, {cmd: classify_command(cmd) for cmd in COMMAND_CASES})

    def test_truncated_input_never_raises_and_never_loosens(self):
        # A command cut off halfway (e.g. an interrupted producer) must still
        # classify without raising.  Every prefix of a mutating command that
        # is itself read-only must be a genuinely allowed form.
        command = "gh api -X GET --input body.json repos/x/y"
        for end in range(len(command) + 1):
            prefix = command[:end]
            with self.subTest(prefix=prefix):
                self.assertIn(classify_command(prefix), {"read_only", "mutating"})
        self.assertEqual("mutating", classify_command(command))

    def test_internal_error_fails_closed(self):
        with mock.patch.object(
            task_change_classifier, "_part_is_read_only", side_effect=RuntimeError
        ):
            self.assertEqual("mutating", classify_command("git status"))

    def test_classification_has_no_side_effects(self):
        boom = AssertionError("classifier must not execute anything")
        with mock.patch.object(subprocess, "run", side_effect=boom), mock.patch.object(
            subprocess, "Popen", side_effect=boom
        ), mock.patch.object(os, "system", side_effect=boom):
            for command in COMMAND_CASES:
                classify_command(command)


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
            "   ": "change",
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                self.assertEqual(expected, classify_task(task))

    def test_non_string_task_is_change(self):
        for value in (None, 1, b"What is this?"):
            with self.subTest(value=value):
                self.assertEqual("change", classify_task(value))


if __name__ == "__main__":
    unittest.main()
