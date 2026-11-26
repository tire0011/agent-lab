"""Group and label no-drift verification failures by error signature (#727).

When the timing check (#689) fails closed, the needs_review reason used to be
only a long list of test ids, and the owner or a paid start had to read the
tails by hand to see that one shared cause sat behind them - on 2026-11-14
the 401s from the real AGENT_API_KEY (#713), on 2026-11-16 about 90 tests in
test_task_orchestrator/177/202/218/234/public_logs whose cause was unknown
(#718). This module makes that grouping explicit:

- :func:`signature` - a deterministic error signature: the exception type plus
  the first line of its message, with numbers, paths and hex ids normalised,
  so the same failure with different line numbers, temp directories, ports or
  pids shares one signature (e.g. ``AssertionError: assert N == N``, or just
  ``TimeoutError`` without a message).
- :func:`group_failures` - ``[{signature, test_ids, count}]``, sorted by count.
- :func:`triage` - labels each group ``host_blocker`` / ``experiment_fault`` /
  ``unclear``. A rule fires first and needs no model call: a group of 10 or
  more tests none of whose test files imports or touches a changed module is a
  ``host_blocker``. Otherwise the local 7B (llama-server, JSON-schema response
  format, at most 120 output tokens, time budget from the measured rates in
  :mod:`modules.local_model_rates`) answers ``{"label": ..., "why": ...}`` with
  the signature, two sample tails and the experiment's changed files as input.
  An unavailable model, a timeout or malformed JSON all label ``unclear``.

When the timing check fails closed, verification stores the top groups in the
run_store table ``failure_triage`` (read operation :func:`list_triage`, MCP
tool ``failure_triage_list``) and appends them to the needs_review reason. One
``host_blocker`` group is also fed to #714's gate-self-repair detector as an
early signal: with it the detector needs 2 experiments instead of 3
(:func:`record_early_signal`, :func:`required_streak_length`).

Safety properties of this module:

- Raw test output never leaves the process unredacted: the repository's
  credential redaction (:func:`modules.redaction.redact_credentials`) is
  applied before signature normalisation, before tails are truncated and sent
  to the model, and again before anything is persisted.
- The measured-rate budget is a wall-clock deadline around the whole model
  call (:func:`asyncio.wait_for`), not only the HTTP inactivity timeout.
- The deterministic host-blocker rule never fires on missing evidence: an
  unreadable, unparseable or missing test file, an unresolved relative import
  or an alias spelling counts as "touches", so the group goes to the model.
- The model's reply is validated against the complete JSON schema; anything
  else (invalid JSON, surrounding prose, missing or wrongly typed fields, an
  empty explanation) is ``unclear``.
- :func:`replay` re-runs stored tails of historical failed-closed runs and
  reports groups, labels, the model's agreement with the rule labels and the
  median seconds per model-called group.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from modules.redaction import redact_credentials

logger = logging.getLogger(__name__)

LABEL_HOST_BLOCKER = "host_blocker"
LABEL_EXPERIMENT_FAULT = "experiment_fault"
LABEL_UNCLEAR = "unclear"
LABELS = (LABEL_HOST_BLOCKER, LABEL_EXPERIMENT_FAULT, LABEL_UNCLEAR)

# The gate whose failed-closed runs are triaged here (#689's timing check).
TIMING_CHECK_GATE = "timing_check"

# At most this many groups are triaged (and model-called) per failed-closed
# run, and at most this many output tokens the local model may use per group.
TOP_GROUPS = 3
MAX_OUTPUT_TOKENS = 120
# The rule threshold: a group of at least this many tests, none of whose test
# files imports or touches a changed module, is a host blocker without a
# model call.
HOST_BLOCKER_MIN_TESTS = 10
SAMPLE_TAILS = 2
SAMPLE_TAIL_CHARS = 1200
MAX_CHANGED_FILES_LISTED = 30
MAX_WHY_CHARS = 300

# Rate fallbacks (tokens/s) for a rate that was never measured; the measured
# medians of LocalModelRates are always preferred.
DEFAULT_PROMPT_RATE = 7.0
DEFAULT_GENERATION_RATE = 2.6
TIME_BUDGET_FRACTION = 0.8
MIN_CALL_TIMEOUT_SECONDS = 15.0
MAX_CALL_TIMEOUT_SECONDS = 240.0

# #714: with one host_blocker group recorded as an early signal, the detector
# needs this many experiments instead of STREAK_LENGTH_DEFAULT.
STREAK_LENGTH_DEFAULT = 3
STREAK_LENGTH_WITH_EARLY_SIGNAL = 2
_EARLY_SIGNAL_KEY = "gate_self_repair:early_signal:{gate}"

_EXCEPTION_SUFFIXES = (
    "Error", "Exception", "Exit", "Interrupt", "Iteration", "Timeout", "Warning",
)
_EXCEPTION_LINE = re.compile(
    r"^(?:[E>]\s+)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?::\s?(.*))?$"
)
_HEX_ID = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "why": {"type": "string", "maxLength": MAX_WHY_CHARS},
    },
    "required": ["label", "why"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You label one group of test failures from a Nexus verification run that failed "
    "closed. Answer with JSON only, no prose: "
    '{"label": "host_blocker"|"experiment_fault"|"unclear", "why": "one short sentence"}. '
    "host_blocker means the host or environment is at fault (missing credential, "
    "service down, host state), not the experiment's change; experiment_fault means "
    "the experiment's changed files plausibly caused the failures; unclear means the "
    "evidence is not enough to decide."
)


# -- signature ----------------------------------------------------------------


def _normalise(text: str) -> str:
    """Collapse whitespace and replace hex ids, path-like tokens and numbers."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    # Hex ids first (their digits would otherwise become N), then any token
    # containing a path separator, then plain numbers. The placeholders must
    # not contain digits themselves.
    text = _HEX_ID.sub("HEX", text)
    text = " ".join(
        "PATH" if ("/" in token or "\\" in token) else token for token in text.split(" ")
    )
    text = _NUMBER.sub("N", text)
    return " ".join(text.split())


def _exception_of(tail: str) -> tuple[str, str]:
    """``(exception type, message)`` of *tail*, from the last exception line up."""
    lines = [line for line in str(tail or "").splitlines() if line.strip()]
    for line in reversed(lines):
        match = _EXCEPTION_LINE.match(line.strip())
        if not match:
            continue
        name, message = match.group(1), match.group(2) or ""
        last_segment = name.rsplit(".", 1)[-1]
        if last_segment.endswith(_EXCEPTION_SUFFIXES) or "." in name:
            return name, message
    if lines:
        return "", lines[-1].strip()
    return "", ""


def signature(tail: str) -> str:
    """The deterministic error signature of one failure *tail*.

    The exception type plus the first (normalised) line of its message; the
    type alone when the message is empty (``TimeoutError``), and the last
    line alone when no exception line is found. Numbers, paths and hex ids
    are normalised, so ``AssertionError: assert 401 == 200`` in two different
    temp directories with different line numbers gives one signature.
    Credentials are redacted before the normalisation, so a signature never
    contains a secret value.
    """
    name, message = _exception_of(redact_credentials(str(tail or "")))
    normalised = _normalise(message)
    if name and normalised:
        return f"{name}: {normalised}"
    if name:
        return name
    return normalised or "unknown"


def group_failures(tails: dict[str, str]) -> list[dict[str, Any]]:
    """``[{signature, test_ids, count}]`` for *tails*, sorted by count (ties by signature)."""
    groups: dict[str, dict[str, Any]] = {}
    for test_id, tail in (tails or {}).items():
        sig = signature(tail)
        group = groups.setdefault(sig, {"signature": sig, "test_ids": [], "count": 0})
        group["test_ids"].append(str(test_id))
        group["count"] += 1
    for group in groups.values():
        group["test_ids"] = sorted(group["test_ids"])
    return sorted(groups.values(), key=lambda g: (-int(g["count"]), str(g["signature"])))


# -- which test files touch a changed module ----------------------------------


def _module_names(path: str) -> set[str]:
    """The dotted module name(s) of *path*, exactly as timing_check.py sees it.

    Reusing :func:`modules.timing_check._module_names` keeps the src/lib
    alias spellings in one place: a changed ``src/foo/bar.py`` is the module
    ``src.foo.bar`` and ``foo.bar`` alike, and both spellings count here.
    """
    from modules import timing_check

    return timing_check._module_names(str(path).replace("\\", "/"))


def changed_modules_of(changed_files: list[str] | set[str] | tuple[str, ...]) -> set[str]:
    """All dotted spellings of every changed Python module."""
    modules: set[str] = set()
    for path in changed_files or []:
        modules |= _module_names(str(path).replace("\\", "/"))
    return modules


def _imports(path: Path, package: str = "") -> set[str] | None:
    """The absolute imported module names of *path*, or None when unknown.

    None means the file cannot be read or parsed: the caller must treat the
    dependency as unknown (never as proof that nothing is imported). Relative
    imports (``from . import x``, ``from ..pkg import y``) are resolved
    against the file's own *package* (its dotted path relative to the repo
    root, so an absolute filesystem path never leaks into a module name).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    package_parts = package.split(".") if package else []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Resolve the relative import against this file's package:
                # level 1 is the own package, each further level goes one up.
                keep = len(package_parts) - (node.level - 1)
                if keep < 0:
                    continue  # beyond the repo root: unknowable, ignore the node
                base = package_parts[:keep]
                module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                module = node.module or ""
            if module:
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_touches_changed(
    test_file: str, changed_files: set[str] | list[str], changed_modules: set[str],
    repo: str | Path | None = None,
) -> bool:
    """Whether *test_file* is itself changed or imports a changed module.

    Conservative: anything unknown counts as touching, so the deterministic
    host-blocker rule never fires on missing evidence. With *repo*, the test
    file's real imports are read - an unreadable, unparseable or missing test
    file, an unresolved relative import or a changed module spelled through
    an src/lib alias all count as touched. Without *repo*, only the path
    itself and the package-tree relation can be judged.
    """
    test_file = str(test_file).replace("\\", "/")
    if not test_file:
        return False
    changed = {str(f).replace("\\", "/") for f in changed_files}
    if test_file in changed:
        return True
    test_modules = _module_names(test_file)
    test_dir = PurePosixPath(test_file).parent
    for path in changed:
        # A changed non-Python file (data, config) in the test's own directory
        # may well be read by the test: count it as touched.
        if PurePosixPath(path).suffix != ".py" and PurePosixPath(path).parent == test_dir:
            return True
    if repo is not None:
        test_package = ".".join(PurePosixPath(test_file).parent.parts)
        imports = _imports(Path(repo) / test_file, test_package)
        if imports is None:
            return True  # unknown dependency: never the rule's proof
        for module in changed_modules:
            if any(name == module or name.startswith(module + ".") for name in imports):
                return True
    for module in changed_modules:
        for test_module in test_modules:
            if (module == test_module or module.startswith(test_module + ".")
                    or test_module.startswith(module + ".")):
                return True
    return False


# -- the local model call ------------------------------------------------------


def _usable_rate(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value) if math.isfinite(value) and value > 0 else fallback


def measured_rates(rates: Any = None) -> tuple[float, float]:
    """``(prompt, generation)`` tokens/s from *rates* (medians object or tuple)."""
    if rates is not None and callable(getattr(rates, "medians", None)):
        try:
            rates = rates.medians()
        except Exception:  # noqa: BLE001 - a broken store falls back per rate
            rates = None
    try:
        prompt_rate, generation_rate = rates if rates is not None else (None, None)
    except (TypeError, ValueError):
        prompt_rate = generation_rate = None
    return (_usable_rate(prompt_rate, DEFAULT_PROMPT_RATE),
            _usable_rate(generation_rate, DEFAULT_GENERATION_RATE))


def _call_timeout_seconds(prompt_tokens: int, prompt_rate: float, generation_rate: float) -> float:
    """The total call timeout from the measured rates and the output cap."""
    work = prompt_tokens / prompt_rate + MAX_OUTPUT_TOKENS / generation_rate
    return min(MAX_CALL_TIMEOUT_SECONDS,
               max(MIN_CALL_TIMEOUT_SECONDS, work / TIME_BUDGET_FRACTION))


def _default_client() -> Any:
    """The production local-model client (patchable in tests)."""
    from modules.llama_cpp_client import LlamaCppClient

    return LlamaCppClient()


def _default_rates() -> Any:
    """The production measured-rates store (patchable in tests)."""
    from modules.local_model_rates import LocalModelRates

    return LocalModelRates()


def _run_async(coro: Any) -> Any:
    """Run *coro* to completion; ``None`` when a loop already runs in this thread."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        return None


async def _safe_available(client: Any) -> bool:
    try:
        return bool(await client.is_available())
    except Exception:  # noqa: BLE001 - an unreachable model is simply unavailable
        return False


def _resolve_client(client: Any) -> Any:
    """An available client, or ``None`` when the model is unavailable."""
    if client is not None:
        return client
    try:
        candidate = _default_client()
    except Exception:  # noqa: BLE001 - no client, no model call
        logger.info("failure triage: no local model client", exc_info=True)
        return None
    if candidate is None:
        return None
    return candidate if _run_async(_safe_available(candidate)) else None


def _sample_tails(group: dict[str, Any], tails: dict[str, str] | None) -> list[str]:
    samples = [str(s) for s in (group.get("samples") or [])][:SAMPLE_TAILS]
    if not samples and tails:
        samples = [
            str(tails[test_id]) for test_id in group.get("test_ids", [])
            if test_id in tails
        ][:SAMPLE_TAILS]
    # Redact before truncating: a credential must not survive into the prompt
    # even in the part that fits the character budget.
    return [redact_credentials(sample)[:SAMPLE_TAIL_CHARS] for sample in samples]


def _build_prompt(group: dict[str, Any], changed_files: list[str],
                  samples: list[str]) -> str:
    changed = changed_files[:MAX_CHANGED_FILES_LISTED]
    lines = [
        "One group of test failures from a verification run that failed closed.",
        f"Signature: {group.get('signature', '')}",
        f"Tests in the group: {int(group.get('count') or 0)}",
        "Sample failure tails:",
    ]
    for sample in samples or ["(none)"]:
        lines.extend(["````text", sample, "````"])
    lines.extend(["", "Changed files in the experiment:"])
    if changed:
        lines.extend(f"- {path}" for path in changed)
    else:
        lines.append("- (none)")
    lines.extend([
        "",
        'Answer with JSON only: {"label": "host_blocker"|"experiment_fault"|"unclear", '
        '"why": "one short sentence"}',
    ])
    return "\n".join(lines)


def _parse_answer(raw: Any) -> tuple[str, str] | None:
    """``(label, why)`` of a model answer, or ``None`` when it is malformed.

    The complete reply is validated against :data:`_ANSWER_SCHEMA`: it must be
    exactly one JSON object with a ``label`` in :data:`LABELS` and a non-empty
    string ``why`` of at most :data:`MAX_WHY_CHARS` characters - no surrounding
    prose, no embedded-object recovery, no extra or missing fields, no wrong
    types. Anything else is malformed.
    """
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if set(data.keys()) != {"label", "why"}:
        return None
    label, why = data["label"], data["why"]
    if not isinstance(label, str) or label not in LABELS:
        return None
    if not isinstance(why, str):
        return None
    why = redact_credentials(why)
    if not why.strip() or len(why) > MAX_WHY_CHARS:
        return None
    return label, why


async def _ask(client: Any, prompt: str, timeout: float) -> str:
    from modules.llama_cpp_client import json_schema_response_format

    try:
        client.timeout = timeout
    except Exception:  # noqa: BLE001 - a fake client may not accept it
        pass
    # The wall-clock deadline wraps the whole completion: client.timeout is
    # only the HTTP inactivity timeout on the non-streaming path, so a slow
    # response would otherwise keep verification waiting beyond the budget.
    return str(await asyncio.wait_for(
        client.complete(
            prompt=prompt, system=_SYSTEM_PROMPT, temperature=0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
            response_format=json_schema_response_format("failure_triage_label", _ANSWER_SCHEMA),
        ),
        timeout=timeout,
    ))


def _model_label(group: dict[str, Any], changed_files: list[str], client: Any,
                 tails: dict[str, str] | None, rates: Any) -> tuple[str, str, str, float | None]:
    """Ask the local model for one group's label; ``unclear`` on any failure.

    Returns ``(label, why, source, seconds)``; *seconds* is the wall-clock
    duration of the model call (``None`` when no call completed).
    """
    if client is None:
        return LABEL_UNCLEAR, "local model unavailable", "unavailable", None
    samples = _sample_tails(group, tails)
    prompt = _build_prompt(group, changed_files, samples)
    prompt_tokens = max(1, len(prompt.encode("utf-8")) // 3)
    prompt_rate, generation_rate = measured_rates(rates)
    timeout = _call_timeout_seconds(prompt_tokens, prompt_rate, generation_rate)
    started = time.monotonic()
    try:
        raw = _run_async(_ask(client, prompt, timeout))
    except (asyncio.TimeoutError, TimeoutError):
        return (LABEL_UNCLEAR,
                redact_credentials(f"local model call exceeded the {timeout:.0f}s budget"),
                "model_error", time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - a crash is no verdict
        detail = redact_credentials(str(exc).splitlines()[0][:120] if str(exc) else "")
        return (LABEL_UNCLEAR,
                f"local model call failed: {type(exc).__name__}"
                + (f": {detail}" if detail else ""),
                "model_error", time.monotonic() - started)
    elapsed = time.monotonic() - started
    if raw is None:
        return LABEL_UNCLEAR, "local model call could not run here", "model_error", None
    timings = getattr(client, "last_timings", None)
    if rates is not None and callable(getattr(rates, "record", None)):
        try:
            rates.record(timings)
        except Exception:  # noqa: BLE001 - measuring must never fail the label
            pass
    parsed = _parse_answer(raw)
    if parsed is None:
        return LABEL_UNCLEAR, "malformed model answer", "model_error", elapsed
    return parsed[0], parsed[1], "model", elapsed


def triage(
    groups: list[dict[str, Any]], changed_files: list[str] | set[str] | tuple[str, ...],
    client: Any = None, *, tails: dict[str, str] | None = None,
    repo: str | Path | None = None, rates: Any = None,
) -> list[dict[str, Any]]:
    """Label each *group* ``host_blocker`` / ``experiment_fault`` / ``unclear``.

    Rule first: a group of at least :data:`HOST_BLOCKER_MIN_TESTS` tests none
    of whose test files imports or touches a changed module is a
    ``host_blocker`` without a model call. Every other group goes to the
    local model (*client*, or the default llama-server client when ``None``;
    ``unclear`` when it is unavailable) with the signature, two sample tails
    and the changed files as input. The result carries the group's keys plus
    ``label``, ``why``, ``source`` (``rule`` | ``model`` | ``unavailable`` |
    ``model_error``) and ``seconds`` (the model call's wall-clock duration,
    ``None`` for the rule and when no call completed).
    """
    changed = sorted({str(f).replace("\\", "/") for f in (changed_files or [])})
    changed_modules = changed_modules_of(changed)
    if rates is None:
        rates = _default_rates()
    active_client: Any = client
    client_resolved = client is not None
    results: list[dict[str, Any]] = []
    for group in groups or []:
        test_ids = [str(t) for t in (group.get("test_ids") or [])]
        test_files = sorted({t.split("::", 1)[0] for t in test_ids if t})
        count = int(group.get("count") or len(test_ids))
        touches = any(
            test_touches_changed(test_file, changed, changed_modules, repo)
            for test_file in test_files
        )
        if not touches and count >= HOST_BLOCKER_MIN_TESTS:
            results.append({
                **group, "count": count, "label": LABEL_HOST_BLOCKER,
                "why": (f"rule: {count} tests, none of their files imports or touches "
                        f"a changed module ({len(changed)} changed files)"),
                "source": "rule", "seconds": None,
            })
            continue
        if not client_resolved:
            active_client = _resolve_client(client)
            client_resolved = True
        label, why, source, seconds = _model_label(group, changed, active_client, tails, rates)
        results.append({**group, "count": count, "label": label, "why": why,
                        "source": source, "seconds": seconds})
    return results


# -- run_store table -----------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS failure_triage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            gate TEXT NOT NULL DEFAULT 'timing_check',
            attempt TEXT NULL,
            signature TEXT NOT NULL,
            label TEXT NOT NULL,
            why TEXT NULL,
            source TEXT NULL,
            test_ids_json TEXT NOT NULL DEFAULT '[]',
            count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_failure_triage_experiment
            ON failure_triage(experiment_id, id);
        CREATE INDEX IF NOT EXISTS idx_failure_triage_label
            ON failure_triage(label, id);
        """
    )
    # Databases created before the attempt column existed (#727 retries are
    # distinguished by verification attempt) get it added here.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(failure_triage)")}
    if columns and "attempt" not in columns:
        conn.execute("ALTER TABLE failure_triage ADD COLUMN attempt TEXT NULL")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _triage_row_values(experiment_id: str, gate: str, group: dict[str, Any],
                       stamp: str, attempt: str | None) -> tuple:
    """One INSERT's parameters; signature and why are redacted at the boundary."""
    return (
        str(experiment_id), gate, attempt,
        redact_credentials(str(group.get("signature", ""))),
        str(group.get("label", LABEL_UNCLEAR)),
        redact_credentials(str(group.get("why") or "")) or None,
        group.get("source"), json.dumps(list(group.get("test_ids") or [])),
        int(group.get("count") or 0), stamp,
    )


def record_triage(
    conn: sqlite3.Connection, experiment_id: str, groups: list[dict[str, Any]],
    *, gate: str = TIMING_CHECK_GATE, attempt: str | None = None, now: str | None = None,
) -> int:
    """Store one row per labelled *group*; returns the number of rows written.

    Re-running the same *attempt* of the same experiment is idempotent: the
    rows of that (experiment, gate, attempt) are replaced, not duplicated.
    """
    stamp = now or _utcnow()
    rows = 0
    with conn:
        # Idempotent per verification attempt: a retried attempt replaces its
        # own rows instead of appending duplicates.
        if attempt is None:
            conn.execute(
                "DELETE FROM failure_triage WHERE experiment_id = ? AND gate = ? AND attempt IS NULL",
                (str(experiment_id), gate),
            )
        else:
            conn.execute(
                "DELETE FROM failure_triage WHERE experiment_id = ? AND gate = ? AND attempt = ?",
                (str(experiment_id), gate, str(attempt)),
            )
        for group in groups or []:
            conn.execute(
                """
                INSERT INTO failure_triage
                    (experiment_id, gate, attempt, signature, label, why, source,
                     test_ids_json, count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _triage_row_values(experiment_id, gate, group, stamp, attempt),
            )
            rows += 1
    return rows


def list_triage(
    conn: sqlite3.Connection, experiment_id: str | None = None, *,
    label: str | None = None, gate: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Stored triage rows, newest first; ``test_ids`` is decoded from JSON."""
    if limit <= 0:
        return []
    query = (
        "SELECT id, experiment_id, gate, attempt, signature, label, why, source, "
        "test_ids_json, count, created_at FROM failure_triage WHERE 1=1"
    )
    params: list[Any] = []
    if experiment_id:
        query += " AND experiment_id = ?"
        params.append(str(experiment_id))
    if label:
        query += " AND label = ?"
        params.append(str(label))
    if gate:
        query += " AND gate = ?"
        params.append(str(gate))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(min(int(limit), 500))
    cursor = conn.execute(query, params)
    columns = [description[0] for description in cursor.description]
    rows: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = dict(zip(columns, row))
        raw_ids = item.pop("test_ids_json", "[]")
        try:
            item["test_ids"] = json.loads(raw_ids) if raw_ids else []
        except ValueError:
            item["test_ids"] = []
        rows.append(item)
    return rows


# -- the early signal for #714's detector --------------------------------------


def record_early_signal(
    conn: sqlite3.Connection, experiment_id: str, group: dict[str, Any], *,
    gate: str = TIMING_CHECK_GATE, reason: str | None = None, now: str | None = None,
) -> None:
    """Feed one ``host_blocker`` group to #714's detector as an early signal.

    Stored under the detector's settings namespace with the gate failure
    *reason* it belongs to; with a matching signal the detector needs
    :data:`STREAK_LENGTH_WITH_EARLY_SIGNAL` experiments instead of
    :data:`STREAK_LENGTH_DEFAULT` (:func:`required_streak_length`). The signal
    is scoped: it only ever applies to the same gate and the same reason, and
    the detector clears it when it is consumed or when it no longer matches
    (:func:`clear_early_signal`).
    """
    from modules.run_store import set_setting

    stamp = now or _utcnow()
    set_setting(
        conn, _EARLY_SIGNAL_KEY.format(gate=gate),
        json.dumps({
            "experiment_id": str(experiment_id), "gate": gate,
            "signature": redact_credentials(str(group.get("signature", ""))),
            "reason": reason,
            "count": int(group.get("count") or 0),
            "source": "failure_triage", "at": stamp,
        }, sort_keys=True),
        updated_at=stamp,
    )


def early_signal(conn: sqlite3.Connection, gate: str = TIMING_CHECK_GATE) -> dict[str, Any] | None:
    """The recorded host_blocker early signal for *gate*, or ``None``."""
    from modules.run_store import get_setting

    raw = get_setting(conn, _EARLY_SIGNAL_KEY.format(gate=gate))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clear_early_signal(conn: sqlite3.Connection, gate: str = TIMING_CHECK_GATE) -> None:
    """Drop the early signal for *gate* (consumed or stale)."""
    with conn:
        conn.execute(
            "DELETE FROM service_settings WHERE key = ?",
            (_EARLY_SIGNAL_KEY.format(gate=gate),),
        )


def required_streak_length(
    conn: sqlite3.Connection, *, gate: str = TIMING_CHECK_GATE,
    reason: str | None = None, default: int = STREAK_LENGTH_DEFAULT,
) -> int:
    """Experiments #714's detector needs: 2 with a matching early signal, else *default* (3).

    The signal only lowers the threshold when it belongs to *gate* and its
    recorded reason is *reason* (or *reason* is None, i.e. the caller only
    asks whether any signal exists for the gate).
    """
    signal = early_signal(conn, gate)
    if signal is not None and (reason is None or signal.get("reason") == reason):
        return STREAK_LENGTH_WITH_EARLY_SIGNAL
    return int(default)


def persist_triage(
    conn: sqlite3.Connection, experiment_id: str, triaged: list[dict[str, Any]], *,
    gate: str = TIMING_CHECK_GATE, reason: str | None = None,
    attempt: str | None = None, now: str | None = None,
) -> int:
    """Store the triage rows and the early signal in one transaction.

    The rows and the #714 signal are written together, so a failure cannot
    leave rows visible while the signal is missing (or the other way round).
    Returns the number of rows written.
    """
    stamp = now or _utcnow()
    host = next(
        (group for group in triaged or []
         if group.get("label") == LABEL_HOST_BLOCKER),
        None,
    )
    rows = 0
    with conn:
        if attempt is None:
            conn.execute(
                "DELETE FROM failure_triage WHERE experiment_id = ? AND gate = ? AND attempt IS NULL",
                (str(experiment_id), gate),
            )
        else:
            conn.execute(
                "DELETE FROM failure_triage WHERE experiment_id = ? AND gate = ? AND attempt = ?",
                (str(experiment_id), gate, str(attempt)),
            )
        for group in triaged or []:
            conn.execute(
                """
                INSERT INTO failure_triage
                    (experiment_id, gate, attempt, signature, label, why, source,
                     test_ids_json, count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _triage_row_values(experiment_id, gate, group, stamp, attempt),
            )
            rows += 1
        if host is not None and reason is not None:
            conn.execute(
                """
                INSERT INTO service_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (
                    _EARLY_SIGNAL_KEY.format(gate=gate),
                    json.dumps({
                        "experiment_id": str(experiment_id), "gate": gate,
                        "signature": redact_credentials(str(host.get("signature", ""))),
                        "reason": reason,
                        "count": int(host.get("count") or 0),
                        "source": "failure_triage", "at": stamp,
                    }, sort_keys=True),
                    stamp,
                ),
            )
    return rows


# -- replay of stored tails (#727 measure) -------------------------------------


def replay(
    runs: list[dict[str, Any]], *, client: Any = None, repo: str | Path | None = None,
    rates: Any = None, check_rule_against_model: bool = True,
) -> dict[str, Any]:
    """Re-run the failure triage over stored tails of historical failed-closed runs.

    *runs* is a list of ``{"experiment_id": ..., "tails": {test_id: tail},
    "changed_files": [...]}`` records (the tails stored by #737's timing-check
    events). For each run the groups are built and labelled as in production;
    when *check_rule_against_model* is set, rule-labelled groups are
    additionally sent to the model so the report can state the model's
    agreement with the rule. The report carries per-run groups with labels
    and sources, the rule agreement counts, and the median wall-clock seconds
    per model-called group.
    """
    report_runs: list[dict[str, Any]] = []
    seconds: list[float] = []
    agree = disagree = unavailable = 0
    for run in runs or []:
        experiment_id = str(run.get("experiment_id") or "")
        tails = {str(k): str(v) for k, v in (run.get("tails") or {}).items()}
        changed_files = [str(f) for f in (run.get("changed_files") or [])]
        groups = group_failures(tails)
        triaged = triage(groups[:TOP_GROUPS], changed_files, client=client,
                         tails=tails, repo=repo, rates=rates)
        entries: list[dict[str, Any]] = []
        for group in triaged:
            entry = {
                "signature": group.get("signature"), "count": group.get("count"),
                "label": group.get("label"), "source": group.get("source"),
                "why": group.get("why"), "test_ids": group.get("test_ids"),
            }
            if group.get("seconds") is not None:
                seconds.append(float(group["seconds"]))
            if (check_rule_against_model and group.get("source") == "rule"
                    and client is not None):
                model_label, _, model_source, model_seconds = _model_label(
                    group, changed_files, client, tails, rates,
                )
                if model_seconds is not None:
                    seconds.append(float(model_seconds))
                entry["model_label"] = model_label
                entry["model_source"] = model_source
                if model_source in ("unavailable", "model_error"):
                    unavailable += 1
                elif model_label == group.get("label"):
                    agree += 1
                else:
                    disagree += 1
            entries.append(entry)
        report_runs.append({"experiment_id": experiment_id, "groups": entries})
    seconds.sort()
    median = seconds[len(seconds) // 2] if seconds else None
    return {
        "runs": report_runs,
        "rule_agreement": {
            "agree": agree, "disagree": disagree, "model_unavailable_or_unclear": unavailable,
        },
        "model_calls": len(seconds),
        "median_seconds_per_group": median,
    }
