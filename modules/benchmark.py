"""Fixed benchmark of ``new_experiment``, with a baseline to compare against (#343).

``benchmarks/tasks/*.md`` is a small frozen set of real, already merged issues.
Each task pins the commit the issue was started from, so every run gets the same
starting point, and names the files the merged fix touched. The runner executes
the tasks through ``ExperimentService`` with one provider, model and prompt and
stores one ``benchmark_runs`` row per task; the report puts a run next to a
stored baseline run.

Three things here are deliberate, because the obvious version measures wrong:

  * Usage is counted at the worker, not read from the experiment. The experiment
    keeps only the first implementation call, so a model that needs two repair
    rounds would look cheaper than one that gets it right the first time.
  * An unknown number stays ``NULL`` and is reported as ``n/a``. Most setups have
    no ``model_prices`` row, and a cost of 0 would make them look free.
  * Runs go to their own database (:func:`default_benchmark_db_path`). In the
    production database adoption, the planner, ``problems`` and the tick
    notifications would treat benchmark experiments as real work.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from modules.coding_provider import build_worker, random_worker_factory
from modules.experiment_evaluation import parse_git_status_short
from modules.experiment_service import Experiment, ExperimentService
from modules.experiment_service.prompts import _build_direct_implementation_prompt
from modules.mailbox import parse_front_matter
from modules.redaction import sanitized_reason
from modules.run_store import (
    DEFAULT_DB_PATH,
    calculate_estimated_cost,
    get_applicable_price,
    get_setting,
    init_db,
    set_setting,
)

BENCHMARK_TASKS_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "tasks"
BASELINE_SETTING_KEY = "benchmark.baseline_run_id"

_MEMORY_SAMPLE_SECONDS = 1.0
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TEST_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# Runs the named modules, not the whole suite: a full run takes about 14 minutes
# and on the VM it stalls on the real llama-server, so its noise would drown the
# signal. tests.conftest has to load first, a single module does not start
# without it. -v is what the verification watchdog reads progress from.
_TEST_LAUNCHER = (
    "import sys, unittest, tests.conftest; "
    "unittest.main(module=None, argv=['unittest', '-v', *sys.argv[1:]])"
)


def default_benchmark_db_path() -> str:
    """Next to the production database, never the production database itself."""
    return str(Path(DEFAULT_DB_PATH).with_name("benchmark.db"))


# ---------------------------------------------------------------------------
# Task set
# ---------------------------------------------------------------------------


class BenchmarkError(ValueError):
    """Raised for an invalid task file or an unknown benchmark run."""


@dataclass(frozen=True)
class BenchmarkTask:
    id: str
    goal: str
    base_commit: str
    expected_files: tuple[str, ...]
    test_modules: tuple[str, ...]
    tests_green_points: int
    expected_files_points: int
    evaluation_points: int
    sha256: str

    @property
    def test_command(self) -> list[str]:
        return ["{python}", "-c", _TEST_LAUNCHER, *self.test_modules]


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def load_task(path: Path) -> BenchmarkTask:
    text = path.read_text(encoding="utf-8")
    try:
        meta, goal = parse_front_matter(text)
    except ValueError as exc:
        raise BenchmarkError(f"{path.name}: {exc}") from exc

    base_commit = meta.get("base_commit", "")
    if not _COMMIT_RE.match(base_commit):
        raise BenchmarkError(f"{path.name}: base_commit must be a full commit hash")
    expected_files = _csv(meta.get("expected_files"))
    test_modules = _csv(meta.get("test_modules"))
    if not goal or not expected_files or not test_modules:
        raise BenchmarkError(f"{path.name}: needs a goal, expected_files and test_modules")
    # These end up on a command line, so only plain module names get through.
    if not all(_TEST_MODULE_RE.match(module) for module in test_modules):
        raise BenchmarkError(f"{path.name}: test_modules must be dotted module names")
    try:
        points = [
            int(meta.get(key, ""))
            for key in ("tests_green_points", "expected_files_points", "evaluation_points")
        ]
    except ValueError as exc:
        raise BenchmarkError(f"{path.name}: rubric points must be integers") from exc
    if min(points) < 0 or sum(points) != 100:
        raise BenchmarkError(f"{path.name}: rubric points must add up to 100")

    return BenchmarkTask(
        id=path.stem,
        goal=goal,
        base_commit=base_commit,
        expected_files=expected_files,
        test_modules=test_modules,
        tests_green_points=points[0],
        expected_files_points=points[1],
        evaluation_points=points[2],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_tasks(
    tasks_dir: Path = BENCHMARK_TASKS_DIR, only: Iterable[str] | None = None
) -> list[BenchmarkTask]:
    tasks = [load_task(path) for path in sorted(tasks_dir.glob("*.md"))]
    if only is None:
        return tasks
    wanted = list(only)
    unknown = sorted(set(wanted) - {task.id for task in tasks})
    if unknown:
        raise BenchmarkError(f"unknown benchmark tasks: {', '.join(unknown)}")
    return [task for task in tasks if task.id in wanted]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskScore:
    score: float
    tests_green: bool
    expected_files_touched: int
    evaluation_outcome: str | None


def score_experiment(task: BenchmarkTask, experiment: Experiment) -> TaskScore:
    changed = set(parse_git_status_short(experiment.git_status).all_paths())
    # Doing nothing leaves the base commit's tests green. That is no achievement.
    tests_green = bool(changed) and experiment.test_exit_code == 0
    touched = len(changed.intersection(task.expected_files))
    outcome = (experiment.quality_evaluation or {}).get("outcome")
    score = (
        (task.tests_green_points if tests_green else 0)
        + task.expected_files_points * touched / len(task.expected_files)
        + (task.evaluation_points if outcome == "pass" else 0)
    )
    return TaskScore(round(score, 1), tests_green, touched, outcome)


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


class _UsageMeter:
    """Adds up the usage of every coding-agent call of one experiment."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None

    def add(self, usage: dict[str, int] | None) -> None:
        if not usage:
            return
        self.input_tokens = (self.input_tokens or 0) + usage.get("input_tokens", 0)
        self.output_tokens = (self.output_tokens or 0) + usage.get("output_tokens", 0)


class _MeteredWorker:
    """A coding worker that reports its usage and receives the prompt variant."""

    def __init__(self, worker: Any, meter: _UsageMeter, prompt_text: str) -> None:
        self._worker = worker
        self._meter = meter
        self._prompt_text = prompt_text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._worker, name)

    def run(self, *, task: str, **kwargs: Any) -> Any:
        prompt = f"{task}\n\n{self._prompt_text}" if self._prompt_text else task
        result = self._worker.run(task=prompt, **kwargs)
        self._meter.add(result.usage)
        return result


def process_tree_rss_bytes(root_pid: int | None = None) -> int | None:
    """Resident memory of a process and all its descendants, or None if unreadable.

    The coding agent and the test run are child processes, and ``ru_maxrss`` only
    ever rises over a process's life, so it could not tell one task from the next.
    """
    root_pid = root_pid or os.getpid()
    parents: dict[int, int] = {}
    pages: dict[int, int] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            # The command name may contain spaces, so split after its ")".
            fields = stat_path.read_text().rpartition(")")[2].split()
            pid = int(stat_path.parent.name)
            parents[pid], pages[pid] = int(fields[1]), int(fields[21])
        except (OSError, ValueError, IndexError):
            continue  # gone since the directory listing
    if root_pid not in pages:
        return None

    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    total, pending = 0, [root_pid]
    while pending:
        pid = pending.pop()
        total += pages[pid]
        pending.extend(children.get(pid, []))
    return total * os.sysconf("SC_PAGE_SIZE")


class _PeakMemorySampler:
    """Samples ``read_rss`` from a background thread while the block runs."""

    def __init__(self, read_rss: Callable[[], int | None], interval: float) -> None:
        self._read_rss = read_rss
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes: int | None = None

    def _sample(self) -> None:
        value = self._read_rss()
        if value is not None:
            self.peak_bytes = max(self.peak_bytes or 0, value)

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    def __enter__(self) -> _PeakMemorySampler:
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        self._thread.join()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Runs tasks through ``ExperimentService``, one fresh service per task.

    *provider* ``None`` keeps the normal rotation between the coding agents,
    which is the current production setup. *prompt_text* is appended to every
    coding-agent call; a change to the built-in prompt shows up in
    ``prompt_sha256`` without any option.
    """

    def __init__(
        self,
        *,
        repo_root: str,
        db_path: str | None = None,
        worktrees_root: str | None = None,
        test_python_executable: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_name: str = "default",
        prompt_text: str = "",
        worker_factory: Callable[[str], Any] | None = None,
        price_db_path: str = DEFAULT_DB_PATH,
        read_rss: Callable[[], int | None] = process_tree_rss_bytes,
        memory_sample_seconds: float = _MEMORY_SAMPLE_SECONDS,
        experiment_log_dir: str | None = None,
    ) -> None:
        if model and not provider:
            raise ValueError("a model needs a provider: the rotation runs several agents")
        self.repo_root = repo_root
        self.db_path = db_path or default_benchmark_db_path()
        self.worktrees_root = worktrees_root
        self.test_python_executable = test_python_executable
        self.provider = provider
        self.model = model
        self.prompt_name = prompt_name
        self.prompt_text = prompt_text
        self.prompt_sha256 = hashlib.sha256(
            f"{_build_direct_implementation_prompt('{goal}')}\n\n{prompt_text}".encode("utf-8")
        ).hexdigest()
        self.price_db_path = price_db_path
        self.read_rss = read_rss
        self.memory_sample_seconds = memory_sample_seconds
        self.experiment_log_dir = experiment_log_dir or str(
            Path(self.db_path).with_name("benchmark-logs")
        )
        self.worker_factory = worker_factory or self._default_worker_factory()

    def _default_worker_factory(self) -> Callable[[str], Any]:
        kwargs = {"model": self.model} if self.model else {}
        if self.provider:
            return lambda _path: build_worker(self.provider, db_path=self.db_path, **kwargs)
        return random_worker_factory(db_path=self.db_path)

    def run(self, tasks: Iterable[BenchmarkTask], run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        conn = init_db(self.db_path)
        try:
            for task in tasks:
                row = self._run_task(run_id, task)
                # One row at a time: a killed run keeps what it already measured.
                with conn:
                    _insert_row(conn, row)
        finally:
            conn.close()
        return run_id

    def _run_task(self, run_id: str, task: BenchmarkTask) -> dict[str, Any]:
        meter = _UsageMeter()

        def metered_factory(path: str) -> _MeteredWorker:
            return _MeteredWorker(self.worker_factory(path), meter, self.prompt_text)

        started_at, started = _utcnow(), time.monotonic()
        sampler = _PeakMemorySampler(self.read_rss, self.memory_sample_seconds)
        experiment: Experiment | None = None
        error: str | None = None
        service = ExperimentService(
            # The single-call pipeline has neither phase; passing them keeps the
            # constructor from advancing the provider rotation.
            selection_worker=object(),
            plan_worker_factory=metered_factory,
            worker_factory=metered_factory,
            db_path=self.db_path,
            repo_root=self.repo_root,
            worktrees_root=self.worktrees_root,
            test_python_executable=self.test_python_executable,
            experiment_log_dir=self.experiment_log_dir,
            base_ref=task.base_commit,
            test_command=task.test_command,
        )
        try:
            with sampler:
                experiment = service.create_experiment(task.goal)
                experiment = service.run_experiment(experiment.id)
        except Exception as exc:  # noqa: BLE001 - one broken task must not end the run
            error = str(exc)
        finally:
            try:
                if experiment is not None:
                    # The diff stays in the database; the checkout would fill the disk.
                    service.cleanup_worktree(experiment.id)
            finally:
                service.close()

        runtime_ms = int((time.monotonic() - started) * 1000)
        score = (
            score_experiment(task, experiment)
            if experiment is not None and error is None
            else TaskScore(0.0, False, 0, None)
        )
        provider = (experiment.implementation_provider if experiment else None) or self.provider
        cost, currency = self._estimate_cost(provider, meter, started_at)
        return {
            "run_id": run_id,
            "task_id": task.id,
            "task_sha256": task.sha256,
            "base_commit": task.base_commit,
            "provider": provider,
            "model": self.model,
            "prompt_name": self.prompt_name,
            "prompt_sha256": self.prompt_sha256,
            "experiment_id": experiment.id if experiment else None,
            "status": experiment.status if experiment and error is None else "error",
            "score": score.score,
            "tests_green": int(score.tests_green),
            "expected_files_touched": score.expected_files_touched,
            "expected_files_total": len(task.expected_files),
            "evaluation_outcome": score.evaluation_outcome,
            "input_tokens": meter.input_tokens,
            "output_tokens": meter.output_tokens,
            "runtime_ms": runtime_ms,
            "estimated_cost": cost,
            "currency": currency,
            "peak_memory_bytes": sampler.peak_bytes,
            "started_at": started_at,
            "ended_at": _utcnow(),
            "error": sanitized_reason(error or (experiment.error if experiment else None)),
        }

    def _estimate_cost(
        self, provider: str | None, meter: _UsageMeter, at_timestamp: str
    ) -> tuple[float | None, str | None]:
        if not provider or not self.model or meter.input_tokens is None:
            return None, None
        # Read-only: looking up a price must not create or change that database.
        try:
            conn = sqlite3.connect(f"file:{self.price_db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None, None
        try:
            price = get_applicable_price(conn, provider, self.model, at_timestamp)
        except sqlite3.Error:
            return None, None
        finally:
            conn.close()
        if price is None:
            return None, None
        cost = calculate_estimated_cost(
            meter.input_tokens,
            meter.output_tokens or 0,
            price["input_price_per_million_tokens"],
            price["output_price_per_million_tokens"],
        )
        return cost, price["currency"]


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _insert_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO benchmark_runs ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


# ---------------------------------------------------------------------------
# Baseline and report
# ---------------------------------------------------------------------------

VERDICT_BETTER = "better"
VERDICT_WORSE = "worse"
VERDICT_EQUAL = "equal"
VERDICT_NOT_COMPARABLE = "not comparable"


def load_run(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    """The rows of one run by task id."""
    cursor = conn.execute(
        "SELECT * FROM benchmark_runs WHERE run_id = ? ORDER BY task_id", (run_id,)
    )
    names = [column[0] for column in cursor.description]
    rows = {row["task_id"]: row for row in (dict(zip(names, raw)) for raw in cursor)}
    if not rows:
        raise BenchmarkError(f"no benchmark run {run_id!r}")
    return rows


def set_baseline(conn: sqlite3.Connection, run_id: str) -> None:
    load_run(conn, run_id)
    set_setting(conn, BASELINE_SETTING_KEY, run_id, updated_at=_utcnow())
    conn.commit()


def get_baseline(conn: sqlite3.Connection) -> str | None:
    return get_setting(conn, BASELINE_SETTING_KEY)


@dataclass
class BenchmarkComparison:
    run_id: str
    baseline_run_id: str
    run: dict[str, dict[str, Any]]
    baseline: dict[str, dict[str, Any]]
    verdict: str
    problems: list[str] = field(default_factory=list)


def compare_runs(
    conn: sqlite3.Connection, run_id: str, baseline_run_id: str
) -> BenchmarkComparison:
    """Compares by total score. A tie stays a tie; the report shows the costs.

    Scores only mean something over the same tasks, so a differing set or a task
    file changed between the runs gives no verdict instead of a misleading one.
    """
    run = load_run(conn, run_id)
    baseline = load_run(conn, baseline_run_id)
    problems = [
        f"{task_id}: only in {name}"
        for name, rows, other in (("baseline", baseline, run), ("run", run, baseline))
        for task_id in sorted(set(rows) - set(other))
    ]
    problems += [
        f"{task_id}: task definition changed"
        for task_id in sorted(set(run) & set(baseline))
        if run[task_id]["task_sha256"] != baseline[task_id]["task_sha256"]
    ]
    delta = sum(row["score"] for row in run.values()) - sum(
        row["score"] for row in baseline.values()
    )
    if problems:
        verdict = VERDICT_NOT_COMPARABLE
    elif delta > 0:
        verdict = VERDICT_BETTER
    elif delta < 0:
        verdict = VERDICT_WORSE
    else:
        verdict = VERDICT_EQUAL
    return BenchmarkComparison(run_id, baseline_run_id, run, baseline, verdict, problems)


def _setup(rows: dict[str, dict[str, Any]]) -> str:
    def values(key: str) -> str:
        return "+".join(sorted({str(row[key]) for row in rows.values() if row[key]})) or "default"

    return f"{values('provider')} / {values('model')} / prompt {values('prompt_name')}"


def _tokens(row: dict[str, Any]) -> int | None:
    if row["input_tokens"] is None and row["output_tokens"] is None:
        return None
    return (row["input_tokens"] or 0) + (row["output_tokens"] or 0)


def _total(values: list[Any]) -> Any:
    """A sum with a gap in it would understate the total, so it stays unknown."""
    return None if any(value is None for value in values) else sum(values)


def _cell(value: Any, spec: str = "") -> str:
    return "n/a" if value is None else format(value, spec)


def _pair(baseline: Any, run: Any, spec: str = "") -> str:
    return f"{_cell(baseline, spec)} -> {_cell(run, spec)}"


def _cost_cell(row: dict[str, Any]) -> str:
    if row["estimated_cost"] is None:
        return "n/a"
    return f"{row['estimated_cost']:.4f} {row['currency']}"


def _mib(row: dict[str, Any]) -> float | None:
    value = row["peak_memory_bytes"]
    return None if value is None else value / (1024 * 1024)


def _seconds(row: dict[str, Any]) -> float | None:
    return None if row["runtime_ms"] is None else row["runtime_ms"] / 1000


_MISSING_ROW = dict.fromkeys(
    ("status", "score", "input_tokens", "output_tokens", "runtime_ms",
     "estimated_cost", "peak_memory_bytes"),
)


def _report_line(
    label: str, baseline: dict[str, Any] | None, run: dict[str, Any] | None
) -> str:
    left, right = baseline or _MISSING_ROW, run or _MISSING_ROW
    return (
        f"| {label} "
        f"| {_pair(left['status'], right['status'])} "
        f"| {_pair(left['score'], right['score'], '.1f')} "
        f"| {_pair(_tokens(left), _tokens(right))} "
        f"| {_pair(_seconds(left), _seconds(right), '.0f')} "
        f"| {_cost_cell(left)} -> {_cost_cell(right)} "
        f"| {_pair(_mib(left), _mib(right), '.0f')} |"
    )


def _summary(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = list(rows.values())
    costs = [row["estimated_cost"] for row in values]
    currencies = {row["currency"] for row in values}
    peaks = [row["peak_memory_bytes"] for row in values if row["peak_memory_bytes"] is not None]
    return {
        "status": f"{sum(row['status'] == 'succeeded' for row in values)}/{len(values)} ok",
        "score": sum(row["score"] for row in values),
        "input_tokens": _total([row["input_tokens"] for row in values]),
        "output_tokens": _total([row["output_tokens"] for row in values]),
        "runtime_ms": sum(row["runtime_ms"] for row in values),
        "estimated_cost": None if len(currencies) > 1 else _total(costs),
        "currency": next(iter(currencies)),
        "peak_memory_bytes": max(peaks) if peaks else None,
    }


def format_report(comparison: BenchmarkComparison) -> str:
    """Markdown: every cell reads ``baseline -> run``."""
    lines = [
        f"# Benchmark run {comparison.run_id}",
        "",
        f"Baseline: {comparison.baseline_run_id} ({_setup(comparison.baseline)})",
        f"Run: {comparison.run_id} ({_setup(comparison.run)})",
        f"Verdict: {comparison.verdict}",
    ]
    if comparison.problems:
        lines += ["", "Not comparable because:", *[f"- {item}" for item in comparison.problems]]
    lines += [
        "",
        "| Task | Status | Score | Tokens | Runtime (s) | Est. cost | Peak memory (MiB) |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for task_id in sorted(set(comparison.baseline) | set(comparison.run)):
        lines.append(
            _report_line(task_id, comparison.baseline.get(task_id), comparison.run.get(task_id))
        )
    lines.append(
        _report_line("Total", _summary(comparison.baseline), _summary(comparison.run))
    )
    return "\n".join(lines) + "\n"
