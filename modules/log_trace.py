"""Reconstruct what Nexus did with one order, from its logs alone. The unified feed
(GET /logs) answers "what happened recently"; this answers "Nexus was given this -
what did it actually do?". One order changes identity as it moves through the
system (inbox filename -> mailbox task id -> experiment id -> session/job ids), so
resolution is forgiving and iterative: find entries the reference plausibly
identifies, read their correlation ids, pull in everything sharing them, repeat
until no new ids appear - the whole causal chain from any one point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules import central_logging

# Correlation fields an entry can carry. Order matters only for stable,
# reproducible reporting of what a reference resolved to.
_CORRELATION_FIELDS = ("experiment_id", "mailbox_task_id", "session_id", "job_id")

# Discover-and-expand round bound: two suffice for real chains (filename -> task ->
# experiment -> session/job); the bound stops a pathological log from turning one
# request into an unbounded scan.
DEFAULT_MAX_EXPAND_ROUNDS = 3

_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")


@dataclass
class TraceResult:
    """One order's reconstructed timeline plus how it was identified."""

    reference: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    correlation_ids: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False

    @property
    def found(self) -> bool:
        return bool(self.entries)


def mailbox_task_ids_for_reference(reference: str) -> list[str]:
    """Candidate transient mailbox task ids for an inbox filename: the id digests
    the path relative to the mailbox repo, but an operator usually has only the
    bare filename, so both spellings are offered."""
    from modules.mailbox.processing import transient_mailbox_task_id

    reference = reference.strip().strip("/")
    if not reference:
        return []
    candidates = [reference]
    name = reference.rsplit("/", 1)[-1]
    if name != reference:
        candidates.append(name)
    else:
        candidates.append(f"inbox/{reference}")
    return [transient_mailbox_task_id(candidate) for candidate in candidates]


def _collect_ids(entries: list[dict[str, Any]]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {field_name: set() for field_name in _CORRELATION_FIELDS}
    for entry in entries:
        for field_name in _CORRELATION_FIELDS:
            value = entry.get(field_name)
            if isinstance(value, str) and value:
                found[field_name].add(value)
    return found


def _entry_key(entry: dict[str, Any]) -> tuple:
    return central_logging.entry_sort_key(entry)


def resolve_trace(
    log_dir: str | Path,
    reference: str,
    *,
    limit: int | None = None,
    max_expand_rounds: int = DEFAULT_MAX_EXPAND_ROUNDS,
) -> TraceResult:
    """Build the complete timeline for the order named by *reference* (an experiment,
    mailbox task, session or chat job id, an inbox filename, or any distinctive
    fragment); anything unrecognized falls back to a full-history search rather
    than a 404 - a half-remembered id is still a useful way in."""
    result = TraceResult(reference=reference)
    reference = (reference or "").strip()
    if not reference:
        return result

    seeds: list[dict[str, Any]] = []
    truncated = False

    # 1) Reference as a correlation id; if it looks like an inbox filename, also as
    #    the message whose transient task id we can derive.
    known: dict[str, set[str]] = {name: set() for name in _CORRELATION_FIELDS}
    if _SAFE_REFERENCE_RE.fullmatch(reference):
        known["experiment_id"].add(reference)
        known["mailbox_task_id"].add(reference)
        known["session_id"].add(reference)
        known["job_id"].add(reference)
        for candidate in mailbox_task_ids_for_reference(reference):
            known["mailbox_task_id"].add(candidate)

    seed_scan = _scan_for_ids(log_dir, known)
    seeds.extend(seed_scan.entries)
    truncated = truncated or seed_scan.truncated

    # 2) Nothing matched an id: search the whole retained history's text.
    if not seeds:
        search_scan = central_logging.scan_log_entries(log_dir, search=reference)
        seeds.extend(search_scan.entries)
        truncated = truncated or search_scan.truncated

    if not seeds:
        result.truncated = truncated
        return result

    # 3) Expand: pull in every entry sharing a discovered correlation id, until no
    #    new id appears.
    collected: dict[tuple, dict[str, Any]] = {_entry_key(e): e for e in seeds}
    discovered = _collect_ids(seeds)
    for _ in range(max_expand_rounds):
        scan = _scan_for_ids(log_dir, discovered)
        truncated = truncated or scan.truncated
        new_entries = [e for e in scan.entries if _entry_key(e) not in collected]
        for entry in new_entries:
            collected[_entry_key(entry)] = entry
        merged = _collect_ids(list(collected.values()))
        if merged == discovered and not new_entries:
            break
        discovered = merged

    entries = sorted(collected.values(), key=_entry_key)
    if limit is not None and limit >= 0 and len(entries) > limit:
        entries = entries[-limit:]

    result.entries = entries
    result.correlation_ids = {
        name: sorted(values) for name, values in discovered.items() if values
    }
    result.truncated = truncated
    return result


def _scan_for_ids(
    log_dir: str | Path, ids: dict[str, set[str]]
) -> central_logging.LogScanResult:
    """Every entry carrying any of *ids*, merged across correlation fields. Each id
    is looked up with a full-history *search* then narrowed to an exact field match,
    not via the dedicated id filters: the experiment_id filter reads only that
    experiment's own file and would miss entries logged under another file that
    carry the id (the link a trace must not lose), and the other filters keep the
    newest-tail shortcut, which would drop older correlated entries."""
    wanted_by_value: dict[str, set[str]] = {}
    for field_name, values in ids.items():
        for value in values:
            if value:
                wanted_by_value.setdefault(value, set()).add(field_name)

    collected: dict[tuple, dict[str, Any]] = {}
    truncated = False
    for value in sorted(wanted_by_value):
        scan = central_logging.scan_log_entries(log_dir, search=value)
        truncated = truncated or scan.truncated
        fields = wanted_by_value[value]
        for entry in scan.entries:
            if any(entry.get(field_name) == value for field_name in fields):
                collected[_entry_key(entry)] = entry
    return central_logging.LogScanResult(
        entries=sorted(collected.values(), key=_entry_key), truncated=truncated
    )


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact "what happened" header for a trace, derived from the entries themselves
    (not any status table), so it can never disagree with the timeline below it."""
    if not entries:
        return {
            "entry_count": 0,
            "started_at": None,
            "ended_at": None,
            "levels": {},
            "phases": [],
            "error_count": 0,
            "last_event": None,
        }

    levels: dict[str, int] = {}
    phases: list[str] = []
    for entry in entries:
        level = str(entry.get("level") or "INFO")
        levels[level] = levels.get(level, 0) + 1
        phase = entry.get("phase")
        if isinstance(phase, str) and phase and phase not in phases:
            phases.append(phase)

    last = entries[-1]
    return {
        "entry_count": len(entries),
        "started_at": entries[0].get("timestamp"),
        "ended_at": last.get("timestamp"),
        "levels": levels,
        "phases": phases,
        "error_count": levels.get("ERROR", 0),
        "last_event": last.get("event"),
        "last_message": last.get("message"),
    }
