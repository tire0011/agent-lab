"""Generate regression tests that demonstrably fail on the unchanged tree.

This is deliberately independent of the experiment pipeline.  A candidate is
only returned after it compiles, is collected by unittest, and fails by
assertion when run against the current (base) working tree.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

MAX_ROUNDS = 3
_TIMEOUT = 20
_SYSTEM = "You write small Python unittest regression tests. Return strict JSON only."
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _sections(text: str) -> dict[str, str]:
    matches = list(_HEADING.finditer(text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match.group(1).strip().lower()] = text[match.end():end].strip()
    return result


def _change_files(change: str, root: Path) -> list[tuple[str, str]]:
    paths = re.findall(r"`([^`\n]+)`|(?<![\w/.-])([\w.-]+(?:/[\w.-]+)+\.py)", change)
    found: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for pair in paths:
        name = next(value for value in pair if value).strip()
        candidate = (root / name).resolve()
        if candidate in seen or root not in candidate.parents or not candidate.is_file():
            continue
        seen.add(candidate)
        found.append((candidate.relative_to(root).as_posix(), candidate.read_text(encoding="utf-8", errors="replace")))
    return found


def _prompt(test_section: str, files: list[tuple[str, str]], feedback: str = "") -> str:
    source = "\n\n".join(f"--- {name} ---\n{body}" for name, body in files) or "(none found)"
    correction = f"\n\nPrevious candidate was rejected:\n{feedback[:2000]}\nCorrect it." if feedback else ""
    return f"""Write one focused Python unittest file for this requested test:
{test_section}

Relevant base-tree files named by the Change section:
{source}

Return exactly one JSON object: {{"path":"tests/test_name.py","content":"..."}}.
Use unittest.TestCase. Do not modify production code.{correction}"""


def _call(client: Any, prompt: str) -> str:
    answer = client.complete(prompt, system=_SYSTEM, temperature=0.0, max_tokens=2000)
    if inspect.isawaitable(answer):
        return str(asyncio.run(answer))
    return str(answer)


def _candidate(answer: str) -> tuple[str, str]:
    text = answer.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    json_text = fenced.group(1).strip() if fenced else text
    try:
        value = json.loads(json_text)
        path, content = str(value["path"]), str(value["content"])
    except (ValueError, TypeError, KeyError):
        code = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if not code:
            raise ValueError("response is not a test-file JSON object")
        path, content = "tests/test_generated.py", code.group(1).strip() + "\n"
    pure = Path(path)
    if pure.is_absolute() or len(pure.parts) < 2 or pure.parts[0] != "tests" or pure.suffix != ".py" or ".." in pure.parts:
        raise ValueError("test path must be a .py file directly under tests/")
    return pure.as_posix(), content


def _run(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=_TIMEOUT, check=False)


def _collect(root: Path, path: Path) -> tuple[list[str], str]:
    compiled = _run([sys.executable, "-m", "py_compile", str(path)], root)
    if compiled.returncode:
        return [], "candidate does not compile:\n" + compiled.stdout
    module = ".".join(path.relative_to(root).with_suffix("").parts)
    script = ("import json,unittest\n"
              "def flat(s):\n"
              " for x in s:\n"
              "  if isinstance(x,unittest.TestSuite): yield from flat(x)\n"
              "  else: yield x\n"
              f"s=unittest.defaultTestLoader.loadTestsFromName({module!r})\n"
              "print(json.dumps([x.id() for x in flat(s)]))\n")
    collected = _run([sys.executable, "-c", script], root)
    if collected.returncode:
        return [], "candidate cannot be collected by unittest:\n" + collected.stdout
    try:
        ids = json.loads(collected.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [], "candidate collection returned no test ids"
    if not ids or any("_FailedTest" in item for item in ids):
        return [], "candidate collection returned no usable test ids"
    return ids, ""


def _assertion_failures(root: Path, ids: list[str]) -> list[str]:
    kept: list[str] = []
    for test_id in ids:
        result = _run([sys.executable, "-m", "unittest", test_id, "-v"], root)
        output = result.stdout
        if result.returncode and "FAILED (failures=" in output and "errors=" not in output:
            kept.append(test_id)
    return kept


def _only_methods(content: str, kept: list[str]) -> str:
    """Remove TestCase methods that did not produce a base-tree assertion failure."""
    wanted = {test_id.rsplit(".", 1)[-1] for test_id in kept}
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)
    remove: list[tuple[int, int]] = []
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test") and node.name not in wanted:
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
                remove.append((start, node.end_lineno or node.lineno))
    for start, end in reversed(remove):
        del lines[start:end]
    return "".join(lines)


def generate_failing_tests(issue_text: str, working_dir: str, client: Any) -> tuple[list[str], str]:
    """Return ``(test_ids, file_content)`` for tests that fail on the base tree."""
    sections = _sections(issue_text)
    requested = sections.get("test", "").strip()
    if not requested:
        return [], ""
    root = Path(working_dir).resolve()
    files = _change_files(sections.get("change", ""), root)
    feedback = ""
    candidate_path = root / "tests" / f"test_local_first_{uuid.uuid4().hex}.py"
    try:
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(MAX_ROUNDS):
            try:
                target_path, content = _candidate(_call(client, _prompt(requested, files, feedback)))
                candidate_path.write_text(content, encoding="utf-8")
                ids, error = _collect(root, candidate_path)
                if error:
                    feedback = error
                    continue
                kept = _assertion_failures(root, ids)
                temporary_module = ".".join(candidate_path.relative_to(root).with_suffix("").parts)
                target_module = ".".join(Path(target_path).with_suffix("").parts)
                public_ids = [target_module + item[len(temporary_module):] for item in kept]
                return public_ids, _only_methods(content, kept) if kept else ""
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, SyntaxError) as exc:
                feedback = str(exc)
        return [], ""
    except Exception:
        # This optional local-model stage must never break an experiment.
        return [], ""
    finally:
        candidate_path.unlink(missing_ok=True)
        cache = candidate_path.parent / "__pycache__"
        for compiled in cache.glob(candidate_path.stem + ".*.pyc") if cache.exists() else ():
            compiled.unlink(missing_ok=True)
