"""Handover fact check: names in an issue checked against the repo (#735).

Issue texts and goals name paths, modules, scripts and ``file.py:123`` line
references that are often stale: ``modules/run_store.py`` is the package
``modules/run_store/``, a script was never written, a function moved down by
nine lines.  Paid coders then lose their first turns to ``ls``/``find``
rounds, and the local model cannot explore at all.

:func:`check_references` extracts those names and resolves each one
deterministically to ``exists``, ``moved``, ``line_shifted`` or ``missing``.
Only a ``moved`` match with several candidates may cost one bounded local
llama call (JSON schema answer, a seconds budget like
:mod:`modules.local_file_localizer`); without a usable answer all candidates
are listed.  :func:`facts_block` renders the one ``Repo facts:`` block both
the paid handover and the local worker prompt get; references that exist add
nothing to it.  The flag ``NEXUS_HANDOVER_FACT_CHECK`` is on by default.
"""

from __future__ import annotations

import asyncio
import builtins
import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

ENV_HANDOVER_FACT_CHECK = "NEXUS_HANDOVER_FACT_CHECK"
REPO_FACTS_HEADING = "Repo facts:"
EVENT = "handover_fact_check"
STATUSES = ("exists", "moved", "line_shifted", "missing")
# The one disambiguation call gets this many seconds and this many tokens.
DISAMBIGUATE_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 80
# At most this many ambiguous references go into that one call; the rest are listed.
MAX_AMBIGUOUS_IN_CALL = 5
MAX_CANDIDATES = 8
MAX_REFERENCES = 40
_FALSE = {"0", "false", "no", "off"}
_SKIP_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "state"}
_EXTENSIONS = ("py", "md", "sh", "toml", "json", "yml", "yaml", "txt", "cfg", "ini",
               "service", "timer", "js", "ts", "html", "css", "sql", "jsonl")
_EXT = "|".join(_EXTENSIONS)
# ``modules/x.py``, ``tests/test_*.py``, ``x.py`` and ``x.py:123`` (or ``:123-130``).
_PATH_RE = re.compile(
    r"(?<![\w/.<>*{}$-])((?:[\w.*-]+/)*[\w*-][\w.*-]*\.(?:" + _EXT + r"))"
    r"(?::(\d+)(?:-\d+)?)?(?![\w/])"
)
_MODULE_RE = re.compile(r"(?<![\w./-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?![\w(/-])")
_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\(\)")
_BUILTINS = frozenset(dir(builtins))


def enabled() -> bool:
    """On unless ``NEXUS_HANDOVER_FACT_CHECK`` is explicitly false."""
    return os.getenv(ENV_HANDOVER_FACT_CHECK, "").strip().lower() not in _FALSE


@dataclass
class Reference:
    """One name from the text and what the repo says about it."""

    token: str
    kind: str  # "path", "module", "line", "symbol"
    status: str
    path: str | None = None  # the resolved repo path, when there is one
    line: int | None = None  # the named line (line refs)
    new_line: int | None = None  # where the symbol is now (line_shifted)
    symbol: str | None = None
    candidates: list[str] = field(default_factory=list)
    chosen_by: str | None = None  # "only" | "llama" | None (all candidates listed)
    note: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.status == "moved" and len(self.candidates) > 1


# Repository index


class _Repo:
    """Every file in the repo, read once per check."""

    def __init__(self, repo: str | Path):
        self.root = Path(repo).resolve()
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_PARTS)
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            for name in sorted(filenames):
                files.append(name if rel_dir == "." else f"{rel_dir}/{name}")
        self.files = files
        self.file_set = set(files)
        self.dirs = {parent.as_posix() for f in files for parent in Path(f).parents}
        self._text: dict[str, list[str]] = {}

    def is_file(self, rel: str) -> bool:
        return rel in self.file_set

    def lines(self, rel: str) -> list[str]:
        if rel not in self._text:
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._text[rel] = text.splitlines()
        return self._text[rel]

    def by_basename(self, name: str) -> list[str]:
        return [f for f in self.files if f.rsplit("/", 1)[-1] == name]

    def packages(self, name: str) -> list[str]:
        suffix = f"{name}/__init__.py"
        return [f for f in self.files if f == suffix or f.endswith("/" + suffix)]

    def glob(self, pattern: str) -> list[str]:
        return [f for f in self.files if fnmatch.fnmatchcase(f, pattern)]

    def symbol_line(self, rel: str, symbol: str) -> int | None:
        """1-based line of ``def``/``class``/assignment of *symbol* in *rel*."""
        definition = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(symbol) + r"\b")
        assignment = re.compile(r"^" + re.escape(symbol) + r"\s*(?::[^=]*)?=")
        lines = self.lines(rel)
        for pattern in (definition, assignment):
            for number, text in enumerate(lines, 1):
                if pattern.search(text):
                    return number
        return None

    def defining_files(self, symbol: str) -> list[str]:
        return [f for f in self.files if f.endswith(".py") and self.symbol_line(f, symbol)]


def _norm(token: str) -> str:
    return token.strip().strip("`'\"").replace("\\", "/").removeprefix("./")


def _module_path(repo: _Repo, dotted: str) -> tuple[str | None, str | None]:
    """``(file, attribute)`` for ``a.b.c``: ``a/b/c.py``, ``a/b/c/__init__.py`` or ``a/b.py`` + ``c``."""
    parts = dotted.split(".")
    base = "/".join(parts)
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if repo.is_file(rel):
            return rel, None
    if len(parts) > 2 or (len(parts) == 2 and parts[0] in repo.dirs):
        parent = "/".join(parts[:-1])
        for rel in (f"{parent}.py", f"{parent}/__init__.py"):
            if repo.is_file(rel) and repo.symbol_line(rel, parts[-1]):
                return rel, parts[-1]
    return None, None


def _moved_candidates(repo: _Repo, rel: str) -> list[str]:
    """Where a missing ``dir/name.py`` might be now; the sibling package comes first."""
    name = rel.rsplit("/", 1)[-1]
    stem, _, suffix = name.rpartition(".")
    candidates: list[str] = []
    if suffix == "py":
        sibling = (rel[: -len(".py")] + "/__init__.py")
        if repo.is_file(sibling):
            return [sibling]
        candidates += repo.packages(stem)
    candidates += repo.by_basename(name)
    return list(dict.fromkeys(c for c in candidates if c != rel))[:MAX_CANDIDATES]


# Extraction and deterministic resolution


def _resolve_path(repo: _Repo, token: str) -> Reference:
    rel = _norm(token)
    if "*" in rel:
        matches = repo.glob(rel)
        return Reference(token=rel, kind="path", status="exists" if matches else "missing",
                         path=matches[0] if matches else None, candidates=matches[:MAX_CANDIDATES])
    if repo.is_file(rel):
        return Reference(token=rel, kind="path", status="exists", path=rel)
    candidates = _moved_candidates(repo, rel)
    if not candidates:
        return Reference(token=rel, kind="path", status="missing")
    if "/" not in rel and len(candidates) == 1:
        # A bare ``x.py`` with one match is a usual shorthand, not a stale name.
        return Reference(token=rel, kind="path", status="exists", path=candidates[0],
                         candidates=candidates, chosen_by="only")
    return Reference(token=rel, kind="path", status="moved",
                     path=candidates[0] if len(candidates) == 1 else None,
                     candidates=candidates, chosen_by="only" if len(candidates) == 1 else None)


def _resolve_module(repo: _Repo, dotted: str) -> Reference | None:
    if dotted.split(".", 1)[0] not in repo.dirs:
        return None  # ``e.g``, ``os.path``, ``self.client``: not a repo module
    rel, attribute = _module_path(repo, dotted)
    if rel is not None:
        return Reference(token=dotted, kind="module", status="exists", path=rel, symbol=attribute)
    name = dotted.rsplit(".", 1)[-1]
    candidates = list(dict.fromkeys(repo.packages(name) + repo.by_basename(f"{name}.py")))
    candidates = candidates[:MAX_CANDIDATES]
    if not candidates:
        return Reference(token=dotted, kind="module", status="missing")
    return Reference(token=dotted, kind="module", status="moved",
                     path=candidates[0] if len(candidates) == 1 else None, candidates=candidates,
                     chosen_by="only" if len(candidates) == 1 else None)


def _resolve_line(repo: _Repo, ref: Reference, line: int, symbol: str | None) -> Reference:
    """Turn a resolved path reference into a ``file.py:123`` reference."""
    ref.kind, ref.line, ref.symbol = "line", line, symbol
    ref.token = f"{ref.token}:{line}"
    if ref.status == "missing" or ref.path is None:
        return ref
    lines = repo.lines(ref.path)
    if symbol:
        found = repo.symbol_line(ref.path, symbol)
        if found is None:
            elsewhere = [f for f in repo.defining_files(symbol) if f != ref.path][:MAX_CANDIDATES]
            if not elsewhere:
                ref.status = "missing"
                ref.note = f"`{symbol}` is not defined in {ref.path}"
                return ref
            ref.status, ref.candidates = "moved", elsewhere
            if len(elsewhere) == 1:
                ref.path, ref.chosen_by = elsewhere[0], "only"
                ref.new_line = repo.symbol_line(elsewhere[0], symbol)
            else:
                ref.path, ref.chosen_by = None, None
            return ref
        if found != line:
            ref.status, ref.new_line = "line_shifted", found
        return ref
    if line > len(lines):
        ref.status = "line_shifted"
        ref.note = f"{ref.path} has only {len(lines)} lines"
    return ref


def _resolve_symbol(repo: _Repo, symbol: str, context: Reference) -> Reference:
    ref = Reference(token=f"{symbol}()", kind="symbol", status="exists", symbol=symbol)
    target = context.path if context.status != "missing" else None
    if target and target.endswith(".py"):
        found = repo.symbol_line(target, symbol)
        if found:
            ref.path, ref.new_line = target, found
            return ref
    elsewhere = repo.defining_files(symbol)[:MAX_CANDIDATES]
    if not elsewhere:
        ref.status = "missing"
        ref.note = "not defined anywhere in the repo"
        return ref
    ref.status = "moved"
    ref.candidates = elsewhere
    if len(elsewhere) == 1:
        ref.path, ref.chosen_by = elsewhere[0], "only"
        ref.new_line = repo.symbol_line(elsewhere[0], symbol)
    return ref


def _extract(repo: _Repo, text: str) -> list[tuple[Reference, str]]:
    """Every referenced name in *text* once, with the line it came from."""
    refs: list[tuple[Reference, str]] = []
    seen: set[str] = set()

    def add(ref: Reference | None, source: str) -> None:
        if ref is None or ref.token in seen or len(refs) >= MAX_REFERENCES:
            return
        seen.add(ref.token)
        refs.append((ref, source))

    for source in (text or "").splitlines():
        # Code spans in which a path matched are not dotted modules.
        path_spans: list[tuple[int, int]] = []
        context: Reference | None = None
        symbols_used: set[str] = set()
        for match in _PATH_RE.finditer(source):
            path_spans.append(match.span())
            ref = _resolve_path(repo, match.group(1))
            if match.group(2):
                after = source[match.end():]
                before = source[: match.start()]
                calls = _CALL_RE.findall(before)[-1:] + _CALL_RE.findall(after)[:1]
                symbol = calls[0] if calls else None
                if symbol:
                    symbols_used.add(symbol)
                ref = _resolve_line(repo, ref, int(match.group(2)), symbol)
            add(ref, source)
            context = context or ref
        for match in _MODULE_RE.finditer(source):
            if any(start <= match.start() < end for start, end in path_spans):
                continue
            ref = _resolve_module(repo, match.group(1))
            add(ref, source)
            if ref is not None and context is None:
                context = ref
        if context is not None:
            for symbol in _CALL_RE.findall(source):
                if symbol not in symbols_used and symbol not in _BUILTINS:
                    add(_resolve_symbol(repo, symbol, context), source)
    return refs


# The one bounded disambiguation call


async def _disambiguate(items: list[tuple[Reference, str]], client: Any,
                        seconds_budget: float) -> bool:
    """One llama call choosing a path for each ambiguous reference; ``True`` when it was made."""
    if client is None or not items or not callable(getattr(client, "complete", None)):
        return False
    from modules.llama_cpp_client import json_schema_response_format

    keys = [f"r{i}" for i in range(len(items))]
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "string", "enum": ref.candidates}
                       for key, (ref, _source) in zip(keys, items)},
        "required": keys,
    }
    questions = "\n".join(
        f"{key}: `{ref.token}` in the line \"{source.strip()[:200]}\"; candidates: "
        + ", ".join(ref.candidates)
        for key, (ref, source) in zip(keys, items)
    )
    prompt = ("An issue names files that are not where it says. For each reference pick the "
              f"candidate path it most likely means.\n{questions}\n\n"
              "Return JSON with one chosen path per key.")
    try:
        raw = await asyncio.wait_for(
            client.complete(prompt=prompt, temperature=0.0, max_tokens=MAX_OUTPUT_TOKENS,
                            response_format=json_schema_response_format("repo_fact_choice", schema)),
            timeout=max(0.001, float(seconds_budget)),
        )
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 and end >= start else {}
    except Exception as exc:  # the candidates stay listed
        logger.info("handover fact check disambiguation failed: %s", exc)
        return True
    if not isinstance(data, dict):
        return True
    for key, (ref, _source) in zip(keys, items):
        choice = data.get(key)
        if isinstance(choice, str) and choice in ref.candidates:
            ref.path, ref.chosen_by = choice, "llama"
    return True


def check_references(text: str, repo: str | Path, *, client: Any = None,
                     seconds_budget: float = DISAMBIGUATE_SECONDS,
                     stats: dict[str, Any] | None = None) -> list[Reference]:
    """Every path, module, line ref and ``func()`` in *text*, resolved against *repo*.

    *client* (a llama-server client) is only called, once, when a ``moved``
    reference has more than one candidate; without it, or without a valid
    answer inside *seconds_budget*, all candidates stay listed. *stats*
    receives ``llama_called``.
    """
    index = _Repo(repo)
    found = _extract(index, text)
    ambiguous = [(ref, source) for ref, source in found if ref.ambiguous][:MAX_AMBIGUOUS_IN_CALL]
    called = False
    if ambiguous and client is not None:
        try:
            called = asyncio.run(_disambiguate(ambiguous, client, seconds_budget))
        except RuntimeError:  # already inside an event loop: list the candidates
            called = False
    if stats is not None:
        stats["llama_called"] = called
    return [ref for ref, _source in found]


# Rendering


def _describe(ref: Reference) -> str:
    if ref.status == "line_shifted":
        if ref.new_line is not None:
            what = f"`{ref.symbol}`" if ref.symbol else "the named code"
            return f"- `{ref.token}`: line shifted; {what} is now at {ref.path}:{ref.new_line}"
        return f"- `{ref.token}`: line shifted; {ref.note or 'the line no longer matches'}"
    if ref.status == "missing":
        detail = ref.note or "not found in the repo (create it only if the task asks for it)"
        return f"- `{ref.token}`: missing; {detail}"
    # moved
    where = (f"{ref.path}:{ref.new_line}" if ref.path and ref.new_line else ref.path)
    if where and ref.chosen_by in ("only", "llama"):
        line = f"- `{ref.token}`: moved; it is {where}"
        others = [c for c in ref.candidates if c != ref.path]
        if ref.chosen_by == "llama" and others:
            line += " (other candidates: " + ", ".join(others) + ")"
        return line
    return f"- `{ref.token}`: moved; candidates: " + ", ".join(ref.candidates)


def facts_block(refs: Iterable[Reference]) -> str:
    """The ``Repo facts:`` block; empty when every reference exists."""
    lines = [_describe(ref) for ref in refs if ref.status != "exists"]
    return REPO_FACTS_HEADING + "\n" + "\n".join(lines) if lines else ""


def counts(refs: Iterable[Reference]) -> dict[str, int]:
    result = {status: 0 for status in STATUSES}
    for ref in refs:
        result[ref.status] = result.get(ref.status, 0) + 1
    return result


def with_facts(task: str, block: str) -> str:
    """*task* with *block* appended once; an existing block is never duplicated."""
    if not block or f"\n{REPO_FACTS_HEADING}\n" in f"\n{task}":
        return task
    return task.rstrip() + "\n\n" + block


def has_facts(task: str) -> bool:
    return f"\n{REPO_FACTS_HEADING}\n" in f"\n{task}"


def run_check(text: str, repo: str | Path, *, client: Any = None,
              experiment_logger: Any | None = None, phase: str = "implementation",
              seconds_budget: float = DISAMBIGUATE_SECONDS) -> str | None:
    """Flag-gated check of *text*: the ``Repo facts:`` block, ``None`` when the flag is off.

    Logs exactly one ``handover_fact_check`` event with the counts per status.
    A failing check is logged and returns ``""``; it never stops a run.
    """
    if not enabled():
        return None
    started = time.monotonic()
    stats: dict[str, Any] = {}
    try:
        refs = check_references(text, repo, client=client, seconds_budget=seconds_budget,
                                stats=stats)
    except Exception as exc:  # the check is a hint, never a reason to fail
        logger.info("handover fact check failed: %s", exc)
        refs, stats["error"] = [], str(exc)[:500]
    block = facts_block(refs)
    per_status = counts(refs)
    seconds = round(time.monotonic() - started, 3)
    stale = [ref.token for ref in refs if ref.status != "exists"]
    message = (f"Repo facts: {len(refs)} reference(s), "
               + ", ".join(f"{per_status[s]} {s}" for s in STATUSES))
    if experiment_logger is not None:
        method = getattr(experiment_logger, "info", None)
        if callable(method):
            try:
                method(phase, EVENT, message, references=len(refs), **per_status,
                       stale=stale[:MAX_REFERENCES], llama_called=bool(stats.get("llama_called")),
                       seconds=seconds, error=stats.get("error"))
            except Exception:  # logging must never fail the run
                logger.debug("experiment logger rejected %s", EVENT, exc_info=True)
    logger.info("%s: %s", EVENT, message)
    return block
