"""Cost measurement for one local coding run (#639).

Best-of-N sampling is only worth it if the gain outweighs the cost on the
2 vCPU VM, so every local coding run (N=1 or not) records:

- wall time for the whole run and for each patch sample,
- CPU time the llama-server process used during the run and the average
  system load (1-minute load average, sampled at the start, after every model
  call and at the end),
- peak resident memory of llama-server seen during the run (``VmRSS`` sampled
  at the same points) and the process' all-time peak (``VmHWM``),
- tokens in and out per model call and tokens per second.

The llama-server process is found by scanning ``/proc`` for a process named
``llama-server`` (or ``NEXUS_LLAMA_SERVER_PID``). When it cannot be found -
another host, no ``/proc``, no permission - the process fields are ``None``;
measuring never raises.
"""

from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules.local_model_rates import draft_from_timings

ENV_SERVER_PID = "NEXUS_LLAMA_SERVER_PID"
_PROCESS_NAMES = ("llama-server", "llama_server", "llama-cpp-server")
_PROC = Path("/proc")


def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def find_server_pid(proc: Path = _PROC) -> int | None:
    """The llama-server PID, or ``None``."""
    configured = os.environ.get(ENV_SERVER_PID, "").strip()
    if configured.isdigit() and (proc / configured).is_dir():
        return int(configured)
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            if comm in _PROCESS_NAMES:
                return int(entry.name)
            argv0 = (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue
        if Path(argv0).name in _PROCESS_NAMES:
            return int(entry.name)
    return None


def process_cpu_seconds(pid: int, proc: Path = _PROC) -> float | None:
    """utime + stime of *pid* in seconds."""
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces; fields after it start at ")".
    fields = raw.rsplit(")", 1)[-1].split()
    try:
        return (int(fields[11]) + int(fields[12])) / _clock_ticks()
    except (IndexError, ValueError):
        return None


def process_memory_kb(pid: int, proc: Path = _PROC) -> tuple[int | None, int | None]:
    """``(VmRSS, VmHWM)`` of *pid* in kB."""
    rss = hwm = None
    try:
        lines = (proc / str(pid) / "status").read_text().splitlines()
    except OSError:
        return None, None
    for line in lines:
        name, _, value = line.partition(":")
        parts = value.split()
        if not parts or not parts[0].isdigit():
            continue
        if name == "VmRSS":
            rss = int(parts[0])
        elif name == "VmHWM":
            hwm = int(parts[0])
    return rss, hwm


def load_average() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return None


@dataclass
class CallRecord:
    """One local model call."""

    call_type: str
    outcome: str
    wall_seconds: float
    tokens_in: int | None
    tokens_out: int | None
    temperature: float = 0.0
    seed: int | None = None
    # Speculative decoding (#669); None when the server drafted nothing.
    draft_n: int | None = None
    draft_n_accepted: int | None = None

    @property
    def draft_acceptance_rate(self) -> float | None:
        if not self.draft_n or self.draft_n_accepted is None:
            return None
        return self.draft_n_accepted / self.draft_n

    @property
    def tokens_per_second(self) -> float | None:
        if self.tokens_out is None or self.wall_seconds <= 0:
            return None
        return self.tokens_out / self.wall_seconds


def call_record(call_type: str, outcome: str, wall_seconds: float, timings: Any,
                prompt_tokens: Any = None, *, temperature: float = 0.0,
                seed: int | None = None) -> CallRecord:
    """Tokens from llama-server's ``timings`` (fallback: the counted prompt)."""
    tokens_in = tokens_out = None
    if isinstance(timings, dict):
        processed = timings.get("prompt_n")
        cached = timings.get("tokens_cached")
        if isinstance(processed, (int, float)) and not isinstance(processed, bool):
            tokens_in = int(max(0, processed))
            if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                tokens_in += int(max(0, cached))
        predicted = timings.get("predicted_n")
        if isinstance(predicted, (int, float)) and not isinstance(predicted, bool):
            tokens_out = int(max(0, predicted))
    if tokens_in is None and isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool):
        tokens_in = prompt_tokens
    draft = draft_from_timings(timings if isinstance(timings, dict) else None)
    return CallRecord(call_type, outcome, max(0.0, float(wall_seconds)), tokens_in, tokens_out,
                      temperature, seed, draft.get("draft_n"), draft.get("draft_n_accepted"))


@dataclass
class RunMetrics:
    """Measures one local coding run; :meth:`finish` returns the log fields."""

    pid: int | None = None
    started: float = field(default_factory=time.monotonic)
    cpu_start: float | None = None
    loads: list[float] = field(default_factory=list)
    rss_samples: list[int] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    proc: Path = _PROC

    @classmethod
    def start(cls, proc: Path = _PROC) -> "RunMetrics":
        metrics = cls(pid=find_server_pid(proc), proc=proc)
        if metrics.pid is not None:
            metrics.cpu_start = process_cpu_seconds(metrics.pid, proc)
        metrics.probe()
        return metrics

    def probe(self) -> None:
        """Sample load and llama-server memory; never raises."""
        load = load_average()
        if load is not None:
            self.loads.append(load)
        if self.pid is not None:
            rss, _hwm = process_memory_kb(self.pid, self.proc)
            if rss is not None:
                self.rss_samples.append(rss)

    def record_call(self, record: CallRecord) -> None:
        self.calls.append(record)
        self.probe()

    def record_sample(self, sample: dict[str, Any]) -> None:
        self.samples.append(dict(sample))

    def finish(self) -> dict[str, Any]:
        self.probe()
        wall = time.monotonic() - self.started
        cpu = hwm = None
        if self.pid is not None:
            end = process_cpu_seconds(self.pid, self.proc)
            if end is not None and self.cpu_start is not None:
                cpu = max(0.0, end - self.cpu_start)
            _rss, hwm = process_memory_kb(self.pid, self.proc)
        tokens_in = sum(c.tokens_in or 0 for c in self.calls)
        tokens_out = sum(c.tokens_out or 0 for c in self.calls)
        model_seconds = sum(c.wall_seconds for c in self.calls)
        patch_calls = [c for c in self.calls if c.call_type == "patch"]
        drafted = sum(c.draft_n or 0 for c in self.calls)
        accepted = sum(c.draft_n_accepted or 0 for c in self.calls if c.draft_n)
        return {
            "wall_seconds": round(wall, 3),
            "model_calls": len(self.calls),
            "model_seconds": round(model_seconds, 3),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_per_second": round(tokens_out / model_seconds, 3) if model_seconds > 0 else None,
            "draft_n": drafted or None,
            "draft_n_accepted": accepted if drafted else None,
            "draft_acceptance_rate": round(accepted / drafted, 3) if drafted else None,
            "patch_samples": len(patch_calls),
            "sample_wall_seconds": [round(c.wall_seconds, 3) for c in patch_calls],
            "sample_wall_seconds_median": (round(statistics.median(c.wall_seconds for c in patch_calls), 3)
                                           if patch_calls else None),
            "llama_server_pid": self.pid,
            "llama_server_cpu_seconds": round(cpu, 3) if cpu is not None else None,
            "llama_server_cpu_cores": round(cpu / wall, 3) if cpu is not None and wall > 0 else None,
            "load_average": round(statistics.fmean(self.loads), 3) if self.loads else None,
            "llama_server_peak_rss_kb": max(self.rss_samples) if self.rss_samples else None,
            "llama_server_hwm_kb": hwm,
            "samples": [dict(s) for s in self.samples],
        }


def summary_line(fields: dict[str, Any]) -> str:
    """A one-line human summary for the experiment log read tool."""
    def fmt(value: Any, spec: str, unit: str = "") -> str:
        return "-" if value is None else f"{value:{spec}}{unit}"

    rss = fields.get("llama_server_peak_rss_kb")
    return (
        f"Local run: wall {fmt(fields.get('wall_seconds'), '.1f', 's')}, "
        f"{fields.get('model_calls', 0)} model calls ({fields.get('patch_samples', 0)} patch samples) "
        f"in {fmt(fields.get('model_seconds'), '.1f', 's')}, "
        f"tokens in/out {fields.get('tokens_in', 0)}/{fields.get('tokens_out', 0)} "
        f"({fmt(fields.get('tokens_per_second'), '.1f', ' tok/s')}), "
        f"llama-server CPU {fmt(fields.get('llama_server_cpu_seconds'), '.1f', 's')} "
        f"(avg {fmt(fields.get('llama_server_cpu_cores'), '.2f', ' cores')}), "
        f"load {fmt(fields.get('load_average'), '.2f')}, "
        f"peak RSS {fmt(rss / 1024 if rss is not None else None, '.0f', ' MiB')}"
    )
