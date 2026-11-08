"""Deterministic runtime (interpreter) execution for the local Nexus controller.

The persistent controller can run shell commands, but a shell command alone has
no deterministic representation of *this* project's Python runtime: an LLM has
to guess an interpreter name, and a guess such as ``python`` fails with exit
code 127 whenever only ``python3`` or a virtualenv interpreter exists.

This module provides two building blocks used by
``modules.task_orchestrator``:

* :func:`resolve_python_interpreter` / :func:`build_python_command` — a typed
  Python execution capability whose interpreter comes from the running Nexus
  environment (explicit configuration, active/project virtualenv or
  ``sys.executable``), never from an LLM guess.
* :func:`classify_command_failure` / :func:`corrected_command` — bounded,
  deterministic recovery for command-resolution failures (exit code 127 /
  ``command not found``), so one corrected retry can be performed without
  another LLM inference.

No host-specific executable literal is ever exposed to the controller prompt;
the controller expresses a script/module plus arguments and Nexus resolves the
executable itself.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

INTERPRETER_ENV_VAR = "NEXUS_PYTHON_EXECUTABLE"

_VENV_RELATIVE_INTERPRETERS = (
    ("bin", "python"),
    ("bin", "python3"),
    ("Scripts", "python.exe"),
)

# Executable names the controller may plausibly emit for the project runtime.
_PYTHON_ALIASES = ("python", "python3")
_PIP_ALIASES = ("pip", "pip3")

_EXIT_CODE_RE = re.compile(r"(?im)^[^\w]*exit[ _]code[^\w]*[:\s]\s*[^\w]*(\d+)")
# A plain, unquoted leading executable token. Deliberately excludes quotes and
# shell metacharacters so a correction can splice the replacement in without
# reinterpreting the rest of the command line.
_LEADING_TOKEN_RE = re.compile(r"^([\w./+@:-]+)(?=\s|$)")
_NOT_FOUND_RE = re.compile(
    r"(?im)^(?:.*?:\s*)?(?:line\s+\d+:\s*)?([\w.\-/+]+):\s*(?:command\s+not\s+found|not\s+found)\s*$"
)


class RuntimeExecutionError(ValueError):
    """Raised when a typed runtime execution request is not usable."""


@dataclass(slots=True)
class PythonCommand:
    """A deterministic Python invocation ready for the trusted command runner."""

    interpreter: str
    argv: list[str]
    command: str
    target: str


@dataclass(slots=True)
class CommandResolutionFailure:
    """A command that failed because its executable could not be resolved."""

    executable: str
    exit_code: int | None
    detail: str


def resolve_python_interpreter(repo_root: str | os.PathLike[str] | None = None) -> str:
    """Return the interpreter Nexus should use for project Python execution.

    Resolution order (first usable wins):

    1. ``NEXUS_PYTHON_EXECUTABLE`` when it points at an executable file.
    2. The active virtualenv (``VIRTUAL_ENV``).
    3. The project virtualenv under ``repo_root`` (``.venv``).
    4. ``sys.executable`` of the running Nexus process.
    5. ``python3`` as a last resort when Python is embedded without an
       ``sys.executable`` (never a controller guess).
    """
    configured = os.environ.get(INTERPRETER_ENV_VAR, "").strip()
    if configured and _is_executable(configured):
        return configured

    virtual_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        candidate = _venv_interpreter(Path(virtual_env))
        if candidate:
            return candidate

    if repo_root:
        candidate = _venv_interpreter(Path(repo_root) / ".venv")
        if candidate:
            return candidate

    if sys.executable and _is_executable(sys.executable):
        return sys.executable
    return "python3"


def build_python_command(
    *,
    script: str | None = None,
    module: str | None = None,
    args: Sequence[str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    interpreter: str | None = None,
) -> PythonCommand:
    """Build a deterministic ``python`` invocation for the command runner.

    Exactly one of ``script`` or ``module`` must be given. Arguments are
    preserved verbatim and shell-quoted, so no argument can inject additional
    shell syntax.
    """
    script = (script or "").strip()
    module = (module or "").strip()
    if bool(script) == bool(module):
        raise RuntimeExecutionError("python_run requires exactly one of script or module")

    resolved = (interpreter or "").strip() or resolve_python_interpreter(repo_root)
    argv = [resolved]
    if module:
        if not re.fullmatch(r"[A-Za-z_][\w.]*", module):
            raise RuntimeExecutionError(f"invalid python module {module!r}")
        argv.extend(["-m", module])
        target = f"-m {module}"
    else:
        if Path(script).is_absolute() or ".." in Path(script).parts:
            raise RuntimeExecutionError(
                "python_run script must be a repository-relative path without '..'"
            )
        argv.append(script)
        target = script
    argv.extend(_normalized_args(args))
    return PythonCommand(
        interpreter=resolved,
        argv=argv,
        command=" ".join(shlex.quote(part) for part in argv),
        target=target,
    )


def classify_command_failure(output: str | None) -> CommandResolutionFailure | None:
    """Classify command runner output as a command-resolution failure.

    Returns ``None`` for successful commands and for ordinary failures (a test
    that fails, a non-zero application exit code, ...) so only genuine
    executable-resolution problems trigger recovery.
    """
    text = output or ""
    if not text.strip():
        return None
    exit_code = _exit_code(text)
    match = _NOT_FOUND_RE.search(text)
    if match is None:
        return None
    if exit_code is not None and exit_code != 127:
        return None
    executable = match.group(1).strip()
    if not executable:
        return None
    return CommandResolutionFailure(
        executable=executable,
        exit_code=exit_code,
        detail=match.group(0).strip()[:500],
    )


def corrected_command(
    command: str,
    *,
    missing_executable: str,
    interpreter: str,
) -> tuple[str, str] | None:
    """Return ``(corrected_command, mapping)`` for a known executable mapping.

    Only the leading executable token is replaced; the remainder of the command
    line is preserved verbatim, so shell operators (``&&``, ``|``, ``>``, ...),
    quoting and redirection in the tail keep their original meaning. Unknown
    executables, and commands whose leading token is not the unresolved
    executable, return ``None`` so the caller can report a controlled
    missing-capability observation instead of guessing.
    """
    command = (command or "").strip()
    if not command:
        return None
    match = _LEADING_TOKEN_RE.match(command)
    if match is None:
        return None
    leading = match.group(1)
    if Path(leading).name != Path(missing_executable).name:
        return None

    name = Path(leading).name
    if name in _PYTHON_ALIASES:
        replacement = shlex.quote(interpreter)
        mapping = f"{name} -> configured python interpreter"
    elif name in _PIP_ALIASES:
        replacement = f"{shlex.quote(interpreter)} -m pip"
        mapping = f"{name} -> configured python interpreter -m pip"
    else:
        return None

    rebuilt = replacement + command[match.end(1):]
    if rebuilt == command:
        return None
    return rebuilt, mapping


def equivalent_command(
    command: str,
    *,
    missing_executable: str,
) -> tuple[str, str] | None:
    """Return a cheap, already-installed equivalent for a missing CLI.

    These mappings are deliberately capability based and bounded.  They are
    only used when the replacement is present, and only rewrite the leading
    executable, so a failed command is never blindly repeated.  More complex
    gaps remain visible to the controller instead of being guessed at here.
    """
    command = (command or "").strip()
    match = _LEADING_TOKEN_RE.match(command)
    if match is None or Path(match.group(1)).name != Path(missing_executable).name:
        return None
    name = Path(missing_executable).name
    if name == "rg" and shutil.which("grep"):
        # grep needs recursive traversal explicitly; it accepts rg's common
        # -n/-C options and extended regex is the closest portable equivalent.
        replacement = "grep -R -E"
        return replacement + command[match.end(1):], "repository text search: rg -> grep -R -E"
    return None


def _normalized_args(args: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for arg in args or ():
        if isinstance(arg, bool) or not isinstance(arg, (str, int, float)):
            raise RuntimeExecutionError("python_run arguments must be strings")
        normalized.append(str(arg))
    return normalized


def _venv_interpreter(venv_root: Path) -> str | None:
    for relative in _VENV_RELATIVE_INTERPRETERS:
        candidate = venv_root.joinpath(*relative)
        if _is_executable(str(candidate)):
            return str(candidate)
    return None


def _is_executable(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False


def exit_code_of(text: str) -> int | None:
    """Public alias of :func:`_exit_code` for callers outside this module.

    The tolerant pattern is the only one that matches what ``process_command``
    actually renders, so consumers share it instead of writing their own.
    """
    return _exit_code(text)


def _exit_code(text: str) -> int | None:
    match = _EXIT_CODE_RE.search(text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
