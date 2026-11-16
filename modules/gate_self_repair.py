"""Self-repair when one of Nexus' own gates blocks everything (#714).

On 2026-11-14/15 the timing check (#689) failed closed with the same reason on
four experiments in a row. The cause was the host (the service exported the
real API key), not any experiment, and the gate blocked every repair attempt.

This module:

1. **Detects** a host-level blocker: three experiments in a row, on different
   issues, end with the same gate failure reason, and the gate also fails on
   the unchanged ``main``. That is ``host_blocker:<gate>``.
2. **Switches off** an allowlisted gate, persisted in ``service_settings`` so
   it survives restarts, opens a "Nexus needs" issue and records the switch
   for the status report.
3. Lets the **repair** run with the gate off (the normal tick picks the
   blocked work up again; tests and review still apply).
4. **Switches back on** after the next merge: the gate runs once against
   ``main``; on a pass it is on again and the issue is closed, otherwise it
   stays off and the owner gets a comment. There is no second automatic try.

Limits: only gates on :data:`SWITCHABLE_GATES` (gates Nexus added itself) can
be switched off; :data:`PROTECTED_GATES` never; at most one gate is off at a
time. Every switch is logged and kept in a bounded history.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.run_store import get_setting, set_setting

logger = logging.getLogger(__name__)

TIMING_CHECK_GATE = "timing_check"

# Gate -> the env switch that already turns it off. Only gates Nexus added
# itself belong here (#714: starting with the timing check only).
SWITCHABLE_GATES: dict[str, str] = {TIMING_CHECK_GATE: "NEXUS_TIMING_CHECK"}

# Never switched off by this module, whatever the evidence.
PROTECTED_GATES = frozenset({"review", "test_suite", "main_guard", "safeguards", "quality_gate"})

STREAK_LENGTH = 3
HISTORY_LIMIT = 50

STATE_OFF = "off"  # switched off, waiting for the repair to merge
STATE_OFF_OWNER = "off_needs_owner"  # recheck on main failed; no automatic retry

_STATE_KEY_PREFIX = "gate_self_repair:state:"
_STREAK_KEY = "gate_self_repair:streak"
_HISTORY_KEY = "gate_self_repair:history"

_ISSUE_RE = re.compile(r"\bGitHub\s+Issue\s+#(\d+)\b", re.IGNORECASE)
_TIMING_FAILED_CLOSED = "timing check failed closed: "
_TIMING_SENSITIVE = "timing_sensitive: "
_TEST_ID_RE = re.compile(r"(tests/[^\s,;]+?\.py(?:::[^\s,;]+)*)")

# A main check answers True (gate fails on main), False (passes) or None (no verdict).
MainCheck = Callable[[str, list[str]], tuple[bool | None, str]]


@dataclass(frozen=True, slots=True)
class GateFailure:
    gate: str
    reason: str
    tests: tuple[str, ...] = ()


@dataclass(slots=True)
class GateDecision:
    action: str  # "none" | "streak" | "main_passes" | "no_verdict" | "not_switchable" | "busy" | "switched_off"
    gate: str | None = None
    reason: str | None = None
    experiment_ids: list[str] = field(default_factory=list)
    issue_number: int | None = None
    detail: str | None = None

    @property
    def classification(self) -> str | None:
        # Only a gate that also failed on unchanged main is a host blocker.
        if self.action == "switched_off" and self.gate:
            return f"host_blocker:{self.gate}"
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise(text: str) -> str:
    """The reason without the per-experiment test list, so runs compare."""
    text = " ".join(str(text or "").split())
    if text.startswith("error: "):
        text = text[len("error: "):]
    head = text.split(": ", 1)[0]
    return head.strip().rstrip(".,;")[:300]


def classify_gate_failure(error: str | None) -> GateFailure | None:
    """Which gate ended an experiment, and why; None when no gate did."""
    text = str(error or "").strip()
    if not text:
        return None
    tests = tuple(dict.fromkeys(_TEST_ID_RE.findall(text)))
    if text.startswith(_TIMING_FAILED_CLOSED):
        return GateFailure(TIMING_CHECK_GATE, _normalise(text[len(_TIMING_FAILED_CLOSED):]), tests)
    if text.startswith(_TIMING_SENSITIVE):
        return GateFailure(TIMING_CHECK_GATE, "timing_sensitive", tests)
    if text.startswith("tests failed"):
        return GateFailure("test_suite", "tests failed", tests)
    if text.startswith("quality evaluation"):
        return GateFailure("quality_gate", _normalise(text), tests)
    return None


def issue_number_from_goal(goal: str | None) -> int | None:
    match = _ISSUE_RE.search(goal or "")
    return int(match.group(1)) if match else None


# -- persisted state ----------------------------------------------------------

def _load_json(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    try:
        raw = get_setting(conn, key)
    except sqlite3.Error:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _save_json(conn: sqlite3.Connection, key: str, value: Any) -> None:
    set_setting(conn, key, json.dumps(value, sort_keys=True), updated_at=_now())


def gate_state(conn: sqlite3.Connection, gate: str) -> dict[str, Any] | None:
    """The persisted switch-off record of *gate*, or None when it is on."""
    state = _load_json(conn, _STATE_KEY_PREFIX + gate, None)
    if isinstance(state, dict) and state.get("state") in {STATE_OFF, STATE_OFF_OWNER}:
        return state
    return None


def gate_is_off(conn: sqlite3.Connection, gate: str) -> bool:
    return gate_state(conn, gate) is not None


def gates_off(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {gate: state for gate in SWITCHABLE_GATES if (state := gate_state(conn, gate))}


def history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    entries = _load_json(conn, _HISTORY_KEY, [])
    return entries if isinstance(entries, list) else []


def _log(conn: sqlite3.Connection, event: str, gate: str, **details: Any) -> None:
    entry = {"at": _now(), "event": event, "gate": gate, **details}
    logger.warning("gate self-repair: %s", json.dumps(entry, sort_keys=True))
    _save_json(conn, _HISTORY_KEY, [*history(conn), entry][-HISTORY_LIMIT:])


def status_lines(conn: sqlite3.Connection) -> list[str]:
    """Lines for the status report: which gate is off and why."""
    lines = []
    for gate, state in gates_off(conn).items():
        issue = f", issue #{state['issue_number']}" if state.get("issue_number") else ""
        waiting = "owner" if state.get("state") == STATE_OFF_OWNER else "repair merge"
        lines.append(
            f"gate `{gate}` switched off since {str(state.get('switched_off_at', ''))[:16]} "
            f"(host_blocker: {state.get('reason')}; waiting for {waiting}{issue})"
        )
    return lines


# -- main check ---------------------------------------------------------------

def timing_check_on_main(repo_root: str, tests: list[str], *, python: str | None = None,
                         ref: str = "origin/main") -> tuple[bool | None, str]:
    """Run the timing check once on unchanged *ref*: (fails, detail)."""
    from modules import timing_check

    if not tests:
        return None, "no tests to check"
    parent = tempfile.mkdtemp(prefix="nexus-gate-main-")
    path = str(Path(parent) / "worktree")
    added = False
    try:
        subprocess.run(["git", "fetch", "origin", "main"], cwd=repo_root, capture_output=True,
                       text=True, timeout=120, check=False)
        proc = subprocess.run(["git", "worktree", "add", "--detach", path, ref], cwd=repo_root,
                              capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            return None, f"worktree on {ref} failed: {proc.stderr.strip()[:200]}"
        added = True
        present = [test for test in tests if (Path(path) / test.split("::", 1)[0]).is_file()]
        if not present:
            return None, "none of the tests exist on main"
        result = timing_check.run_timing_check(path, present, python=python)
        if result == "ok":
            return False, "timing check passed on main"
        flagged = timing_check.flagged_tests(result)
        if not flagged:
            return None, f"timing check on main gave no verdict: {result}"
        control = timing_check.control_check(path, flagged, python=python)
        if control is None:
            return None, "control run on main gave no verdict"
        drift_only, without_drift = control
        if without_drift:
            return True, "fail on main without drift too: " + ", ".join(without_drift)
        return True, "timing-sensitive on main: " + ", ".join(drift_only)
    except Exception as exc:  # noqa: BLE001 - a crashed check is no verdict
        return None, f"main check crashed: {type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if added:
            subprocess.run(["git", "worktree", "remove", "--force", path], cwd=repo_root,
                           capture_output=True, text=True, timeout=60, check=False)
        import shutil

        shutil.rmtree(parent, ignore_errors=True)


# -- the controller -----------------------------------------------------------

class GateSelfRepair:
    """Detection, switch-off and switch-on of host-blocked gates (#714).

    *main_checks* maps a gate to a callable ``(repo_root, tests) -> (fails,
    detail)``; *github* needs ``open_need_issue``, ``comment`` and
    ``close_issue`` (all failures there are logged, never raised).
    """

    def __init__(self, conn: sqlite3.Connection, *, repo_root: str,
                 main_checks: dict[str, MainCheck] | None = None,
                 lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self.repo_root = repo_root
        self.main_checks = main_checks if main_checks is not None else {
            TIMING_CHECK_GATE: lambda root, tests: timing_check_on_main(root, tests),
        }
        self._lock = lock or threading.Lock()

    # Detection --------------------------------------------------------------

    def record_outcome(self, experiment_id: str, error: str | None, *, issue_number: int | None,
                       github: Any | None = None) -> GateDecision:
        """Feed one finished experiment; may switch an allowlisted gate off."""
        failure = classify_gate_failure(error)
        with self._lock:
            streak = _load_json(self._conn, _STREAK_KEY, [])
            if not isinstance(streak, list):
                streak = []
            if failure is None:
                _save_json(self._conn, _STREAK_KEY, [])
                return GateDecision("none")
            if issue_number is None:
                # Distinct issues cannot be shown: neither counts nor breaks the row.
                return GateDecision("none", failure.gate, failure.reason)
            same = [e for e in streak if e.get("gate") == failure.gate and e.get("reason") == failure.reason]
            if len(same) != len(streak):
                streak = []
            if not any(e.get("issue_number") == issue_number for e in streak):
                streak.append({
                    "experiment_id": experiment_id, "issue_number": issue_number,
                    "gate": failure.gate, "reason": failure.reason, "tests": list(failure.tests),
                })
            _save_json(self._conn, _STREAK_KEY, streak)
        decision = GateDecision(
            "streak", failure.gate, failure.reason,
            [e["experiment_id"] for e in streak],
        )
        if len(streak) < STREAK_LENGTH:
            return decision
        return self._on_streak(failure, streak, github)

    def _on_streak(self, failure: GateFailure, streak: list[dict[str, Any]],
                   github: Any | None) -> GateDecision:
        ids = [e["experiment_id"] for e in streak]
        gate = failure.gate
        if gate in PROTECTED_GATES or gate not in SWITCHABLE_GATES:
            self._log("repeated_failure_not_switchable", gate, reason=failure.reason, experiment_ids=ids)
            self._reset_streak()
            return GateDecision("not_switchable", gate, failure.reason, ids)
        with self._lock:
            off = gates_off(self._conn)
        if off:
            self._log("repeated_failure_other_gate_off", gate, reason=failure.reason,
                      experiment_ids=ids, already_off=sorted(off))
            self._reset_streak()
            return GateDecision("busy", gate, failure.reason, ids)
        tests = list(dict.fromkeys(t for e in streak for t in e.get("tests") or []))
        fails, detail = self._main_check(gate, tests)
        if fails is not True:
            # Main passes (or gives no verdict): the experiments are at fault.
            self._log("host_blocker_rejected", gate, reason=failure.reason,
                      experiment_ids=ids, main=detail)
            self._reset_streak()
            return GateDecision("main_passes" if fails is False else "no_verdict",
                                gate, failure.reason, ids, detail=detail)
        return self._switch_off(gate, failure.reason, streak, detail, github)

    def _main_check(self, gate: str, tests: list[str]) -> tuple[bool | None, str]:
        check = self.main_checks.get(gate)
        if check is None:
            return None, "no main check for this gate"
        try:
            return check(self.repo_root, tests)
        except Exception as exc:  # noqa: BLE001 - no verdict, never a pass
            return None, f"main check crashed: {type(exc).__name__}: {str(exc)[:200]}"

    def _reset_streak(self) -> None:
        with self._lock:
            _save_json(self._conn, _STREAK_KEY, [])

    # Switch off -------------------------------------------------------------

    def _switch_off(self, gate: str, reason: str, streak: list[dict[str, Any]], main_detail: str,
                    github: Any | None) -> GateDecision:
        ids = [e["experiment_id"] for e in streak]
        issues = [e["issue_number"] for e in streak]
        state = {
            "state": STATE_OFF, "gate": gate, "env_var": SWITCHABLE_GATES[gate],
            "reason": reason, "experiment_ids": ids, "issue_numbers": issues,
            "tests": list(dict.fromkeys(t for e in streak for t in e.get("tests") or [])),
            "main_detail": main_detail, "switched_off_at": _now(), "issue_number": None,
        }
        with self._lock:
            _save_json(self._conn, _STATE_KEY_PREFIX + gate, state)
            _save_json(self._conn, _STREAK_KEY, [])
        self._log("switched_off", gate, reason=reason, experiment_ids=ids, main=main_detail)
        number = self._open_issue(github, gate, reason, ids, issues, main_detail)
        if number is not None:
            state["issue_number"] = number
            with self._lock:
                _save_json(self._conn, _STATE_KEY_PREFIX + gate, state)
        return GateDecision("switched_off", gate, reason, ids, issue_number=number, detail=main_detail)

    def _open_issue(self, github: Any | None, gate: str, reason: str, ids: list[str],
                    issues: list[int], main_detail: str) -> int | None:
        if github is None or not callable(getattr(github, "open_need_issue", None)):
            self._log("needs_issue_skipped", gate, reason="no GitHub service")
            return None
        body = (
            f"Nexus classified `host_blocker:{gate}`: its own gate `{gate}` failed with the same "
            f"reason on {len(ids)} experiments in a row on different issues, and also on the "
            "unchanged `main`. The cause is on this host, not in the experiments.\n\n"
            f"- Gate: `{gate}` (switched off: `{SWITCHABLE_GATES[gate]}=0`, persisted)\n"
            f"- Reason: {reason}\n"
            f"- Experiments: {', '.join(f'`{i}`' for i in ids)}\n"
            f"- Issues: {', '.join(f'#{n}' for n in issues)}\n"
            f"- Check on main: {main_detail}\n\n"
            "The repair runs with this gate off; the normal tests and review still apply. "
            "After the next merge Nexus runs the gate once on `main`: a pass switches it on "
            "again and closes this issue, a failure leaves it off and comments here (#714)."
        )
        try:
            issue = github.open_need_issue(f"gate {gate} blocks every experiment on this host", body)
        except Exception as exc:  # noqa: BLE001 - the switch already holds
            self._log("needs_issue_failed", gate, error=str(exc)[:300])
            return None
        number = issue.get("number") if isinstance(issue, dict) else None
        return int(number) if number else None

    # Switch back on ---------------------------------------------------------

    def after_merge(self, experiment_id: str, *, github: Any | None = None) -> GateDecision:
        """After a merged repair: run the off gate once on main, decide once."""
        with self._lock:
            off = gates_off(self._conn)
        for gate, state in off.items():
            if state.get("state") != STATE_OFF:
                continue  # already rechecked once; the owner decides (no loop)
            fails, detail = self._main_check(gate, list(state.get("tests") or []))
            number = state.get("issue_number")
            if fails is False:
                with self._lock:
                    _save_json(self._conn, _STATE_KEY_PREFIX + gate,
                               {"state": "on", "gate": gate, "switched_on_at": _now(),
                                "repair_experiment_id": experiment_id})
                self._log("switched_on", gate, repair_experiment_id=experiment_id, main=detail)
                if number and github is not None:
                    self._github_call(gate, github, "comment", number,
                                      f"Repair `{experiment_id}` merged; `{gate}` passed on `main` "
                                      f"and is switched on again ({detail}).")
                    self._github_call(gate, github, "close_issue", number)
                return GateDecision("switched_on", gate, state.get("reason"),
                                    list(state.get("experiment_ids") or []), number, detail)
            state.update(state=STATE_OFF_OWNER, recheck_at=_now(), recheck_detail=detail,
                         repair_experiment_id=experiment_id)
            with self._lock:
                _save_json(self._conn, _STATE_KEY_PREFIX + gate, state)
            self._log("switch_on_failed", gate, repair_experiment_id=experiment_id, main=detail)
            if number and github is not None:
                self._github_call(gate, github, "comment", number,
                                  f"Repair `{experiment_id}` merged, but `{gate}` still does not pass on "
                                  f"`main` ({detail}). The gate stays off; Nexus will not retry on its "
                                  "own. Owner: please look at the host, then switch it on with "
                                  "`gate_self_repair.switch_on_by_owner`.")
            return GateDecision("still_off", gate, state.get("reason"),
                                list(state.get("experiment_ids") or []), number, detail)
        return GateDecision("none")

    def switch_on_by_owner(self, gate: str) -> bool:
        """The owner's way out of :data:`STATE_OFF_OWNER`."""
        with self._lock:
            if not gate_is_off(self._conn, gate):
                return False
        with self._lock:
            _save_json(self._conn, _STATE_KEY_PREFIX + gate,
                       {"state": "on", "gate": gate, "switched_on_at": _now(), "by": "owner"})
        self._log("switched_on", gate, by="owner")
        return True

    def _log(self, event: str, gate: str, **details: Any) -> None:
        with self._lock:
            _log(self._conn, event, gate, **details)

    def _github_call(self, gate: str, github: Any, name: str, *args: Any) -> None:
        method = getattr(github, name, None)
        if not callable(method):
            return
        try:
            method(*args)
        except Exception as exc:  # noqa: BLE001 - reporting never undoes a switch
            self._log(f"{name}_failed", gate, error=str(exc)[:300])
