"""Cheap, bounded local-model file localization (#696)."""

from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from modules.llama_cpp_client import json_schema_response_format

ENV_LOCAL_LOCALIZE = "NEXUS_LOCAL_LOCALIZE"
MAX_OUTPUT_TOKENS = 150
DEFAULT_PROMPT_RATE = 7.0
DEFAULT_OUTPUT_RATE = 2.6
_FALSE = {"", "0", "false", "no", "off"}
_SKIP_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
LIKELY_FILES_HEADING = "Likely files:"


def enabled() -> bool:
    return os.getenv(ENV_LOCAL_LOCALIZE, "").strip().lower() not in _FALSE


def _summary(path: Path) -> str:
    if path.suffix != ".py":
        return ""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return ""
    doc = ast.get_docstring(tree, clean=True)
    if doc:
        return doc.splitlines()[0].strip()
    names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return ", ".join(names[:12])


def repo_map(repo: str | Path, *, token_budget: int) -> str:
    """Return a deterministic path/signature map no larger than the estimated budget."""
    root = Path(repo).resolve()
    remaining = max(0, token_budget) * 4  # conservative, tokenizer-free estimate
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if (not path.is_file() or path.is_symlink()
                or any(part in _SKIP_PARTS for part in path.relative_to(root).parts)):
            continue
        rel = path.relative_to(root).as_posix()
        line = rel
        summary = _summary(path)
        if summary:
            line += f" — {summary}"
        encoded = (line + "\n").encode("utf-8")
        if len(encoded) > remaining:
            continue
        lines.append(line)
        remaining -= len(encoded)
    return "\n".join(lines)


def _prompt_budget(seconds_budget: float, rates: tuple[float | None, float | None] | None) -> int:
    """Return the admission allowance for this call, using measured rates when supplied."""
    from modules.local_coding_worker import time_budget

    measured = rates or (DEFAULT_PROMPT_RATE, DEFAULT_OUTPUT_RATE)
    allowance = time_budget(seconds_budget, MAX_OUTPUT_TOKENS, measured)
    if allowance is None and rates is not None:
        allowance = time_budget(
            seconds_budget, MAX_OUTPUT_TOKENS,
            (DEFAULT_PROMPT_RATE, DEFAULT_OUTPUT_RATE),
        )
    return allowance.prompt_tokens if allowance else 0


async def localize(
    issue_text: str,
    repo: str | Path,
    client: Any,
    k: int = 5,
    *,
    seconds_budget: float = 45,
    rates: tuple[float | None, float | None] | None = None,
) -> list[str]:
    """Ask llama-server for at most *k* existing repository paths."""
    k = max(0, int(k))
    if not k:
        return []
    overhead = max(64, len(issue_text.encode("utf-8")) // 4 + 90)
    mapping = repo_map(
        repo,
        token_budget=max(0, _prompt_budget(seconds_budget, rates) - overhead),
    )
    prompt = (
        f"Issue:\n{issue_text}\n\nRepository map:\n{mapping}\n\n"
        f"Return JSON {{\"paths\": [...]}} with at most {k} paths most likely to change."
    )
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"paths": {"type": "array", "maxItems": k, "items": {"type": "string"}}},
        "required": ["paths"],
    }
    try:
        raw = await client.complete(
            prompt=prompt, temperature=0.0, max_tokens=MAX_OUTPUT_TOKENS,
            response_format=json_schema_response_format("file_localization", schema),
        )
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 and end >= start else {}
    except Exception:
        return []
    root = Path(repo).resolve()
    result: list[str] = []
    for value in data.get("paths", []) if isinstance(data, dict) else []:
        if not isinstance(value, str):
            continue
        rel = value.strip().replace("\\", "/").removeprefix("./")
        candidate = (root / rel).resolve()
        if rel not in result and candidate.is_file() and root in candidate.parents:
            result.append(rel)
        if len(result) >= k:
            break
    return result


def add_likely_files(task: str, repo: str | Path, client: Any) -> str:
    """Flag-gated synchronous boundary used by coding workers/handovers."""
    # The launcher adds the hint for every coder.  A local worker sees that
    # same goal later and must reuse it, rather than spending a second local
    # call on the identical localization request.
    if not enabled() or f"\n{LIKELY_FILES_HEADING}" in task:
        return task
    try:
        paths = asyncio.run(localize(task, repo, client))
    except RuntimeError:
        return task
    return task if not paths else task + f"\n\n{LIKELY_FILES_HEADING} " + ", ".join(paths)
