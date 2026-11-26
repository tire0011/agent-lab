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
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

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
    """
    name, message = _exception_of(tail)
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
    parsed = PurePosixPath(str(path).replace("\\", "/"))
    if parsed.suffix != ".py":
        return set()
    module_path = parsed.parent if parsed.name == "__init__.py" else parsed.with_suffix("")
    return {".".join(module_path.parts)} if module_path.parts else set()


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_touches_changed(
    test_file: str, changed_files: set[str] | list[str], changed_modules: set[str],
    repo: str | Path | None = None,
) -> bool:
    """Whether *test_file* is itself changed or imports a changed module.

    With *repo*, the test file's real imports are read; without it, a changed
    module in the same package tree counts as touched.
    """
    test_file = str(test_file).replace("\\", "/")
    if not test_file:
        return False
    changed = {str(f).replace("\\", "/") for f in changed_files}
    if test_file in changed:
        return True
    test_modules = _module_names(test_file)
    if repo is not None:
        imports = _imports(Path(repo) / test_file)
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
    return [sample[:SAMPLE_TAIL_CHARS] for sample in samples]


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
    """``(label, why)`` of a model answer, or ``None`` when it is malformed."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                data = json.loads(raw[start:end + 1])
            except ValueError:
                return None
    if not isinstance(data, dict):
        return None
    label = data.get("label")
    if label not in LABELS:
        return None
    why = str(data.get("why") or "")[:MAX_WHY_CHARS]
    return str(label), why


async def _ask(client: Any, prompt: str, timeout: float) -> str:
    from modules.llama_cpp_client import json_schema_response_format

    try:
        client.timeout = timeout
    except Exception:  # noqa: BLE001 - a fake client may not accept it
        pass
    return str(await client.complete(
        prompt=prompt, system=_SYSTEM_PROMPT, temperature=0.0,
        max_tokens=MAX_OUTPUT_TOKENS,
        response_format=json_schema_response_format("failure_triage_label", _ANSWER_SCHEMA),
    ))


def _model_label(group: dict[str, Any], changed_files: list[str], client: Any,
                 tails: dict[str, str] | None, rates: Any) -> tuple[str, str, str]:
    """Ask the local model for one group's label; ``unclear`` on any failure."""
    if client is None:
        return LABEL_UNCLEAR, "local model unavailable", "unavailable"
    samples = _sample_tails(group, tails)
    prompt = _build_prompt(group, changed_files, samples)
    prompt_tokens = max(1, len(prompt.encode("utf-8")) // 3)
    prompt_rate, generation_rate = measured_rates(rates)
    timeout = _call_timeout_seconds(prompt_tokens, prompt_rate, generation_rate)
    try:
        raw = _run_async(_ask(client, prompt, timeout))
    except Exception as exc:  # noqa: BLE001 - a timeout or crash is no verdict
        detail = str(exc).splitlines()[0][:120] if str(exc) else ""
        return (LABEL_UNCLEAR,
                f"local model call failed: {type(exc).__name__}"
                + (f": {detail}" if detail else ""),
                "model_error")
    if raw is None:
        return LABEL_UNCLEAR, "local model call could not run here", "model_error"
    timings = getattr(client, "last_timings", None)
    if rates is not None and callable(getattr(rates, "record", None)):
        try:
            rates.record(timings)
        except Exception:  # noqa: BLE001 - measuring must never fail the label
            pass
    parsed = _parse_answer(raw)
    if parsed is None:
        return LABEL_UNCLEAR, "malformed model answer", "model_error"
    return parsed[0], parsed[1], "model"


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
    ``label``, ``why`` and ``source`` (``rule`` | ``model`` | ``unavailable`` |
    ``model_error``).
    """
    changed = sorted({str(f).replace("\\", "/") for f in (changed_files or [])})
    changed_modules: set[str] = set()
    for path in changed:
        changed_modules |= _module_names(path)
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
                "source": "rule",
            })
            continue
        if not client_resolved:
            active_client = _resolve_client(client)
            client_resolved = True
        label, why, source = _model_label(group, changed, active_client, tails, rates)
        results.append({**group, "count": count, "label": label, "why": why, "source": source})
    return results


# -- run_store table -----------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS failure_triage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            gate TEXT NOT NULL DEFAULT 'timing_check',
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_triage(
    conn: sqlite3.Connection, experiment_id: str, groups: list[dict[str, Any]],
    *, gate: str = TIMING_CHECK_GATE, now: str | None = None,
) -> int:
    """Store one row per labelled *group*; returns the number of rows written."""
    stamp = now or _utcnow()
    rows = 0
    with conn:
        for group in groups or []:
            conn.execute(
                """
                INSERT INTO failure_triage
                    (experiment_id, gate, signature, label, why, source,
                     test_ids_json, count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(experiment_id), gate, str(group.get("signature", "")),
                    str(group.get("label", LABEL_UNCLEAR)), group.get("why"),
                    group.get("source"), json.dumps(list(group.get("test_ids") or [])),
                    int(group.get("count") or 0), stamp,
                ),
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
        "SELECT id, experiment_id, gate, signature, label, why, source, "
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
    gate: str = TIMING_CHECK_GATE, now: str | None = None,
) -> None:
    """Feed one ``host_blocker`` group to #714's detector as an early signal.

    Stored under the detector's settings namespace; with it the detector needs
    :data:`STREAK_LENGTH_WITH_EARLY_SIGNAL` experiments instead of
    :data:`STREAK_LENGTH_DEFAULT` (:func:`required_streak_length`).
    """
    from modules.run_store import set_setting

    stamp = now or _utcnow()
    set_setting(
        conn, _EARLY_SIGNAL_KEY.format(gate=gate),
        json.dumps({
            "experiment_id": str(experiment_id), "gate": gate,
            "signature": str(group.get("signature", "")),
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


def required_streak_length(
    conn: sqlite3.Connection, *, gate: str = TIMING_CHECK_GATE,
    default: int = STREAK_LENGTH_DEFAULT,
) -> int:
    """Experiments #714's detector needs: 2 with an early signal, else *default* (3)."""
    if early_signal(conn, gate) is not None:
        return STREAK_LENGTH_WITH_EARLY_SIGNAL
    return int(default)
