from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.runtime_execution import (
    INTERPRETER_ENV_VAR,
    RuntimeExecutionError,
    build_python_command,
    classify_command_failure,
    corrected_command,
    equivalent_command,
    resolve_python_interpreter,
)


class EquivalentCommandTests(unittest.TestCase):
    def test_rg_has_bounded_grep_equivalent_when_available(self):
        with patch("modules.runtime_execution.shutil.which", return_value="/usr/bin/grep"):
            recovered = equivalent_command(
                'rg -n -C 4 "VERSION" agent.py tests', missing_executable="rg"
            )
        self.assertEqual(recovered, (
            'grep -R -E -n -C 4 "VERSION" agent.py tests',
            "repository text search: rg -> grep -R -E",
        ))


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ResolveInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._saved = {
            key: os.environ.pop(key, None) for key in (INTERPRETER_ENV_VAR, "VIRTUAL_ENV")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_defaults_to_running_interpreter(self):
        self.assertEqual(resolve_python_interpreter(self.repo), sys.executable)

    def test_project_virtualenv_is_preferred_over_sys_executable(self):
        interpreter = _make_executable(self.repo / ".venv" / "bin" / "python")
        self.assertEqual(resolve_python_interpreter(self.repo), str(interpreter))

    def test_active_virtualenv_is_preferred_over_project_virtualenv(self):
        _make_executable(self.repo / ".venv" / "bin" / "python")
        active = _make_executable(Path(self.tmp.name) / "active" / "bin" / "python")
        os.environ["VIRTUAL_ENV"] = str(active.parent.parent)
        self.assertEqual(resolve_python_interpreter(self.repo), str(active))

    def test_explicit_configuration_wins(self):
        configured = _make_executable(self.repo / "custom" / "python")
        os.environ[INTERPRETER_ENV_VAR] = str(configured)
        _make_executable(self.repo / ".venv" / "bin" / "python")
        self.assertEqual(resolve_python_interpreter(self.repo), str(configured))

    def test_missing_configured_interpreter_falls_back(self):
        os.environ[INTERPRETER_ENV_VAR] = str(self.repo / "does-not-exist")
        self.assertEqual(resolve_python_interpreter(self.repo), sys.executable)


class BuildPythonCommandTests(unittest.TestCase):
    def test_script_and_arguments_are_preserved(self):
        plan = build_python_command(
            script="agent.py",
            args=["--version", "--label", "a b"],
            interpreter="/opt/venv/bin/python",
        )
        self.assertEqual(
            plan.argv,
            ["/opt/venv/bin/python", "agent.py", "--version", "--label", "a b"],
        )
        self.assertEqual(plan.command, "/opt/venv/bin/python agent.py --version --label 'a b'")
        self.assertEqual(plan.target, "agent.py")

    def test_module_execution(self):
        plan = build_python_command(
            module="unittest",
            args=["discover", "-s", "tests"],
            interpreter="/opt/venv/bin/python",
        )
        self.assertEqual(plan.command, "/opt/venv/bin/python -m unittest discover -s tests")
        self.assertEqual(plan.target, "-m unittest")

    def test_requires_exactly_one_target(self):
        with self.assertRaises(RuntimeExecutionError):
            build_python_command(interpreter="/opt/venv/bin/python")
        with self.assertRaises(RuntimeExecutionError):
            build_python_command(script="a.py", module="unittest", interpreter="/x/python")

    def test_rejects_escaping_script_paths_and_invalid_modules(self):
        with self.assertRaises(RuntimeExecutionError):
            build_python_command(script="../secrets.py", interpreter="/x/python")
        with self.assertRaises(RuntimeExecutionError):
            build_python_command(module="unittest; rm -rf /", interpreter="/x/python")

    def test_rejects_non_string_arguments(self):
        with self.assertRaises(RuntimeExecutionError):
            build_python_command(script="a.py", args=[{"x": 1}], interpreter="/x/python")


class CommandFailureClassificationTests(unittest.TestCase):
    def test_classifies_command_not_found(self):
        failure = classify_command_failure(
            "**Exit code:** 127\n\n### stderr\n\nbash: line 1: python: command not found\n"
        )
        assert failure is not None
        self.assertEqual(failure.executable, "python")
        self.assertEqual(failure.exit_code, 127)

    def test_classifies_short_shell_form(self):
        failure = classify_command_failure("exit_code: 127\nstderr:\npython: not found")
        assert failure is not None
        self.assertEqual(failure.executable, "python")

    def test_ordinary_failure_is_not_a_resolution_failure(self):
        self.assertIsNone(
            classify_command_failure("**Exit code:** 1\n\n### stderr\n\nAssertionError\n")
        )
        self.assertIsNone(classify_command_failure("**Exit code:** 0\n\n### stdout\n\n0.2.0"))
        self.assertIsNone(classify_command_failure(""))

    def test_application_error_mentioning_not_found_keeps_its_exit_code(self):
        self.assertIsNone(
            classify_command_failure("**Exit code:** 2\n\n### stderr\n\nconfig: not found")
        )


class CorrectedCommandTests(unittest.TestCase):
    def test_python_is_mapped_to_the_configured_interpreter(self):
        mapping = corrected_command(
            "python agent.py --version",
            missing_executable="python",
            interpreter="/opt/venv/bin/python",
        )
        assert mapping is not None
        corrected, name = mapping
        self.assertEqual(corrected, "/opt/venv/bin/python agent.py --version")
        self.assertIn("python", name)

    def test_pip_is_mapped_to_module_invocation(self):
        mapping = corrected_command(
            "pip install -r requirements-test.txt",
            missing_executable="pip",
            interpreter="/opt/venv/bin/python",
        )
        assert mapping is not None
        self.assertEqual(
            mapping[0], "/opt/venv/bin/python -m pip install -r requirements-test.txt"
        )

    def test_unknown_executable_has_no_mapping(self):
        self.assertIsNone(
            corrected_command(
                "cargo build",
                missing_executable="cargo",
                interpreter="/opt/venv/bin/python",
            )
        )

    def test_failure_from_a_nested_executable_is_not_remapped(self):
        self.assertIsNone(
            corrected_command(
                "make test",
                missing_executable="python",
                interpreter="/opt/venv/bin/python",
            )
        )

    def test_identical_correction_is_rejected(self):
        self.assertIsNone(
            corrected_command(
                "python --version",
                missing_executable="python",
                interpreter="python",
            )
        )

    def test_compound_command_tail_is_preserved_verbatim(self):
        for tail in (
            "script.py && echo done",
            "script.py | tee out.txt",
            "script.py > out.txt 2>&1",
            "script.py; echo done",
            "-c 'print(\"a b\")'",
        ):
            with self.subTest(tail=tail):
                mapping = corrected_command(
                    f"python {tail}",
                    missing_executable="python",
                    interpreter="/opt/venv/bin/python",
                )
                assert mapping is not None
                self.assertEqual(mapping[0], f"/opt/venv/bin/python {tail}")

    def test_interpreter_with_spaces_is_quoted(self):
        mapping = corrected_command(
            "python script.py && echo done",
            missing_executable="python",
            interpreter="/opt/my venv/bin/python",
        )
        assert mapping is not None
        self.assertEqual(mapping[0], "'/opt/my venv/bin/python' script.py && echo done")

    def test_leading_token_that_is_not_a_plain_word_is_not_corrected(self):
        for command in ('"python" script.py', "PYTHONPATH=. python script.py"):
            with self.subTest(command=command):
                self.assertIsNone(
                    corrected_command(
                        command,
                        missing_executable="python",
                        interpreter="/opt/venv/bin/python",
                    )
                )


class SanitizedCommandTests(unittest.TestCase):
    def test_secrets_are_scrubbed_but_interpreter_paths_survive(self):
        from modules.redaction import redact, redact_secrets

        command = "/opt/venv/bin/python agent.py --token=sk-abcdefghijklmnop"
        sanitized = redact_secrets(command)
        self.assertIn("/opt/venv/bin/python agent.py", sanitized)
        self.assertNotIn("sk-abcdefghijklmnop", sanitized)
        # The persisted/API redaction still removes absolute paths.
        self.assertNotIn("/opt/venv/bin/python", redact(command))


if __name__ == "__main__":
    unittest.main()
