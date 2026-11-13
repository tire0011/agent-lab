"""Hunk-level local shadow replay (#649): one merged symbol at a time.

Whole-experiment replays (#637) are too large for a 7B model on the VM: they
time out or overflow the context, so every variant scores 0. This mode cuts a
merged experiment into at most :data:`MAX_HUNK_GROUPS` *units* and scores the
local coder on each one.

The merged change is the diff ``old..new`` from
:func:`~modules.local_shadow_replay.merged_range`: the commit on main against
its first parent, or the experiment commit against its base commit. Every
text below comes from that one pair, so the prompt, the "applied" check, the
overlap and the scored tree all refer to the same files.

1. **Units.** The non-test ``.py`` files the merge added or modified are diffed
   with ``git diff -U0``. Each hunk goes to the *innermost* function or class
   of the merged file that encloses all its non-blank lines (decorators
   included), else to ``<module>``. Symbols are keyed by qualified name
   (``A.run`` and ``B.run`` are different units); a repeated name in the same
   scope gets ``#2``, ``#3``.
2. **Micro-task.** The worktree is a detached checkout of the merged commit.
   The merged test changes are undone (the model never sees them), and the
   unit's file is the merged file with *only this unit's hunks* turned back
   into the old lines. A file over 16 KiB is cut to its imports plus the
   enclosing top-level symbol. The task names the issue title, one goal line
   (merged commit summary + symbol) and the file. The local coding worker
   runs with ``split_tasks=False`` and its normal correction loop.
3. **Scoring.** The worker's file is read, then the worktree is reset to the
   merged commit (``reset --hard`` + ``clean``), so edits to other files are
   gone and every other merged file and hunk is back. The local version of
   the unit's symbol is spliced into the merged file. A local file that does
   not parse, or that lost the symbol, is a failed unit: never the merged
   text. Then ``py_compile`` runs, and the merged tests that touch the module
   run with ``pytest -k <module>`` (without ``-k`` if that selects nothing).
   Those tests first run on the plain merged tree; if they fail there, the
   unit is not counted as tested (``has_tests=0``).
4. **Storage.** One ``local_replay_units`` row per (experiment, file,
   symbol, variant) with a ``status``: ``scored`` (the worker finished inside
   its time limit), ``timeout``, ``local_too_slow`` (#667: the measured rates
   could not afford a useful patch, the model was not called) or ``error``. ``running`` marks a unit in
   progress. One ``local_replay_unit_runs`` row per (experiment, variant)
   says whether the experiment is ``done``, has ``no_units`` or hit a set-up
   ``error``, so the sweep never picks the same experiment forever.

Interruptions: a unit left ``running`` by a crashed process is retried once
(:data:`MAX_UNIT_ATTEMPTS`), then stored as ``error``. Finished units are
never run again, so a second call or a resumed sweep continues where the
last one stopped. The sweep (:func:`replay_next_unit`) does at most one unit
per tick.
"""

from __future__ import annotations

import ast
import difflib
import keyword
import logging
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from modules import local_shadow_replay as lsr
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

MAX_HUNK_GROUPS = 5
MODULE_SYMBOL = "<module>"
MAX_UNIT_ATTEMPTS = 2
ENV_UNIT_TIMEOUT = "NEXUS_SHADOW_REPLAY_UNIT_TIMEOUT_SECONDS"
DEFAULT_UNIT_TIMEOUT_SECONDS = 900.0
_MAX_MICRO_FILE_BYTES = 16 * 1024
_MAX_TITLE_CHARS = 200

UNIT_RUNNING = "running"
UNIT_SCORED = "scored"
UNIT_TIMEOUT = "timeout"
UNIT_ERROR = "error"
UNIT_LOCAL_TOO_SLOW = lsr.STATUS_LOCAL_TOO_SLOW

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_NO_UNITS = "no_units"
RUN_ERROR = "error"
FINISHED_RUN_STATUSES = (RUN_DONE, RUN_NO_UNITS, RUN_ERROR)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TIMEOUT_RE = re.compile(r"timed?[ _-]?out|timeout", re.IGNORECASE)


class SpliceError(ValueError):
    """The local file cannot be scored for this unit (does not parse, symbol missing)."""


def unit_timeout_seconds() -> float:
    return lsr._float_env(ENV_UNIT_TIMEOUT, DEFAULT_UNIT_TIMEOUT_SECONDS)


def unit_lease_seconds() -> float:
    """Lease for one unit: the worker, the baseline and the scored test run (#675)."""
    return lsr.lease_seconds(unit_timeout_seconds() + lsr.tests_timeout_seconds())


# ---- text and symbols --------------------------------------------------------

def _lines(text: str) -> list[str]:
    """Lines split on ``\\n`` only (as git counts them), with their line ends."""
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


@dataclass(frozen=True)
class Symbol:
    key: str
    start: int  # 1-based, first decorator line
    end: int    # 1-based, inclusive
    depth: int


def symbol_table(source: str) -> dict[str, Symbol]:
    """Every function/class keyed by unique qualified name; raises ``SyntaxError``."""
    tree = ast.parse(source)
    found: dict[str, Symbol] = {}
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                seen[qualname] = seen.get(qualname, 0) + 1
                key = qualname if seen[qualname] == 1 else f"{qualname}#{seen[qualname]}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                found[key] = Symbol(key, start, child.end_lineno or child.lineno, depth)
                visit(child, f"{qualname}.", depth + 1)
            else:
                visit(child, prefix, depth)

    visit(tree, "", 0)
    return found


def _innermost(table: dict[str, Symbol], first: int, last: int) -> Symbol | None:
    best: Symbol | None = None
    for item in table.values():
        if item.start <= first and item.end >= last and (
                best is None or item.end - item.start < best.end - best.start):
            best = item
    return best


def display_name(key: str) -> str:
    return re.sub(r"#\d+$", "", key)


# ---- units -------------------------------------------------------------------

@dataclass(frozen=True)
class Hunk:
    """One ``git diff -U0`` hunk as 0-based slices ``old[i1:i2]`` -> ``new[j1:j2]``."""

    i1: int
    i2: int
    j1: int
    j2: int


@dataclass(frozen=True)
class HunkUnit:
    """The merged hunks of one file that belong to one symbol."""

    file: str
    symbol: str
    hunks: tuple[Hunk, ...]
    old_text: str     # the file before the merge
    merged_text: str  # the file after the merge
    start_text: str   # merged file with only this unit's hunks reverted

    @property
    def is_module(self) -> bool:
        return self.symbol == MODULE_SYMBOL


def file_hunks(repo: str, old: str, new: str, path: str) -> list[Hunk]:
    out = lsr._git(repo, ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                          old, new, "--", path]).stdout
    hunks: list[Hunk] = []
    for line in out.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        a, b = int(match.group(1)), int(match.group(2) or 1)
        c, d = int(match.group(3)), int(match.group(4) or 1)
        i1 = a if b == 0 else a - 1
        j1 = c if d == 0 else c - 1
        hunks.append(Hunk(i1, i1 + b, j1, j1 + d))
    return hunks


def _apply(old_lines: list[str], new_lines: list[str], hunks: list[Hunk],
           reverted: set[Hunk]) -> str:
    """The new file, with the hunks in *reverted* turned back into their old lines."""
    out: list[str] = []
    pos = 0
    for hunk in hunks:
        out.extend(new_lines[pos:hunk.j1])
        out.extend(old_lines[hunk.i1:hunk.i2] if hunk in reverted else new_lines[hunk.j1:hunk.j2])
        pos = hunk.j2
    out.extend(new_lines[pos:])
    return "".join(out)


def _hunk_key(hunk: Hunk, old_lines: list[str], new_lines: list[str],
              old_table: dict[str, Symbol], new_table: dict[str, Symbol]) -> str:
    added = [n for n in range(hunk.j1, hunk.j2) if new_lines[n].strip()]
    if added:
        found = _innermost(new_table, added[0] + 1, added[-1] + 1)
        return found.key if found else MODULE_SYMBOL
    removed = [n for n in range(hunk.i1, hunk.i2) if old_lines[n].strip()]
    if removed:
        found = _innermost(old_table, removed[0] + 1, removed[-1] + 1)
        if found is not None and found.key in new_table:
            return found.key
    # A pure deletion sits between merged lines j1 and j1 + 1 (1-based).
    found = _innermost(new_table, hunk.j1, hunk.j1 + 1) if hunk.j1 >= 1 else None
    return found.key if found else MODULE_SYMBOL


def _changed_python_files(repo: str, old: str, new: str) -> list[str]:
    out = lsr._git(repo, ["diff", "--name-status", "-z", "--no-renames", "--no-ext-diff",
                          old, new]).stdout
    tokens = out.split("\0")
    paths: list[str] = []
    for i in range(0, len(tokens) - 1, 2):
        status, path = tokens[i], tokens[i + 1]
        if status[:1] in {"A", "M"} and path.endswith(".py") and not lsr._is_test_file(path):
            paths.append(path)
    return paths


def hunk_units(repo: str, old: str, new: str, limit: int = MAX_HUNK_GROUPS) -> list[HunkUnit]:
    """At most *limit* units: non-test Python hunks grouped by innermost merged symbol."""
    units: list[HunkUnit] = []
    for path in _changed_python_files(repo, old, new):
        old_text, new_text = lsr._source_at(repo, old, path), lsr._source_at(repo, new, path)
        try:
            new_table = symbol_table(new_text)
        except (SyntaxError, ValueError):
            logger.info("hunk replay: merged %s does not parse here; no units", path)
            continue
        try:
            old_table = symbol_table(old_text) if old_text else {}
        except (SyntaxError, ValueError):
            old_table = {}
        old_lines, new_lines = _lines(old_text), _lines(new_text)
        hunks = file_hunks(repo, old, new, path)
        if not hunks or _apply(old_lines, new_lines, hunks, set(hunks)) != old_text:
            logger.warning("hunk replay: the hunks of %s do not reproduce the old file; skipped", path)
            continue
        groups: dict[str, list[Hunk]] = {}
        for hunk in hunks:
            groups.setdefault(_hunk_key(hunk, old_lines, new_lines, old_table, new_table), []).append(hunk)
        for key, members in groups.items():
            start = _apply(old_lines, new_lines, hunks, set(members))
            units.append(HunkUnit(path, key, tuple(members), old_text, new_text, start))
            if len(units) >= limit:
                return units
    return units


# ---- splicing and similarity -------------------------------------------------

def _owned_top_level(unit: HunkUnit, table: dict[str, Symbol]) -> set[str]:
    """Top-level merged symbols a module-level unit's hunks reach into."""
    owned: set[str] = set()
    for hunk in unit.hunks:
        first, last = hunk.j1 + 1, max(hunk.j2, hunk.j1 + 1)
        for item in table.values():
            if item.depth == 0 and item.start <= last and item.end >= first:
                owned.add(item.key)
    return owned


def splice_unit(unit: HunkUnit, local_text: str) -> str:
    """The merged file with this unit's code taken from *local_text*.

    Raises :class:`SpliceError` when the local file does not parse or lacks a
    symbol it must have. Never falls back to the merged code.
    """
    try:
        local_table = symbol_table(local_text)
    except (SyntaxError, ValueError) as exc:
        raise SpliceError(f"the local {unit.file} does not parse: {exc}") from exc
    merged_table = symbol_table(unit.merged_text)
    local_lines, merged_lines = _lines(local_text), _lines(unit.merged_text)
    if not unit.is_module:
        local = local_table.get(unit.symbol)
        if local is None:
            raise SpliceError(f"the local {unit.file} has no {display_name(unit.symbol)}")
        merged = merged_table[unit.symbol]
        return "".join(merged_lines[:merged.start - 1] + local_lines[local.start - 1:local.end]
                       + merged_lines[merged.end:])
    # Module level: keep the local file, but every top-level symbol this unit
    # does not reach into comes back from the merged file.
    owned = _owned_top_level(unit, merged_table)
    replacements: list[tuple[Symbol, Symbol]] = []
    for key, merged in merged_table.items():
        if merged.depth != 0 or key in owned:
            continue
        local = local_table.get(key)
        if local is None:
            raise SpliceError(f"the local {unit.file} has no {display_name(key)}")
        replacements.append((local, merged))
    out = list(local_lines)
    for local, merged in sorted(replacements, key=lambda pair: pair[0].start, reverse=True):
        out[local.start - 1:local.end] = merged_lines[merged.start - 1:merged.end]
    return "".join(out)


def _change_set(before: str, after: str) -> set[tuple[str, str]]:
    a, b = _lines(before), _lines(after)
    changes: set[tuple[str, str]] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changes.update(("-", line.strip()) for line in a[i1:i2] if line.strip())
        changes.update(("+", line.strip()) for line in b[j1:j2] if line.strip())
    return changes


def changed_line_overlap(merged: set[tuple[str, str]], local: set[tuple[str, str]]) -> float:
    """Jaccard overlap of the changed (added/removed, non-blank) lines."""
    if not merged and not local:
        return 1.0
    return len(merged & local) / len(merged | local)


def _symbol_text(text: str, key: str) -> str | None:
    try:
        item = symbol_table(text).get(key)
    except (SyntaxError, ValueError):
        return None
    return None if item is None else "".join(_lines(text)[item.start - 1:item.end])


# ---- the micro-task ----------------------------------------------------------

def worker_start_text(unit: HunkUnit) -> tuple[str, bool]:
    """``(text, reduced)``: the file the worker starts from.

    Over 16 KiB, a symbol unit gets only the imports plus the enclosing
    top-level symbol, so the prompt stays small.
    """
    text = unit.start_text
    if unit.is_module or len(text.encode()) <= _MAX_MICRO_FILE_BYTES:
        return text, False
    try:
        tree = ast.parse(text)
        table = symbol_table(text)
    except (SyntaxError, ValueError):
        return text, False
    lines = _lines(text)
    kept: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kept.extend(lines[node.lineno - 1:node.end_lineno or node.lineno])
    merged_table = symbol_table(unit.merged_text)
    target = merged_table[unit.symbol]
    top = _innermost({k: v for k, v in merged_table.items() if v.depth == 0}, target.start, target.end)
    if top is not None and top.key in table:
        item = table[top.key]
        kept.extend(["\n", "\n"] + lines[item.start - 1:item.end])
    return "".join(kept), True


def issue_title(goal: str) -> str:
    for line in str(goal or "").splitlines():
        if line.strip():
            return line.strip()[:_MAX_TITLE_CHARS]
    return ""


def micro_task(title: str, summary: str, unit: HunkUnit, *, reduced: bool) -> str:
    if unit.is_module:
        what = "the module-level code (imports, constants, top-level statements)"
        goal = f"{summary}: module-level change in {unit.file}"
    else:
        name = display_name(unit.symbol)
        what = f"`{name}` (create it if it does not exist yet)"
        goal = f"{summary}: change `{name}` in {unit.file}"
    lines = [
        f"Issue: {title}",
        f"Goal: {goal}",
        "",
        f"Edit only {unit.file}. Change only {what}. Keep every other function and class "
        "exactly as it is. Do not add or change tests.",
    ]
    if reduced:
        lines.append(f"{unit.file} is large: the working copy holds only its imports and the "
                     "code around this change. Keep that shape.")
    return "\n".join(lines)


def hunk_worker_factory(client: Any) -> Any:
    """The correcting local worker with task splitting off (a micro-task is small)."""
    from modules.local_coding_worker import LocalCodingWorker, local_samples, sample_temperatures

    return LocalCodingWorker(client=client, split_tasks=False, planner=lsr._no_free_tier,
                             samples=local_samples(), sample_temperatures=sample_temperatures())


class UnitLogger:
    """Experiment logger for one unit: patch rounds and prompt/cached tokens."""

    def __init__(self) -> None:
        self.rounds = 0
        self.prompt_tokens = 0
        self.cached_tokens = 0

    def _record(self, _phase: str, event: str, _message: str = "", **fields: Any) -> None:
        if event == "local_patch_round":
            self.rounds += 1
        elif event == "local_model_call":
            total = fields.get("prompt_tokens_total")
            if not isinstance(total, int) or isinstance(total, bool):
                total = fields.get("prompt_tokens")
            if isinstance(total, int) and not isinstance(total, bool):
                self.prompt_tokens += max(0, total)
            cached = fields.get("prompt_tokens_cached")
            if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                self.cached_tokens += max(0, int(cached))

    info = warning = error = _record


# ---- tests -------------------------------------------------------------------

def module_name(path: str) -> str:
    p = Path(path)
    return p.parent.name if p.stem == "__init__" else p.stem


def unit_test_files(repo: str, new: str, changes: list[lsr.MergedTestChange], path: str) -> list[str]:
    """Merged test files (under ``tests/``) that name the unit's module."""
    module = module_name(path)
    if not module:
        return []
    selected: list[str] = []
    for change in changes:
        if change.delete or not lsr._TEST_FILE_RE.match(Path(change.path).name):
            continue
        content = lsr._source_at(repo, new, change.path)
        if module in Path(change.path).stem or re.search(rf"\b{re.escape(module)}\b", content):
            selected.append(change.path)
    return selected


def _usable_k(module: str) -> bool:
    return module.isidentifier() and not keyword.iskeyword(module) and module not in {"and", "or", "not"}


def run_unit_tests(worktree: str, tests: list[str], module: str, timeout: float) -> lsr.MergedTestRun:
    """Run *tests* with ``pytest -k module``; without ``-k`` if that selects nothing."""
    if not tests:
        return lsr.MergedTestRun(0, 0, "no merged tests touch this module")
    with tempfile.TemporaryDirectory(prefix="nexus-shadow-unit-tests-") as tmp:
        report = Path(tmp) / "junit.xml"
        env = lsr._test_env(tmp)
        selections = ([["-k", module]] if _usable_k(module) else []) + [[]]
        for selection in selections:
            report.unlink(missing_ok=True)
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   f"--junitxml={report}", *selection, *tests]
            code, output = lsr._run_bounded(cmd, worktree, env, timeout)
            if code is None:
                return lsr.MergedTestRun(0, 0, f"unit tests timed out after {timeout:g}s")
            if code == 5 and selection:
                continue  # -k selected nothing: run the selected files whole
            if not report.is_file():
                return lsr.MergedTestRun(0, 0, f"pytest exited {code} without a report: {output.strip()[-500:]}")
            try:
                total, passed = lsr.parse_junit(str(report))
            except Exception as exc:  # noqa: BLE001 - an unreadable report is a failed run
                return lsr.MergedTestRun(0, 0, f"unreadable pytest report: {exc}")
            return lsr.MergedTestRun(total, passed, None if code == 0 else output.strip()[-500:])
    return lsr.MergedTestRun(0, 0, "pytest selected no tests")


def _all_passed(run: lsr.MergedTestRun) -> bool:
    return run.total > 0 and run.passed == run.total and not run.error


# ---- worktree steps ----------------------------------------------------------

def reset_to_merged(worktree: str) -> None:
    """Back to the detached merged commit: every merged file and hunk, nothing else."""
    lsr._git(worktree, ["reset", "-q", "--hard", "HEAD"], timeout=120)
    lsr._git(worktree, ["clean", "-q", "-ffdx"], timeout=120)


def hide_merged_tests(repo: str, worktree: str, old: str, new: str) -> None:
    """Undo the merged test changes so the model never sees them."""
    out = lsr._git(repo, ["diff", "--name-status", "-z", "--no-renames", "--no-ext-diff",
                          old, new]).stdout
    tokens = out.split("\0")
    root = Path(worktree).resolve()
    for i in range(0, len(tokens) - 1, 2):
        status, path = tokens[i], tokens[i + 1]
        if not lsr._is_test_file(path):
            continue
        target = lsr._inside(root, path)
        if target is None:
            raise lsr.ReplayError(f"refusing test path outside the worktree: {path}")
        if status[:1] == "A":
            if target.is_symlink() or target.is_file():
                target.unlink()
            continue
        content = lsr._git(repo, ["show", f"{old}:{path}"], text=False).stdout
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _write_text(root: str, rel: str, text: str) -> None:
    target = lsr._inside(Path(root).resolve(), rel)
    if target is None:
        raise lsr.ReplayError(f"refusing path outside the worktree: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read_text(root: str, rel: str) -> str:
    target = lsr._inside(Path(root).resolve(), rel)
    if target is None or not target.is_file():
        return ""
    with open(target, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


# ---- storage -----------------------------------------------------------------

_UNIT_KEY = "experiment_id=? AND file=? AND symbol=? AND prompt_variant=?"


def _run_row(conn: sqlite3.Connection, experiment_id: str, variant: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM local_replay_unit_runs WHERE experiment_id=? AND prompt_variant=?",
        (experiment_id, variant),
    ).fetchone()


def _start_run(conn: sqlite3.Connection, experiment_id: str, variant: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO local_replay_unit_runs (experiment_id, prompt_variant, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        (experiment_id, variant, RUN_RUNNING, lsr._iso(lsr._utcnow())),
    )


def _finish_run(conn: sqlite3.Connection, experiment_id: str, variant: str, status: str, *,
                units: int = 0, error: str | None = None, merged_commit: str | None = None) -> None:
    conn.execute(
        "UPDATE local_replay_unit_runs SET status=?, units=?, error=?, merged_commit=?, finished_at=? "
        "WHERE experiment_id=? AND prompt_variant=? AND status=?",
        (status, units, error[:lsr._MAX_ERROR_CHARS] if error else None, merged_commit,
         lsr._iso(lsr._utcnow()), experiment_id, variant, RUN_RUNNING),
    )


def _claim_unit(conn: sqlite3.Connection, experiment_id: str, variant: str, unit: HunkUnit,
                model: str) -> bool:
    """True when this unit should run now; finished units and spent retries say False.

    Only the holder of the global replay lease calls this, so a ``running``
    row always belongs to a process that stopped before finishing it.
    """
    key = (experiment_id, unit.file, unit.symbol, variant)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(f"SELECT status, attempts FROM local_replay_units WHERE {_UNIT_KEY}", key).fetchone()
        now = lsr._iso(lsr._utcnow())
        if row is not None and row["status"] != UNIT_RUNNING:
            conn.execute("COMMIT")
            return False
        if row is None:
            conn.execute(
                "INSERT INTO local_replay_units (experiment_id, file, symbol, prompt_variant, model, "
                "status, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (*key, model, UNIT_RUNNING, now),
            )
        elif int(row["attempts"] or 0) >= MAX_UNIT_ATTEMPTS:
            conn.execute(
                f"UPDATE local_replay_units SET status=?, error=?, finished_at=? WHERE {_UNIT_KEY}",
                (UNIT_ERROR, f"interrupted {int(row['attempts'])} times before the unit finished",
                 now, *key),
            )
            conn.execute("COMMIT")
            return False
        else:
            logger.warning("hunk replay: retrying interrupted unit %s %s:%s",
                           experiment_id, unit.file, unit.symbol)
            conn.execute(
                f"UPDATE local_replay_units SET attempts=attempts+1, created_at=?, model=? WHERE {_UNIT_KEY}",
                (now, model, *key),
            )
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@dataclass
class UnitOutcome:
    status: str = UNIT_ERROR
    applied: bool = False
    compiled: bool = False
    has_tests: bool = False
    tests_passed: bool = False
    tests_total: int = 0
    tests_ok: int = 0
    overlap: float = 0.0
    seconds: float = 0.0
    rounds: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    error: str | None = None


def _store_unit(conn: sqlite3.Connection, experiment_id: str, variant: str, unit: HunkUnit,
                outcome: UnitOutcome) -> None:
    conn.execute(
        "UPDATE local_replay_units SET status=?, applied=?, compiled=?, has_tests=?, tests_passed=?, "
        "tests_total=?, tests_ok=?, overlap=?, seconds=?, rounds=?, prompt_tokens=?, cached_tokens=?, "
        f"error=?, finished_at=? WHERE {_UNIT_KEY} AND status=?",
        (outcome.status, int(outcome.applied), int(outcome.compiled), int(outcome.has_tests),
         int(outcome.tests_passed), int(outcome.tests_total), int(outcome.tests_ok),
         round(float(outcome.overlap), 6), round(float(outcome.seconds), 3), int(outcome.rounds),
         int(outcome.prompt_tokens), int(outcome.cached_tokens),
         outcome.error[:lsr._MAX_ERROR_CHARS] if outcome.error else None, lsr._iso(lsr._utcnow()),
         experiment_id, unit.file, unit.symbol, variant, UNIT_RUNNING),
    )


def _unit_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("applied", "compiled", "has_tests", "tests_passed"):
        data[key] = bool(data.get(key))
    return data


def stored_units(conn: sqlite3.Connection, experiment_id: str, variant: str) -> list[dict[str, Any]]:
    return [_unit_dict(row) for row in conn.execute(
        "SELECT * FROM local_replay_units WHERE experiment_id=? AND prompt_variant=? ORDER BY id",
        (experiment_id, variant),
    ).fetchall()]


def _summary(conn: sqlite3.Connection, experiment_id: str, variant: str, ran: int) -> dict[str, Any]:
    run = _run_row(conn, experiment_id, variant)
    units = stored_units(conn, experiment_id, variant)
    return {
        "experiment_id": experiment_id, "prompt_variant": variant,
        "status": run["status"] if run is not None else RUN_RUNNING,
        "error": run["error"] if run is not None else None,
        "ran_units": ran, "units": units,
        "tests_passed": sum(1 for u in units if u["tests_passed"]),
        "tests_total": len(units),
    }


# ---- one unit ----------------------------------------------------------------

def _score_unit(repo: str, worktree: str, unit: HunkUnit, *, old: str, new: str, title: str,
                summary: str, client: Any, worker_factory: Callable[[Any], Any],
                tests: list[str], test_runner: Callable[[str, list[str], float], lsr.MergedTestRun],
                baseline: dict[str, lsr.MergedTestRun]) -> UnitOutcome:
    outcome = UnitOutcome()
    timeout = unit_timeout_seconds()
    tests_timeout = lsr.tests_timeout_seconds()

    reset_to_merged(worktree)
    if unit.file not in baseline:
        baseline[unit.file] = (test_runner(worktree, tests, tests_timeout) if tests
                               else lsr.MergedTestRun(0, 0, "no merged tests touch this module"))
    base_run = baseline[unit.file]
    outcome.has_tests = _all_passed(base_run)
    notes: list[str] = []
    if not outcome.has_tests:
        notes.append("not tested: " + (base_run.error or
                                       f"merged tests fail on the merged tree ({base_run.passed}/{base_run.total})"))

    # The model's view: merged tree, merged tests undone, this unit's hunks reverted.
    hide_merged_tests(repo, worktree, old, new)
    start_text, reduced = worker_start_text(unit)
    _write_text(worktree, unit.file, start_text)
    log = UnitLogger()
    worker = worker_factory(client)
    started = time.monotonic()
    try:
        result = worker.run(
            task=micro_task(title, summary, unit, reduced=reduced), working_dir=worktree,
            sandbox="workspace-write", timeout_seconds=timeout, experiment_logger=log,
            phase="local_shadow_replay_hunk",
        )
    except Exception as exc:  # noqa: BLE001 - stored as this unit's error
        outcome.seconds = time.monotonic() - started
        outcome.rounds, outcome.prompt_tokens, outcome.cached_tokens = (
            log.rounds, log.prompt_tokens, log.cached_tokens)
        outcome.error = f"local worker raised {type(exc).__name__}: {exc}"
        return outcome
    outcome.seconds = time.monotonic() - started
    outcome.rounds, outcome.prompt_tokens, outcome.cached_tokens = (
        log.rounds, log.prompt_tokens, log.cached_tokens)
    usage = getattr(result, "usage", None)
    if not outcome.prompt_tokens and isinstance(usage, dict):
        outcome.prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    worker_error = None if getattr(result, "success", False) else str(
        getattr(result, "error", None) or "local worker did not succeed")
    timed_out = bool(getattr(result, "timed_out", False)) or outcome.seconds >= timeout or bool(
        worker_error and _TIMEOUT_RE.search(worker_error))
    outcome.status = UNIT_TIMEOUT if timed_out else UNIT_SCORED
    if worker_error and worker_error.startswith(UNIT_LOCAL_TOO_SLOW):
        outcome.status = UNIT_LOCAL_TOO_SLOW
    if worker_error:
        notes.insert(0, worker_error)

    local_text = _read_text(worktree, unit.file)
    # Back to merged: edits to other files are dropped, every other hunk returns.
    reset_to_merged(worktree)
    merged_changes = _change_set(unit.start_text, unit.merged_text)
    try:
        scored = splice_unit(unit, local_text)
    except SpliceError as exc:
        outcome.applied = local_text != start_text
        outcome.overlap = changed_line_overlap(merged_changes, _change_set(start_text, local_text))
        notes.insert(0, str(exc))
        outcome.error = "; ".join(notes)
        return outcome
    if unit.is_module:
        region_changed = scored != unit.start_text
    else:
        region_changed = _symbol_text(local_text, unit.symbol) != _symbol_text(start_text, unit.symbol)
    outcome.applied = local_text != start_text and region_changed
    outcome.overlap = changed_line_overlap(merged_changes, _change_set(unit.start_text, scored))
    if outcome.applied:
        _write_text(worktree, unit.file, scored)
        outcome.compiled = lsr._compiles(worktree, [unit.file])
        if outcome.compiled and outcome.has_tests:
            run = test_runner(worktree, tests, tests_timeout)
            outcome.tests_total, outcome.tests_ok = int(run.total), int(run.passed)
            outcome.tests_passed = _all_passed(run)
            if run.error:
                notes.append(run.error)
        elif not outcome.compiled:
            notes.append("the spliced file does not compile")
    else:
        notes.append("the local patch did not change this unit")
    outcome.error = "; ".join(notes) if notes else None
    return outcome


# ---- the replay --------------------------------------------------------------

def replay_hunks(
    experiment_id: str,
    *,
    repo_root: str,
    db_path: str = DEFAULT_DB_PATH,
    variant: str | None = None,
    client: Any | None = None,
    worker_factory: Callable[[Any], Any] | None = None,
    test_runner: Callable[[str, list[str], float], lsr.MergedTestRun] | None = None,
    max_units: int | None = None,
    _global_lease: lsr.GlobalLease | None = None,
) -> dict[str, Any] | None:
    """Score the merged units of one experiment; at most *max_units* new ones.

    Returns the experiment summary (run status and every stored unit row), or
    ``None`` when another replay holds the global lease. Raises ``ValueError``
    for an experiment that is unknown, not merged or not paid. Every later
    failure is stored (as the run's or the unit's error).
    """
    variant = variant or lsr.prompt_variant()
    conn = lsr._connect(db_path)
    owned_lease = _global_lease is None
    lease = _global_lease or lsr._acquire_global_lease(conn, lsr.lease_seconds())
    if lease is None:
        conn.close()
        return None
    ran = 0
    try:
        lsr.cleanup_orphaned_worktree(repo_root, lease.orphaned_worktree)
        row, adoption = lsr._load(conn, experiment_id)
        existing = _run_row(conn, experiment_id, variant)
        if existing is not None and existing["status"] in FINISHED_RUN_STATUSES:
            return _summary(conn, experiment_id, variant, 0)
        _start_run(conn, experiment_id, variant)
        new: str | None = None
        try:
            base = str(row["base_commit_sha"]).strip()
            old, new = lsr.merged_range(repo_root, adoption["integrated_commit_sha"],
                                        adoption["experiment_commit_sha"], base)
            units = hunk_units(repo_root, old, new)
            if not units:
                _finish_run(conn, experiment_id, variant, RUN_NO_UNITS, merged_commit=new,
                            error="the merged diff changes no non-test Python code")
                return _summary(conn, experiment_id, variant, 0)
            summary = lsr._git(repo_root, ["show", "-s", "--format=%s", new]).stdout.strip()
            title = issue_title(str(row["goal"] or ""))
            test_changes = lsr.merged_test_changes(repo_root, old, new)
            client = client if client is not None else lsr._UsageCountingClient(
                timeout=lsr.call_timeout_seconds())
            model = str(getattr(client, "model", "") or "")
            factory = worker_factory or hunk_worker_factory
            baseline: dict[str, lsr.MergedTestRun] = {}
            worktree: str | None = None
            try:
                for unit in units:
                    if max_units is not None and ran >= max_units:
                        break
                    # #675: the lease must outlast this unit's worker and both test runs.
                    if not lsr.renew_lease(db_path, lease.token, unit_lease_seconds()):
                        logger.warning("hunk replay %s: lost the global lease; stopping",
                                       experiment_id)
                        break
                    if not _claim_unit(conn, experiment_id, variant, unit, model):
                        continue
                    if worktree is None:
                        worktree = lsr._create_worktree(repo_root, new, db_path=db_path,
                                                        lease_token=lease.token)
                    tests = unit_test_files(repo_root, new, test_changes, unit.file)
                    module = module_name(unit.file)
                    runner = test_runner or (lambda wt, t, timeout, _module=module:
                                             run_unit_tests(wt, t, _module, timeout))
                    ran += 1
                    try:
                        outcome = _score_unit(
                            repo_root, worktree, unit, old=old, new=new, title=title, summary=summary,
                            client=client, worker_factory=factory, tests=tests, test_runner=runner,
                            baseline=baseline,
                        )
                    except Exception as exc:  # one bad unit must not stop the others
                        logger.warning("hunk replay %s %s:%s failed: %s",
                                       experiment_id, unit.file, unit.symbol, exc)
                        outcome = UnitOutcome(status=UNIT_ERROR, error=f"{type(exc).__name__}: {exc}")
                    _store_unit(conn, experiment_id, variant, unit, outcome)
            finally:
                if worktree is not None:
                    lsr.remove_worktree(repo_root, worktree)
                    lsr._set_lease_worktree(db_path, lease.token, None)
            stored = {(u["file"], u["symbol"]): u["status"] for u in stored_units(conn, experiment_id, variant)}
            if all(stored.get((u.file, u.symbol)) not in (None, UNIT_RUNNING) for u in units):
                _finish_run(conn, experiment_id, variant, RUN_DONE, units=len(units), merged_commit=new)
        except Exception as exc:  # stored, so a broken case is not retried every tick
            message = str(exc) if isinstance(exc, lsr.ReplayError) else f"{type(exc).__name__}: {exc}"
            logger.warning("hunk replay of %s failed: %s", experiment_id, message)
            _finish_run(conn, experiment_id, variant, RUN_ERROR, error=message, merged_commit=new)
        return _summary(conn, experiment_id, variant, ran)
    finally:
        if owned_lease:
            lsr._release_global_lease(conn, lease.token)
        conn.close()


# ---- the sweep ---------------------------------------------------------------

def next_hunk_candidate(conn: sqlite3.Connection, variant: str) -> str | None:
    """The oldest merged paid experiment whose hunk replay is not finished for *variant*.

    An experiment with a finished run row (``done``, ``no_units``, ``error``)
    is never picked again; an unfinished one is resumed first.
    """
    found = conn.execute(
        f"""SELECT e.id FROM experiments AS e
            JOIN experiment_adoptions AS a ON a.experiment_id=e.id
            WHERE a.status=?
              AND e.implementation_provider IN ({','.join('?' for _ in lsr.PAID_PROVIDERS)})
              AND TRIM(COALESCE(e.base_commit_sha, '')) != ''
              AND NOT EXISTS (SELECT 1 FROM local_replay_unit_runs r
                              WHERE r.experiment_id=e.id AND r.prompt_variant=?
                                AND r.status IN ({','.join('?' for _ in FINISHED_RUN_STATUSES)}))
            ORDER BY COALESCE(a.ended_at, a.updated_at, e.created_at), e.id LIMIT 1""",
        (lsr.MERGED_ADOPTION_STATUS, *lsr.PAID_PROVIDERS, variant, *FINISHED_RUN_STATUSES),
    ).fetchone()
    return str(found[0]) if found else None


# ---- read side ---------------------------------------------------------------

def unit_variant_summary(*, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Per prompt variant: completion, timeouts, pass rate, overlap and token cost of the units.

    ``units`` counts finished units (not ``running``). ``completion_rate`` is
    the share that finished inside the worker time limit (status ``scored``).
    ``unit_pass_rate`` is tests_passed/units; ``tested_pass_rate`` only over
    units whose merged tests pass on the merged tree.
    """
    conn = lsr._read_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT prompt_variant, COUNT(DISTINCT experiment_id) experiments, "
            "SUM(status != ?) units, SUM(status = ?) running, SUM(status = ?) completed, "
            "SUM(status = ?) timeouts, SUM(status = ?) errors, "
            "SUM(CASE WHEN status != ? THEN applied ELSE 0 END) applied, "
            "SUM(CASE WHEN status != ? THEN compiled ELSE 0 END) compiled, "
            "SUM(CASE WHEN status != ? THEN has_tests ELSE 0 END) tested, "
            "SUM(CASE WHEN status != ? THEN tests_passed ELSE 0 END) passed, "
            "AVG(CASE WHEN status != ? THEN overlap END) overlap, "
            "AVG(CASE WHEN status != ? THEN seconds END) seconds, "
            "SUM(prompt_tokens) prompt_tokens, SUM(cached_tokens) cached_tokens "
            "FROM local_replay_units GROUP BY prompt_variant ORDER BY prompt_variant",
            (UNIT_RUNNING, UNIT_RUNNING, UNIT_SCORED, UNIT_TIMEOUT, UNIT_ERROR,
             *([UNIT_RUNNING] * 6)),
        ).fetchall()
        runs = {(r["prompt_variant"], r["status"]): int(r["n"]) for r in conn.execute(
            "SELECT prompt_variant, status, COUNT(*) n FROM local_replay_unit_runs "
            "GROUP BY prompt_variant, status").fetchall()}
    except sqlite3.OperationalError:
        rows, runs = [], {}
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        units = int(row["units"] or 0)
        tested = int(row["tested"] or 0)
        passed = int(row["passed"] or 0)
        out.append({
            "prompt_variant": row["prompt_variant"],
            "experiments": int(row["experiments"] or 0),
            "units": units,
            "running": int(row["running"] or 0),
            "completed": int(row["completed"] or 0),
            "timeouts": int(row["timeouts"] or 0),
            "errors": int(row["errors"] or 0),
            "completion_rate": round(int(row["completed"] or 0) / units, 4) if units else None,
            "applied": int(row["applied"] or 0),
            "compiled": int(row["compiled"] or 0),
            "tested_units": tested,
            "tests_passed": passed,
            "unit_pass_rate": round(passed / units, 4) if units else None,
            "tested_pass_rate": round(passed / tested, 4) if tested else None,
            "average_overlap": round(float(row["overlap"] or 0), 4),
            "average_seconds": round(float(row["seconds"] or 0), 3),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "cached_tokens": int(row["cached_tokens"] or 0),
            "experiments_without_units": runs.get((row["prompt_variant"], RUN_NO_UNITS), 0),
            "experiment_errors": runs.get((row["prompt_variant"], RUN_ERROR), 0),
        })
    return out
