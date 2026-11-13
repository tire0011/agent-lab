#!/usr/bin/env python3
"""Local llama-server speed benchmark, with and without a draft model (#669).

Sends five fixed prompts - the four frozen ``benchmarks/tasks/*.md`` issues and
one SEARCH/REPLACE edit task on a fixed file - with a fixed seed and
temperature 0 to the llama-server at ``--url`` and prints one JSON document:
per prompt and aggregated (``all`` and ``edit``) prompt tok/s, output tok/s,
draft acceptance rate and wall time, plus available memory and llama-server
RSS before and after (from ``/proc``, ``null`` where unknown).

    python scripts/bench_local_speed.py --url http://127.0.0.1:8080 > no-draft.json
    # switch the draft model on (scripts/fetch-draft-model.sh, then
    #   nexus-llama-model switch DRAFT_MODEL=qwen2.5-coder-0.5b-instruct-q8_0.gguf)
    python scripts/bench_local_speed.py --url http://127.0.0.1:8080 \\
        --baseline no-draft.json > draft.json

With ``--baseline`` the output also has ``speedup``: the output tok/s ratio
against the earlier run, for ``all`` and ``edit``. #669 counts a success at
``edit`` >= 1.4 and a negative result below 1.2.

Rates come from llama-server's own ``timings`` (sum of tokens / sum of time),
not from the wall clock, so HTTP overhead does not blur them. The prompt cache
is off for every request so each run evaluates the full prompt. Only the
Python standard library is used. Exit code 0 when every prompt answered,
1 when at least one failed (the JSON is printed anyway).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.local_model_rates import draft_from_timings  # noqa: E402
from modules.local_run_metrics import find_server_pid, process_memory_kb  # noqa: E402

TASKS_DIR = _REPO_ROOT / "benchmarks" / "tasks"
DEFAULT_SEED = 42
DEFAULT_MAX_TOKENS = 256
DEFAULT_TIMEOUT = 600.0

SYSTEM_PROMPT = "You are a careful senior Python developer working in the agent-lab repository."

# Frozen like benchmarks/tasks: changing this text makes older runs incomparable.
EDIT_FILE_PATH = "modules/retry_policy.py"
EDIT_FILE_TEXT = '''"""Retry policy for outgoing HTTP calls."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait before retry number *attempt* (1-based)."""
        return min(self.max_delay, self.base_delay * 2 ** (attempt - 1))

    def run(self, call):
        last_error = None
        for attempt in range(1, self.attempts + 1):
            try:
                return call()
            except Exception as exc:
                last_error = exc
                time.sleep(self.delay(attempt))
        raise last_error


def default_policy() -> RetryPolicy:
    return RetryPolicy()
'''
EDIT_TASK = (
    "Change `RetryPolicy` so that `delay` adds random jitter of up to 10% of the computed "
    "delay (use `random.uniform`), never exceeds `max_delay`, and `run` does not sleep "
    "after the last failed attempt.\n\n"
    "Answer only with SEARCH/REPLACE edit blocks in exactly this format, nothing else:\n\n"
    "path/to/file.py\n<<<<<<< SEARCH\nexact lines copied from the file\n=======\n"
    "replacement lines\n>>>>>>> REPLACE\n\n"
    f"File `{EDIT_FILE_PATH}`:\n\n```python\n{EDIT_FILE_TEXT}```\n"
)


def _task_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def fixed_prompts(tasks_dir: Path = TASKS_DIR) -> list[dict[str, str]]:
    """The five prompts: four frozen benchmark tasks (sorted) and the edit task."""
    prompts = []
    for path in sorted(tasks_dir.glob("*.md"))[:4]:
        prompts.append({
            "name": path.stem, "kind": "task",
            "prompt": ("Plan the implementation of this issue: name the files to change and "
                       "the concrete code changes, briefly.\n\n" + _task_body(path)),
        })
    prompts.append({"name": "search-replace-edit", "kind": "edit", "prompt": EDIT_TASK})
    return prompts


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rate(tokens: Any, ms: Any) -> float | None:
    if _number(tokens) and _number(ms) and tokens > 0 and ms > 0:
        return round(float(tokens) * 1000.0 / float(ms), 3)
    return None


def run_prompt(base_url: str, item: dict[str, str], *, seed: int, max_tokens: int,
               timeout: float) -> dict[str, Any]:
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": item["prompt"]}],
        "temperature": 0, "seed": seed, "max_tokens": max_tokens,
        "cache_prompt": False, "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    result: dict[str, Any] = {"name": item["name"], "kind": item["kind"]}
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local URL
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.update(wall_seconds=round(time.monotonic() - started, 3),
                      error=f"{type(exc).__name__}: {exc}"[:300])
        return result
    wall = time.monotonic() - started
    timings = data.get("timings") if isinstance(data, dict) else None
    timings = timings if isinstance(timings, dict) else {}
    draft = draft_from_timings(timings)
    result.update(
        wall_seconds=round(wall, 3),
        prompt_n=timings.get("prompt_n"), prompt_ms=timings.get("prompt_ms"),
        predicted_n=timings.get("predicted_n"), predicted_ms=timings.get("predicted_ms"),
        prompt_tokens_per_second=_rate(timings.get("prompt_n"), timings.get("prompt_ms")),
        output_tokens_per_second=_rate(timings.get("predicted_n"), timings.get("predicted_ms")),
        draft_n=draft.get("draft_n"), draft_n_accepted=draft.get("draft_n_accepted"),
        acceptance_rate=(round(draft["draft_acceptance_rate"], 3) if draft else None),
    )
    if not timings:
        result["error"] = "response without timings"
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> float:
        return sum(r[key] for r in results if _number(r.get(key)))

    drafted, accepted = total("draft_n"), total("draft_n_accepted")
    return {
        "prompts": len(results),
        "failed": sum(1 for r in results if r.get("error")),
        "prompt_tokens_per_second": _rate(total("prompt_n"), total("prompt_ms")),
        "output_tokens_per_second": _rate(total("predicted_n"), total("predicted_ms")),
        "draft_n": int(drafted) if drafted else None,
        "draft_n_accepted": int(accepted) if drafted else None,
        "acceptance_rate": round(accepted / drafted, 3) if drafted else None,
        "wall_seconds": round(total("wall_seconds"), 3),
    }


def memory_snapshot(proc: Path = Path("/proc")) -> dict[str, Any]:
    """MemAvailable (what ``free -m`` calls available) and llama-server RSS, in MiB."""
    available = None
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    rss = None
    pid = find_server_pid(proc)
    if pid is not None:
        rss_kb, _hwm = process_memory_kb(pid, proc)
        rss = rss_kb // 1024 if rss_kb is not None else None
    return {"mem_available_mib": available, "llama_server_rss_mib": rss}


def speedup(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for group in ("all", "edit"):
        new = (current.get("summary") or {}).get(group, {}).get("output_tokens_per_second")
        old = (baseline.get("summary") or {}).get(group, {}).get("output_tokens_per_second")
        out[group] = round(new / old, 3) if _number(new) and _number(old) and old > 0 else None
    return out


def bench(base_url: str, *, seed: int = DEFAULT_SEED, max_tokens: int = DEFAULT_MAX_TOKENS,
          timeout: float = DEFAULT_TIMEOUT, tasks_dir: Path = TASKS_DIR,
          proc: Path = Path("/proc")) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    before = memory_snapshot(proc)
    results = [run_prompt(base_url, item, seed=seed, max_tokens=max_tokens, timeout=timeout)
               for item in fixed_prompts(tasks_dir)]
    return {
        "url": base_url, "seed": seed, "max_tokens": max_tokens,
        "results": results,
        "summary": {"all": summarize(results),
                    "edit": summarize([r for r in results if r["kind"] == "edit"])},
        "memory": {"before": before, "after": memory_snapshot(proc)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="llama-server base URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request")
    parser.add_argument("--baseline", type=Path, help="JSON of an earlier run to compute the speed-up")
    parser.add_argument("--label", help="free text stored in the output, e.g. 'draft q8_0'")
    args = parser.parse_args(argv)
    report = bench(args.url, seed=args.seed, max_tokens=args.max_tokens, timeout=args.timeout)
    if args.label:
        report["label"] = args.label
    if args.baseline:
        report["speedup"] = speedup(report, json.loads(args.baseline.read_text(encoding="utf-8")))
    print(json.dumps(report, indent=2))
    return 1 if report["summary"]["all"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
