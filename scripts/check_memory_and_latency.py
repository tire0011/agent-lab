#!/usr/bin/env python3
"""Does a bigger prompt budget cost the VM its headroom, or its speed?

The prompt budget used to collapse to 500 characters on any host where the
model was resident, because it subtracted that model's footprint from
``MemAvailable`` (#223). With that corrected, prompts run against the full
context window instead - which is the point, and also a sustained change in
what llama-server holds.

Two numbers decide whether that is safe here, and neither can be reasoned
out:

* **Headroom.** ``llama-server.service`` sets ``MemoryMax=6G``. A full-context
  prompt was measured at 6138 MiB resident against that 6144 MiB ceiling. Six
  mebibytes is not a margin. The model is mmap'd, so the likely failure is not
  an OOM kill but the kernel evicting model pages and reading them back - on a
  host whose disk is 13.5x slower than a dev box, that does not look like a
  crash, it looks like inference stopping.
* **Latency.** Each controller turn now writes its decision to the production
  database, which commits with ``synchronous=FULL``. Two extra fsyncs per turn
  on that disk is a cost worth knowing before it is shipped, not after.

Neither is a pass/fail the script can decide alone: what counts as acceptable
latency is a judgement. It reports, and fails only on the one outcome that is
unambiguously bad - llama-server going away.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _rss_mib(process: str = "llama-server") -> int | None:
    """Resident size of *process*, summed over its pids."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-C", process],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    total = sum(int(line) for line in out.split() if line.isdigit())
    return total // 1024 if total else None


def _meminfo_mib(field: str) -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == field:
                digits = "".join(c for c in value if c.isdigit())
                return int(digits) // 1024 if digits else None
    except OSError:
        return None
    return None


def _memory_max_mib(unit: str = "llama-server") -> int | None:
    """The cgroup ceiling, which is what actually kills the process."""
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "MemoryMax", "--value"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out) // (1024 * 1024) if out.isdigit() else None


def _alive(unit: str = "llama-server") -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=30, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _sample(label: str) -> dict:
    return {
        "label": label,
        "llama_rss_mib": _rss_mib(),
        "mem_available_mib": _meminfo_mib("MemAvailable"),
        "swap_free_mib": _meminfo_mib("SwapFree"),
        "alive": _alive(),
    }


def _print(sample: dict, ceiling: int | None) -> None:
    rss = sample["llama_rss_mib"]
    headroom = f"{ceiling - rss:>5} MiB" if (ceiling and rss) else "    ?"
    print(
        f"  {sample['label']:<22} rss={str(rss):>6} MiB"
        f"  headroom={headroom}"
        f"  available={str(sample['mem_available_mib']):>6} MiB"
        f"  swap_free={str(sample['swap_free_mib']):>6} MiB"
        f"  llama={'up' if sample['alive'] else 'DOWN'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--turns", type=int, default=5,
                        help="controller turns to drive (default 5)")
    parser.add_argument("--db-path", type=Path,
                        default=Path("/tmp/nexus-memory-check.db"))
    parser.add_argument("--repo-root", type=Path, default=Path("/agent"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ceiling = _memory_max_mib()
    print("memory and latency under the corrected prompt budget")
    print(f"llama-server MemoryMax: {ceiling} MiB" if ceiling else
          "llama-server MemoryMax: unset (no cgroup ceiling)")
    print(f"turns: {args.turns}")
    print()

    from modules.prompt_budget import (
        compute_budget, read_mem_available_kb, read_swap_free_kb, read_swap_total_kb,
    )

    plan = compute_budget(
        mem_available_kb=read_mem_available_kb(),
        swap_free_kb=read_swap_free_kb(),
        swap_total_kb=read_swap_total_kb(),
    )
    print(f"budget: observation_chars={plan.observation_chars} "
          f"effective={plan.effective_budget_chars} low_memory={plan.low_memory}")
    print()

    samples = [_sample("baseline")]
    _print(samples[0], ceiling)

    if args.db_path.exists():
        args.db_path.unlink()

    from types import SimpleNamespace
    from modules.task_orchestrator import NexusTaskOrchestrator
    from modules.operations_registry import OperationsRegistry

    def _no_experiments():
        return SimpleNamespace(
            resolve_experiment=lambda i: (_ for _ in ()).throw(KeyError(i))
        )

    registry = OperationsRegistry(
        experiment_service=_no_experiments(),
        db_path=str(args.db_path),
        repo_root=str(args.repo_root),
    )
    orchestrator = NexusTaskOrchestrator(
        operations_registry=registry,
        experiment_service=_no_experiments(),
        max_steps=3,
    )

    durations: list[float] = []
    try:
        for turn in range(1, args.turns + 1):
            started = time.perf_counter()
            try:
                orchestrator.run(
                    "summarise what this repository is for",
                    source_type="task",
                    source_id=f"memory-check-{turn}",
                )
            except Exception as exc:  # noqa: BLE001 - a failed turn is a data point
                print(f"  turn {turn} raised: {type(exc).__name__}: {str(exc)[:200]}")
            durations.append(time.perf_counter() - started)
            samples.append(_sample(f"after turn {turn}"))
            _print(samples[-1], ceiling)
    finally:
        orchestrator.close()

    print()
    if durations:
        ordered = sorted(durations)
        print("turn latency")
        print(f"  runs:   {len(durations)}")
        print(f"  min:    {ordered[0]:.1f}s")
        print(f"  median: {ordered[len(ordered) // 2]:.1f}s")
        print(f"  max:    {ordered[-1]:.1f}s")
    print()

    if args.json:
        print(json.dumps({"samples": samples, "durations": durations}, indent=2))
        print()

    rss_values = [s["llama_rss_mib"] for s in samples if s["llama_rss_mib"]]
    if rss_values and ceiling:
        peak = max(rss_values)
        print(f"peak rss {peak} MiB against a {ceiling} MiB ceiling "
              f"({ceiling - peak} MiB spare)")
        if ceiling - peak < 64:
            # Not a failure: the run survived. But a margin this thin is the
            # thing to watch, and saying so is the point of the check.
            print("  WARNING: under 64 MiB of headroom. The model is mmap'd, so")
            print("  the kernel is more likely to evict its pages than to kill it -")
            print("  which shows up as inference slowing down, not as a crash.")

    if not samples[-1]["alive"]:
        print("FAIL: llama-server is not running any more")
        return 1
    print("PASS: llama-server survived; the numbers above are the judgement call")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
