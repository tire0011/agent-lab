"""Cheap, bounded local-model file localization (#696)."""

from __future__ import annotations

import ast
import asyncio
import json
import math
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


def _usable_rate(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value) if math.isfinite(value) and value > 0 else fallback


def measured_rates(rates: Any = None) -> tuple[float, float]:
    """``(prompt, generation)`` tokens/s from *rates*, falling back per rate.

    *rates* is either the worker's ``(prompt, generation)`` medians tuple or a
    ``LocalModelRates``-like object with ``medians()``.  The hard-coded
    7.0/2.6 only fill in a rate that was not measured.  The client's timeout
    is deliberately never consulted (#719).
    """
    if rates is not None and callable(getattr(rates, "medians", None)):
        try:
            rates = rates.medians()
        except Exception:
            rates = None
    try:
        prompt_rate, generation_rate = rates if rates is not None else (None, None)
    except (TypeError, ValueError):
        prompt_rate = generation_rate = None
    return (_usable_rate(prompt_rate, DEFAULT_PROMPT_RATE),
            _usable_rate(generation_rate, DEFAULT_OUTPUT_RATE))


def _call_budget(seconds_budget: float, rates: Any = None) -> tuple[int, int]:
    """``(prompt tokens, output tokens)`` one call can afford in *seconds_budget*.

    The answer is capped to the generation share of the budget first: at the
    fallback 2.6 tok/s the full 150-token answer alone would take ~58 s and
    leave no room for any map inside 45 s.
    """
    from modules.local_coding_worker import (
        MAX_GENERATION_TIME_SHARE,
        TIME_BUDGET_FRACTION,
        time_budget,
    )

    measured = measured_rates(rates)
    try:
        seconds = float(seconds_budget) * TIME_BUDGET_FRACTION
    except (TypeError, ValueError):
        return 0, 0
    if not math.isfinite(seconds) or seconds <= 0:
        return 0, 0
    max_output = max(1, min(MAX_OUTPUT_TOKENS,
                            int(seconds * MAX_GENERATION_TIME_SHARE * measured[1])))
    allowance = time_budget(seconds_budget, max_output, measured)
    if allowance is None:
        return 0, max_output
    return allowance.prompt_tokens, allowance.output_tokens


def _prompt_budget(seconds_budget: float, rates: Any = None) -> int:
    """Prompt-token admission allowance for *seconds_budget* at the measured rates."""
    return _call_budget(seconds_budget, rates)[0]


async def localize(
    issue_text: str,
    repo: str | Path,
    client: Any,
    k: int = 5,
    *,
    seconds_budget: float = 45,
    rates: Any = None,
) -> list[str]:
    """Ask llama-server for at most *k* existing repository paths.

    The repository map is sized from *seconds_budget* and *rates* (the
    worker's ``rates.medians()`` or the rates object itself), never from
    ``client.timeout``.
    """
    k = max(0, int(k))
    if not k:
        return []
    prompt_budget, output_budget = _call_budget(seconds_budget, rates)
    overhead = max(64, len(issue_text.encode("utf-8")) // 4 + 90)
    mapping = repo_map(repo, token_budget=max(0, prompt_budget - overhead))
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
            prompt=prompt, temperature=0.0, max_tokens=output_budget,
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
