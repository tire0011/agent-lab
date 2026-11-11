"""Test-first core (#634, part 1 of #615): local-model tests that fail on the base tree.

:func:`generate_failing_tests` reads the issue's **Test** section and the files
named in its **Change** section, asks the local model for one unittest file under
``tests/`` and keeps only the test methods that

1. compile,
2. are collected by ``unittest`` and
3. FAIL by assertion when run against the unchanged (base) working tree.

Tests that pass on base, or that error (exception instead of assertion), crash or
time out, are dropped. A round whose candidate does not compile, cannot be
collected, or keeps no test is rejected and the model is asked again with the
error fed back, as in the #597 correction loop (at most :data:`MAX_ROUNDS` rounds,
only the last round's error is sent, bounded to :data:`FEEDBACK_ERROR_CHARS`).

When only some methods fail, the others are removed from the file; a class left
without tests is removed (or keeps ``pass`` when another class inherits from it).
The pruned file is validated again (compile, collect, every collected test fails
on base) before it is returned, so the returned ids always match the content.

The stage is optional: a missing Test section, an unreachable server or any other
error returns ``([], "")`` and never raises (``KeyboardInterrupt``/``SystemExit``
still propagate). The candidate lives in a uniquely named temporary file that test
discovery does not pick up (``tests/_nexus_test_first_*.py``); it is removed on
every exit path, existing working-tree files are never written. Not wired into
the experiment pipeline yet.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3
FEEDBACK_ERROR_CHARS = 1500  # same bound as the #597 correction loop
MAX_FILE_CHARS = 8000
MAX_CHANGE_FILES = 5
MAX_ANSWER_TOKENS = 2000
_TIMEOUT = 20.0  # seconds per subprocess (collection, one test run)
_STALE_SECONDS = 3600.0
_CANDIDATE_PREFIX = "_nexus_test_first_"

_SYSTEM = "You write small Python unittest regression tests. Return strict JSON only."

_SECTION_NAMES = r"problem|changes?|tests?|why|context|background|notes?|acceptance(?:[ \t]+criteria)?"
# Accepts "## Test", "**Test**", "**Test:**", "**Test** (`tests/x.py`) ..." and "Test: ..."
# (the format of real Nexus issues, compare experiment_evaluation._SECTION_HEADING_RE).
_SECTION_HEADING_RE = re.compile(
    rf"^[ \t]*(?:"
    rf"\#{{1,6}}[ \t]*(?:\*\*)?(?P<md>{_SECTION_NAMES})(?:[ \t]*:)?(?:\*\*)?(?=[ \t]|$|[:(])"
    rf"|\*\*(?P<bold>{_SECTION_NAMES})[ \t]*:?[ \t]*\*\*[ \t]*:?"
    rf"|(?P<colon>{_SECTION_NAMES})[ \t]*:"
    rf")(?P<rest>[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
# Other boundaries that end a section without starting a known one.
_SECTION_END_RE = re.compile(r"^[ \t]*(?:\#{1,6}[ \t]+\S[^\n]*|-{3,}[ \t]*)$", re.MULTILINE)
_PATH_RE = re.compile(r"`([^`\n]+)`|(?<![\w/.-])([\w.-]+(?:/[\w.-]+)*\.py)\b")


class _Interrupted(Exception):
    """The local model cannot be used for the rest of this call."""


def _strip_handover(text: str) -> str:
    from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING, HANDOVER_HEADING

    return text.split(HANDOVER_HEADING, 1)[0].split(EARLIER_ATTEMPTS_HEADING, 1)[0]


def _canonical(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip().lower())
    return {"tests": "test", "changes": "change", "note": "notes"}.get(name, name)


def _sections(text: str) -> dict[str, str]:
    """Known issue sections by canonical name; inline heading text is kept."""
    text = _strip_handover(text or "")
    headings = list(_SECTION_HEADING_RE.finditer(text))
    ends = sorted({m.start() for m in headings} | {m.start() for m in _SECTION_END_RE.finditer(text)})
    result: dict[str, str] = {}
    for match in headings:
        name = _canonical(match.group("md") or match.group("bold") or match.group("colon"))
        end = next((pos for pos in ends if pos > match.start()), len(text))
        inline = match.group("rest").strip()
        body = text[match.end():end].strip()
        content = "\n".join(part for part in (inline, body) if part).strip()
        if content and name not in result:
            result[name] = content
    return result


def _change_files(change: str, root: Path) -> list[tuple[str, str]]:
    """Existing files under *root* named in the Change section (bounded)."""
    found: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for pair in _PATH_RE.findall(change):
        name = next((value for value in pair if value), "").strip()
        if not name or len(found) >= MAX_CHANGE_FILES:
            continue
        try:
            candidate = (root / name).resolve()
            if candidate in seen or root not in candidate.parents or not candidate.is_file():
                continue
            seen.add(candidate)
            body = candidate.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + "\n...[file cut]"
        found.append((candidate.relative_to(root).as_posix(), body))
    return found


def _render_feedback(round_no: int, error: str, previous: str) -> str:
    error = error.strip() or "unknown error"
    if len(error) > FEEDBACK_ERROR_CHARS:
        error = error[:FEEDBACK_ERROR_CHARS].rstrip() + "\n...[error cut]"
    text = (f"\n\nCorrection (round {round_no} of {MAX_ROUNDS}): your previous answer was rejected.\n"
            f"Error:\n{error}\n")
    if previous:
        text += f"\nPrevious file (fix it, do not repeat the mistake):\n{previous[:MAX_FILE_CHARS]}\n"
    return text


def _prompt(sections: dict[str, str], files: list[tuple[str, str]], feedback: str = "") -> str:
    source = "\n\n".join(f"--- {name} ---\n{body}" for name, body in files) or "(none found)"
    problem = sections.get("problem", "")
    problem_text = f"Problem:\n{problem}\n\n" if problem else ""
    return (
        "Write one focused Python unittest file for the requested test, BEFORE the change is made.\n\n"
        f"Current base-tree files named by the Change section (stable for this run):\n{source}\n\n"
        f"{problem_text}Change (not implemented yet):\n{sections.get('change', '(not given)')}\n\n"
        f"Requested test:\n{sections['test']}\n\n"
        'Return exactly one JSON object: {"path":"tests/test_name.py","content":"..."}.\n'
        "Use unittest.TestCase. Every test must FAIL on the current code by an assertion "
        "(not by an exception or import error: check with hasattr/getattr before calling new names) "
        "and pass once the change is made. Do not modify production code; use a new file name."
        f"{feedback}"
    )


def _call(client: Any, prompt: str) -> str:
    answer = client.complete(prompt, system=_SYSTEM, temperature=0.0, max_tokens=MAX_ANSWER_TOKENS)
    if inspect.isawaitable(answer):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return str(asyncio.run(_awaited(answer)))
        # Called from inside an event loop: run the coroutine on a private loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return str(pool.submit(asyncio.run, _awaited(answer)).result())
    return str(answer)


async def _awaited(value: Any) -> Any:
    return await value


def _candidate(answer: str, root: Path) -> tuple[str, str]:
    text = answer.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    json_text = fenced.group(1).strip() if fenced else text
    try:
        value = json.loads(json_text)
        path, content = str(value["path"]), str(value["content"])
    except (ValueError, TypeError, KeyError):
        code = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if not code:
            raise ValueError('answer is not a JSON object {"path": ..., "content": ...}') from None
        path, content = "tests/test_generated.py", code.group(1).strip() + "\n"
    pure = Path(path.strip())
    if (pure.is_absolute() or len(pure.parts) != 2 or pure.parts[0] != "tests" or pure.suffix != ".py"
            or not pure.stem.isidentifier() or not pure.stem.startswith("test")):
        raise ValueError(f"test path {path!r} must be a new tests/test_*.py file directly under tests/")
    if (root / pure).exists():
        raise ValueError(f"{pure.as_posix()} already exists; use a new file name")
    if not content.strip():
        raise ValueError("test file content is empty")
    return pure.as_posix(), content


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, timeout=_TIMEOUT, check=False, env=_env())


_COLLECT_SCRIPT = """\
import json, sys, unittest
def flat(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flat(item)
        else:
            yield item
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])
print(sys.argv[1] + json.dumps([test.id() for test in flat(suite)]))
"""

_RUN_SCRIPT = """\
import json, sys, unittest
result = unittest.TestResult()
unittest.defaultTestLoader.loadTestsFromName(sys.argv[2]).run(result)
print(sys.argv[1] + json.dumps({"run": result.testsRun, "failures": len(result.failures),
    "errors": len(result.errors), "skipped": len(result.skipped),
    "expected": len(result.expectedFailures), "unexpected": len(result.unexpectedSuccesses),
    "detail": [text[-400:] for _, text in result.errors][:1]}))
"""


def _marked(output: str, marker: str) -> Any:
    for line in reversed(output.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise ValueError("no result line")


def _module(root: Path, path: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _collect(root: Path, path: Path, content: str) -> tuple[list[str], str]:
    """``(test ids, error)``; compiling happens in-process, so no ``.pyc`` is written."""
    try:
        compile(content, path.name, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        return [], f"candidate does not compile: {type(exc).__name__}: {exc}"
    marker = f"@@nexus-{uuid.uuid4().hex}@@"
    try:
        done = _run([sys.executable, "-B", "-c", _COLLECT_SCRIPT, marker, _module(root, path)], root)
    except subprocess.TimeoutExpired:
        return [], f"collecting the candidate timed out after {_TIMEOUT:g}s"
    try:
        ids = _marked(done.stdout, marker)
    except (ValueError, TypeError):
        return [], "candidate cannot be collected by unittest:\n" + done.stdout[-FEEDBACK_ERROR_CHARS:]
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
        return [], "unittest collected no test methods from the candidate"
    if any("_FailedTest" in item for item in ids):
        return [], "candidate cannot be imported by unittest:\n" + done.stdout[-FEEDBACK_ERROR_CHARS:]
    module = _module(root, path)
    if any(not item.startswith(module + ".") for item in ids):
        return [], "candidate collects tests from other modules; do not import TestCase classes"
    return ids, ""


def _fails_on_base(root: Path, test_id: str) -> tuple[bool, str]:
    """``(True, "")`` only for a test that fails by assertion; otherwise the reason."""
    marker = f"@@nexus-{uuid.uuid4().hex}@@"
    try:
        done = _run([sys.executable, "-B", "-c", _RUN_SCRIPT, marker, test_id], root)
    except subprocess.TimeoutExpired:
        return False, f"{test_id}: timed out after {_TIMEOUT:g}s"
    try:
        result = _marked(done.stdout, marker)
    except (ValueError, TypeError):
        return False, f"{test_id}: test process crashed (exit {done.returncode}): {done.stdout[-300:]}"
    if result.get("errors"):
        return False, f"{test_id}: raised an error instead of failing an assertion: {''.join(result['detail'])}"
    if result.get("run") == 1 and result.get("failures") == 1:
        return True, ""
    return False, f"{test_id}: passes on the base tree (it must fail before the change)"


def _base_names(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def _is_test(node: ast.stmt) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _prune(content: str, kept: set[tuple[str, str]]) -> str:
    """Remove test methods whose ``(class, method)`` is not in *kept*.

    A class left without test methods is removed, unless a remaining class
    inherits from it; then its body gets ``pass`` so the file still compiles.
    Classes without any test method (helpers, mixins) stay unchanged.
    """
    tree = ast.parse(content)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    parents = {cls.name: {_base_names(base) for base in cls.bases} for cls in classes}

    def below(name: str) -> set[str]:
        found: set[str] = set()
        todo = [name]
        while todo:
            current = todo.pop()
            for other, bases in parents.items():
                if current in bases and other not in found:
                    found.add(other)
                    todo.append(other)
        return found

    drops: dict[str, list[ast.stmt]] = {}
    alive: set[str] = set()
    for cls in classes:
        tests = [node for node in cls.body if _is_test(node)]
        owners = {cls.name} | below(cls.name)  # an inherited test runs in every subclass
        drops[cls.name] = [node for node in tests
                           if not any((owner, node.name) in kept for owner in owners)]  # type: ignore[attr-defined]
        if not tests or len(drops[cls.name]) < len(tests):
            alive.add(cls.name)

    def start(node: ast.stmt) -> int:
        return min([node.lineno, *(item.lineno for item in getattr(node, "decorator_list", []))]) - 1

    edits: list[tuple[int, int, str]] = []  # (first line index, end line, replacement)
    for cls in classes:
        if cls.name not in alive and not (below(cls.name) & alive):
            edits.append((start(cls), cls.end_lineno or cls.lineno, ""))
            continue
        drop = drops[cls.name]
        if not drop:
            continue
        empty = all(node in drop for node in cls.body)
        for index, node in enumerate(drop):
            filler = " " * node.col_offset + "pass\n" if empty and index == 0 else ""
            edits.append((start(node), node.end_lineno or node.lineno, filler))
    lines = content.splitlines(keepends=True)
    for begin, end, replacement in sorted(edits, reverse=True):
        lines[begin:end] = [replacement] if replacement else []
    return "".join(lines)


def _check(root: Path, path: Path, content: str) -> tuple[list[str], list[str], str]:
    """``(collected ids, ids failing on base, reasons for the others)``."""
    path.write_text(content, encoding="utf-8")
    ids, error = _collect(root, path, content)
    if error:
        return [], [], error
    kept: list[str] = []
    reasons: list[str] = []
    for test_id in ids:
        fails, reason = _fails_on_base(root, test_id)
        if fails:
            kept.append(test_id)
        else:
            reasons.append(reason)
    return ids, kept, "\n".join(reasons)


def _remove_stale(tests_dir: Path) -> None:
    """Candidates left behind by a killed earlier run (older than ``_STALE_SECONDS``)."""
    now = time.time()
    for stale in tests_dir.glob(_CANDIDATE_PREFIX + "*.py"):
        try:
            if now - stale.stat().st_mtime > _STALE_SECONDS:
                stale.unlink()
        except OSError:
            pass


def _cleanup(path: Path, created_dirs: list[Path]) -> None:
    try:
        path.unlink(missing_ok=True)
        cache = path.parent / "__pycache__"
        if cache.is_dir():
            for compiled in cache.glob(path.stem + ".*.pyc"):
                compiled.unlink(missing_ok=True)
    except OSError:
        logger.warning("local_test_first: could not remove candidate %s", path)
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()  # only when empty: never removes files that were already there
        except OSError:
            pass


def generate_failing_tests(issue_text: str, working_dir: str, client: Any) -> tuple[list[str], str]:
    """Return ``(test_ids, file_content)`` for tests that fail on the base tree.

    ``test_ids`` use the module path of the file the model chose (for example
    ``tests.test_value.ValueTests.test_value``); ``file_content`` contains exactly
    those tests. ``([], "")`` when nothing usable was produced.
    """
    try:
        sections = _sections(issue_text)
        if not sections.get("test"):
            logger.info("local_test_first: issue has no Test section")
            return [], ""
        root = Path(working_dir).resolve()
        if not root.is_dir():
            return [], ""
        files = _change_files(sections.get("change", ""), root)
    except Exception:  # noqa: BLE001 - optional stage, never breaks an experiment
        logger.exception("local_test_first: preparing the prompt failed")
        return [], ""

    tests_dir = root / "tests"
    candidate_path = tests_dir / f"{_CANDIDATE_PREFIX}{uuid.uuid4().hex}.py"
    created_dirs: list[Path] = []
    try:
        if not tests_dir.exists():
            tests_dir.mkdir(parents=True)
            created_dirs.append(tests_dir)
        _remove_stale(tests_dir)
        feedback = ""
        for round_no in range(1, MAX_ROUNDS + 1):
            try:
                answer = _call(client, _prompt(sections, files, feedback))
            except Exception as exc:  # noqa: BLE001 - unreachable server, HTTP error, ...
                raise _Interrupted(f"local model call failed: {type(exc).__name__}: {exc}") from exc
            previous = ""
            try:
                target, content = _candidate(answer, root)
                previous = content
                result = _validated(root, candidate_path, target, content)
            except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
                result = ([], "", f"{type(exc).__name__}: {exc}")
            ids, final, error = result
            logger.info("local_test_first round %d/%d: %s", round_no, MAX_ROUNDS,
                        f"kept {len(ids)} test(s)" if ids else f"rejected ({error[:200]})")
            if ids:
                return ids, final
            feedback = _render_feedback(round_no + 1, error, previous)
        return [], ""
    except _Interrupted as exc:
        logger.info("local_test_first: %s", exc)
        return [], ""
    except Exception:  # noqa: BLE001 - optional stage, never breaks an experiment
        logger.exception("local_test_first failed")
        return [], ""
    finally:
        _cleanup(candidate_path, created_dirs)


def _validated(root: Path, path: Path, target: str, content: str) -> tuple[list[str], str, str]:
    """``(public ids, content, error)``; content holds exactly the returned tests."""
    ids, kept, reasons = _check(root, path, content)
    if not kept:
        return [], "", reasons or "no test fails on the base tree"
    final = content
    if len(kept) < len(ids):
        temp_module = _module(root, path)
        pairs = {tuple(item[len(temp_module) + 1:].rsplit(".", 1)) for item in kept}
        final = _prune(content, pairs)  # type: ignore[arg-type]
        recollected, rekept, again = _check(root, path, final)
        # The pruned file must contain exactly the kept tests, and all of them must fail.
        if not recollected or set(recollected) != set(rekept) or not set(rekept) <= set(kept):
            return [], "", ("after removing the tests that do not fail on base, the file was not valid "
                            "(write independent test classes; every test must fail on base):\n"
                            + (again or "collected tests changed"))
        kept = rekept
    temp_module = _module(root, path)
    target_module = ".".join(Path(target).with_suffix("").parts)
    return [target_module + item[len(temp_module):] for item in kept], final, ""
