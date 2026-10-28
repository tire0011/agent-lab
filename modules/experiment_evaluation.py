"""Deterministic (non-LLM) quality evaluation for Nexus experiments.

The verification phase in ``modules.experiment_service`` already runs the
project's test suite and rejects a diff that fails it. That is necessary
but not sufficient: a test suite passing says nothing about whether the
*actual* changes made by the implementation phase match what was planned
and what the original task asked for. The regression this module fixes:
an experiment whose task was "extract the Logs functionality from
``static/app.js`` into a new ``static/logs.js`` module" ended up only
editing ``tests/test_api.py`` (adding tests that happen to already pass
against the *existing*, unchanged ``app.js``) and was still marked
``succeeded``.

This module adds one more, fully deterministic gate between "tests
passed" and "status = succeeded":

  * it classifies the experiment's *actual* changes (tracked-modified vs
    newly-created untracked files, already excluding VCS/venv/cache/log
    noise),
  * it compares them against the structured plan's declared
    ``change_kind`` (``implementation`` / ``tests_only`` / ``documentation``
    / ``analysis``), its named non-test files, and any ``required_artifacts``,
  * and — as a conservative fallback only used when the plan itself names
    nothing concrete — against any explicit repo-relative file path
    literally mentioned in the original task text (e.g. ``static/logs.js``).

No additional LLM call is used anywhere in this module: every check below
is a plain, inspectable, deterministic function over strings/paths.

Two entry points are used by ``ExperimentService``:

  * ``validate_plan(plan, goal)`` — cheap, pre-implementation sanity check
    on the structured plan itself (before spending an implementation Codex
    call). Returns a rejection reason or ``None``.
  * ``evaluate_implementation(plan, goal, changed, worktree_path)`` — the
    post-implementation, post-tests-passed check. Returns an
    ``EvaluationResult`` whose ``outcome`` is ``"pass"`` or
    ``"needs_review"`` — it is never used to promote a *failed* test run to
    success, only to potentially downgrade an otherwise-"tests passed" run.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Change-kind vocabulary
# ---------------------------------------------------------------------------

VALID_CHANGE_KINDS: tuple[str, ...] = ("implementation", "tests_only", "documentation", "analysis")

# Conservative default for plans that predate the ``change_kind`` field
# (backward compatibility, see ``PlanLike``). "implementation" is the
# *strictest* of the four kinds with respect to the checks below (it is
# the only one that unconditionally requires real non-test evidence), so
# defaulting to it can only ever make evaluation stricter for legacy data,
# never accidentally more permissive.
DEFAULT_CHANGE_KIND = "implementation"

# Bounds applied to every list/field written into logs or the DB so a
# pathological diff (thousands of files) can never blow up storage.
MAX_EVAL_LIST_ITEMS = 50
MAX_EVAL_ITEM_CHARS = 300


# ---------------------------------------------------------------------------
# Test-path classification
# ---------------------------------------------------------------------------

_TEST_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)[^/]+\.(?:test|spec)\.[jt]sx?$"),
)


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip('"').replace("\\", "/")
    # Strip a leading "./" prefix (possibly repeated), but never touch a
    # genuine leading dot of a dotfile/dotdir (e.g. ".git/...") — a naive
    # ``str.lstrip("./")`` would incorrectly also eat that leading dot.
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_test_path(path: str) -> bool:
    """True if *path* is a test file/directory by a fixed, inspectable
    naming convention (not a keyword scan of file *content*)."""
    normalized = _normalize_path(path)
    return any(pattern.search(normalized) for pattern in _TEST_PATH_PATTERNS)


def split_test_and_non_test(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """Splits *paths* into ``(test_paths, non_test_paths)``, preserving order."""
    test_paths: list[str] = []
    non_test_paths: list[str] = []
    for raw in paths:
        if not raw:
            continue
        (test_paths if is_test_path(raw) else non_test_paths).append(raw)
    return test_paths, non_test_paths


# ---------------------------------------------------------------------------
# Noise filtering: paths that must never count as "actual implementation
# evidence" even if they somehow surface in a git status listing. This is
# defense in depth — such paths are normally already excluded by
# .gitignore — for the (untracked-file) case where a stray ignored path
# could otherwise still appear.
# ---------------------------------------------------------------------------

_IGNORED_PATH_PREFIXES: tuple[str, ...] = (
    ".git/", ".venv/", ".venv-agent/", "venv/", "env/", "node_modules/",
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    "state/runs/", "state/experiment-logs/", "state/agent.db",
)
_IGNORED_PATH_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".log")


def is_ignored_support_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if any(normalized.startswith(prefix) for prefix in _IGNORED_PATH_PREFIXES):
        return True
    if any(normalized.endswith(suffix) for suffix in _IGNORED_PATH_SUFFIXES):
        return True
    return False


# ---------------------------------------------------------------------------
# Changed-file collection (git status --short / porcelain v1 parsing only;
# the actual subprocess call is made elsewhere with shell=False — this
# function only parses already-captured text, never invokes a shell).
# ---------------------------------------------------------------------------


@dataclass
class ChangedFiles:
    tracked_modified: list[str] = field(default_factory=list)
    untracked_new: list[str] = field(default_factory=list)

    def all_paths(self) -> list[str]:
        return [*self.tracked_modified, *self.untracked_new]


_STATUS_LINE_RE = re.compile(r"^(?P<x>.)(?P<y>.)\s+(?P<rest>.+)$")


def parse_git_status_short(git_status_text: str | None) -> ChangedFiles:
    """Parses ``git status --short`` (porcelain v1) output into tracked vs
    newly-created untracked files, dropping VCS/venv/cache/log noise and
    capping each list at ``MAX_EVAL_LIST_ITEMS`` entries."""
    tracked: list[str] = []
    untracked: list[str] = []
    for line in (git_status_text or "").splitlines():
        if not line.strip():
            continue
        match = _STATUS_LINE_RE.match(line)
        if not match:
            continue
        code_x, code_y, rest = match.group("x"), match.group("y"), match.group("rest")
        # Renames/copies are rendered as "old -> new"; only the resulting
        # (new) path reflects the current worktree contents.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = _normalize_path(rest)
        if not path or is_ignored_support_path(path):
            continue
        if code_x == "?" and code_y == "?":
            untracked.append(path)
        else:
            tracked.append(path)
    return ChangedFiles(
        tracked_modified=tracked[:MAX_EVAL_LIST_ITEMS],
        untracked_new=untracked[:MAX_EVAL_LIST_ITEMS],
    )


# ---------------------------------------------------------------------------
# Explicit file-path extraction from free-text task goals. Used only as a
# conservative fallback "required artifact" source when the plan itself
# names nothing concrete for a non-tests_only change — never to override a
# more specific plan, and never to *approve* anything by itself (a goal
# mentioning no path just means no fallback artifact is added).
# ---------------------------------------------------------------------------

# Requires at least one path separator so incidental mentions of bare
# documentation files (e.g. "see README.md") are not treated as a
# required-artifact path; matches project-relative paths such as
# "static/logs.js" or "modules/experiment_evaluation.py".
_GOAL_PATH_RE = re.compile(r"\b[\w][\w-]*(?:/[\w.-]+)+\.[A-Za-z0-9]{1,8}\b")


def extract_explicit_goal_artifacts(goal: str) -> list[str]:
    # Only the task itself names files to create. The handover appended to it
    # (#263) quotes whatever Nexus looked at - file listings, command output -
    # and made ".git/hooks/*.sample" a required artifact for #349.
    from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING, HANDOVER_HEADING

    task = (goal or "").split(HANDOVER_HEADING, 1)[0].split(EARLIER_ATTEMPTS_HEADING, 1)[0]
    found: list[str] = []
    for match in _GOAL_PATH_RE.finditer(task):
        path = match.group(0)
        if is_test_path(path) or is_ignored_support_path(path):
            continue
        if path not in found:
            found.append(path)
    return found[:10]


# ---------------------------------------------------------------------------
# "tests_only" goal justification: a plan may only declare
# change_kind="tests_only" when the *original* task explicitly and
# narrowly asked for a tests-only change. This intentionally does not try
# to classify arbitrary free text by keyword scanning; it only recognizes
# a small, explicit set of "tests-only" phrasings, and separately rejects
# outright whenever the goal names a concrete non-test file path (a
# concrete file path is a much stronger, structural signal that real
# implementation work was actually requested than any keyword could be).
# ---------------------------------------------------------------------------

_TESTS_ONLY_GOAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnur\s+tests?\b", re.IGNORECASE),
    re.compile(r"\bausschlie(?:ss|\u00df)lich\s+tests?\b", re.IGNORECASE),
    re.compile(r"\btests?[- ]only\b", re.IGNORECASE),
    re.compile(r"\bonly\s+(?:add|write|extend|update|create)\s+tests?\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+(?:changing|modifying|touching)\s+(?:any\s+)?(?:production|application|app)\s*code\b", re.IGNORECASE),
    re.compile(r"\bkeine?\s+(?:produktions|anwendungs)?code[- ]?(?:\u00e4nderungen|aenderungen)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+change\s+(?:any\s+)?production\s+code\b", re.IGNORECASE),
)


def goal_is_explicit_tests_only(goal: str) -> bool:
    text = goal or ""
    for match in _GOAL_PATH_RE.finditer(text):
        path = match.group(0)
        if not is_test_path(path) and not is_ignored_support_path(path):
            return False
    return any(pattern.search(text) for pattern in _TESTS_ONLY_GOAL_PATTERNS)


# ---------------------------------------------------------------------------
# Backward-compatible plan view
# ---------------------------------------------------------------------------


class PlanLike:
    """Minimal, backward-compatible read-only view over a structured plan.

    Accepts either an ``ExperimentPlan`` dataclass instance (has attributes)
    or a plain ``dict`` (as stored in ``experiments.plan_json``/returned by
    ``Experiment.plan``). Older stored plans that predate ``change_kind``,
    ``required_artifacts``, and ``acceptance_criteria`` always get the
    safe, conservative defaults below rather than raising — so historical
    experiments keep working exactly as if they had been explicitly
    declared as a generic ``implementation`` change with no extra
    guarantees, which is the strictest of the four kinds.
    """

    __slots__ = ("title", "affected_files", "change_kind", "required_artifacts", "acceptance_criteria")

    def __init__(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        self.title: str = str(data.get("title") or "")
        affected = data.get("affected_files") or []
        self.affected_files: list[str] = [str(p) for p in affected] if isinstance(affected, list) else []
        change_kind = data.get("change_kind")
        self.change_kind: str = str(change_kind) if change_kind else DEFAULT_CHANGE_KIND
        artifacts = data.get("required_artifacts") or []
        self.required_artifacts: list[str] = [str(p) for p in artifacts] if isinstance(artifacts, list) else []
        criteria = data.get("acceptance_criteria") or []
        self.acceptance_criteria: list[str] = [str(c) for c in criteria] if isinstance(criteria, list) else []

    @classmethod
    def from_any(cls, plan: Any) -> "PlanLike":
        if plan is None:
            return cls(None)
        if isinstance(plan, dict):
            return cls(plan)
        return cls(
            {
                "title": getattr(plan, "title", ""),
                "affected_files": getattr(plan, "affected_files", None),
                "change_kind": getattr(plan, "change_kind", None),
                "required_artifacts": getattr(plan, "required_artifacts", None),
                "acceptance_criteria": getattr(plan, "acceptance_criteria", None),
            }
        )

    def expected_non_test_files(self) -> list[str]:
        _, non_test = split_test_and_non_test(self.affected_files)
        return non_test


# ---------------------------------------------------------------------------
# Pre-implementation plan validation
# ---------------------------------------------------------------------------


def validate_plan(plan: Any, goal: str) -> str | None:
    """Deterministic, pre-implementation completeness check.

    Independent of and additional to
    ``experiment_service.plan_violates_policy`` — never loosens any
    existing safety rule; only adds change-kind-aware completeness rules
    so an insufficient plan is rejected (and, per the existing pipeline,
    gets exactly one policy-replan retry) *before* an expensive
    implementation Codex call is spent on it.

    Returns a human-readable rejection reason, or ``None`` if acceptable.
    """
    p = PlanLike.from_any(plan)

    if p.change_kind not in VALID_CHANGE_KINDS:
        return f"plan declares an unknown change_kind {p.change_kind!r}"

    non_test_affected = p.expected_non_test_files()

    if p.change_kind == "tests_only":
        if non_test_affected:
            return (
                "plan declares change_kind='tests_only' but affected_files names "
                f"non-test file(s): {non_test_affected!r}"
            )
        if not goal_is_explicit_tests_only(goal):
            return (
                "plan declares change_kind='tests_only' but the original task did "
                "not explicitly and exclusively request a tests-only change"
            )
        return None

    if p.change_kind in ("implementation", "documentation"):
        if not non_test_affected and not p.required_artifacts:
            return (
                f"plan declares change_kind={p.change_kind!r} but names no concrete "
                "non-test file or required artifact"
            )
        return None

    # change_kind == "analysis": a read-only investigation is not required
    # to name a file to change; plan_violates_policy()'s existing
    # "must touch a concrete code or test file" rule still applies upstream
    # for any plan that reaches the (workspace-write) implementation phase.
    return None


# ---------------------------------------------------------------------------
# Post-implementation evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvaluationCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name[:MAX_EVAL_ITEM_CHARS],
            "passed": bool(self.passed),
            "detail": self.detail[:MAX_EVAL_ITEM_CHARS],
        }


@dataclass
class EvaluationResult:
    outcome: str  # "pass" or "needs_review"
    reason: str
    checks: list[EvaluationCheck] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason[:MAX_EVAL_ITEM_CHARS * 4],
            "checks": [c.to_dict() for c in self.checks[:MAX_EVAL_LIST_ITEMS]],
            "expected_files": [f[:MAX_EVAL_ITEM_CHARS] for f in self.expected_files[:MAX_EVAL_LIST_ITEMS]],
            "changed_files": [f[:MAX_EVAL_ITEM_CHARS] for f in self.changed_files[:MAX_EVAL_LIST_ITEMS]],
            "missing_artifacts": [f[:MAX_EVAL_ITEM_CHARS] for f in self.missing_artifacts[:MAX_EVAL_LIST_ITEMS]],
        }


def _git_ignored(path: str, worktree_path: str | None) -> bool:
    """True if git ignores *path* in *worktree_path*; never raises."""
    if not worktree_path or not path:
        return False
    try:
        return subprocess.run(
            ["git", "-C", worktree_path, "check-ignore", "-q", "--", _normalize_path(path)],
            capture_output=True, timeout=10, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _is_git_internal(path: str, worktree_path: str | None) -> bool:
    """True if *path* points into git's own directory, which nobody can
    commit (#403). Planners name ``.git/hooks/*.sample`` or
    ``git/outbox_publisher.lock`` - the second with the leading dot lost.
    A ``git/`` path only counts as internal when the worktree has no real
    ``git/`` directory; without a worktree it is treated as internal."""
    normalized = _normalize_path(path)
    if normalized == ".git" or normalized.startswith(".git/"):
        return True
    if normalized == "git" or normalized.startswith("git/"):
        if not worktree_path:
            return True
        try:
            return not (Path(worktree_path) / "git").is_dir()
        except OSError:
            return True
    return False


def drop_git_internal(paths: Iterable[str], worktree_path: str | None) -> tuple[list[str], list[str]]:
    """Splits *paths* into ``(kept, dropped)`` by ``_is_git_internal``."""
    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        (dropped if _is_git_internal(path, worktree_path) else kept).append(path)
    return kept, dropped


def _artifact_exists(artifact: str, worktree_path: str | None) -> bool:
    """True if *artifact* exists as a real file inside *worktree_path*.

    Defensive: refuses absolute paths and ``..`` traversal (never treats
    those as "present" — an artifact outside the worktree can never
    satisfy a requirement about the worktree's own contents), and never
    raises (a filesystem error is treated as "missing", never as "present").
    """
    if not worktree_path or not artifact:
        return False
    normalized = _normalize_path(artifact)
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        return False
    try:
        return (Path(worktree_path) / normalized).is_file()
    except OSError:
        return False


def _diff_contains(artifact: str, changed_paths: list[str]) -> bool:
    normalized = _normalize_path(artifact)
    return any(_normalize_path(p) == normalized for p in changed_paths)


def evaluate_implementation(
    plan: Any,
    goal: str,
    changed: ChangedFiles,
    worktree_path: str | None,
) -> EvaluationResult:
    """Deterministic post-implementation, post-tests-passed quality gate.

    Never called to override a *failed* test run — callers must only
    invoke this after tests have already passed, and must still treat any
    exception raised here as ``needs_review`` (never as ``succeeded``).
    """
    p = PlanLike.from_any(plan)
    # When no structured plan exists (single-call pipeline) and the original
    # goal is exclusively tests-only, promote change_kind accordingly so the
    # evaluator does not incorrectly require non-test file evidence.
    if plan is None and p.change_kind == DEFAULT_CHANGE_KIND and goal_is_explicit_tests_only(goal):
        p.change_kind = "tests_only"
    all_changed = changed.all_paths()
    test_changed, non_test_changed = split_test_and_non_test(all_changed)

    checks: list[EvaluationCheck] = []
    reasons: list[str] = []
    expected_files: list[str] = []
    missing_artifacts: list[str] = []

    if p.change_kind == "tests_only":
        if non_test_changed:
            checks.append(EvaluationCheck(
                "tests_only_scope", False,
                f"plan declares tests_only but non-test file(s) changed: {non_test_changed}",
            ))
            reasons.append(f"unerwartete Nicht-Test-Änderungen bei tests_only-Plan: {non_test_changed}")
        else:
            checks.append(EvaluationCheck(
                "tests_only_scope", True, "only test files changed, consistent with tests_only plan",
            ))
    elif p.change_kind == "analysis":
        checks.append(EvaluationCheck(
            "analysis_scope", True, "analysis/investigation plans are not required to change files",
        ))
    else:
        # "implementation" or "documentation" (and, defensively, any other
        # value — validate_plan() already rejects truly unknown kinds
        # before implementation is ever attempted).
        non_test_affected, dropped_affected = drop_git_internal(p.expected_non_test_files(), worktree_path)

        if not non_test_changed:
            checks.append(EvaluationCheck(
                "non_test_evidence", False,
                "only test files changed (pure test diff) for a non-tests_only plan",
            ))
            reasons.append("nur Tests geändert")
        else:
            checks.append(EvaluationCheck(
                "non_test_evidence", True, f"non-test file(s) changed: {non_test_changed}",
            ))

        required_artifacts, dropped_artifacts = drop_git_internal(p.required_artifacts, worktree_path)
        if not required_artifacts:
            required_artifacts = list(non_test_affected)
        if not required_artifacts:
            required_artifacts = extract_explicit_goal_artifacts(goal)
        expected_files = required_artifacts

        # A git-ignored path (runtime state such as state/mailbox.lock) can
        # never be committed, so it cannot be a required artifact (#185).
        # Neither can a path inside git's own directory (#403).
        required_artifacts = [a for a in required_artifacts if not _git_ignored(a, worktree_path)]
        required_artifacts, dropped_late = drop_git_internal(required_artifacts, worktree_path)
        dropped = list(dict.fromkeys([*dropped_artifacts, *dropped_affected, *dropped_late]))
        dropped_lines = "".join(f"\nignored git-internal path: {d}" for d in dropped)
        missing_artifacts = [a for a in required_artifacts if not _artifact_exists(a, worktree_path)]
        if missing_artifacts:
            checks.append(EvaluationCheck(
                "required_artifacts_present", False, f"missing on disk: {missing_artifacts}{dropped_lines}",
            ))
            for artifact in missing_artifacts:
                reasons.append(f"erforderliches Artefakt {artifact} fehlt")
        else:
            checks.append(EvaluationCheck(
                "required_artifacts_present", True,
                ("all required artifacts present" if required_artifacts else "no required artifacts declared")
                + dropped_lines,
            ))

        if non_test_affected:
            matched = [f for f in non_test_affected if _diff_contains(f, all_changed)]
            if not matched:
                checks.append(EvaluationCheck(
                    "plan_diff_evidence", False,
                    "none of the plan's named non-test files appear in the actual diff",
                ))
                reasons.append(
                    "keine nachvollziehbare Evidenz im Diff für die geplanten Nicht-Test-Änderungen"
                )
            else:
                checks.append(EvaluationCheck(
                    "plan_diff_evidence", True, f"matched planned file(s): {matched}",
                ))

    if reasons:
        outcome = "needs_review"
        reason = "; ".join(dict.fromkeys(reasons))  # de-duplicate, preserve order
    else:
        outcome = "pass"
        reason = "deterministic quality checks passed"

    return EvaluationResult(
        outcome=outcome,
        reason=reason,
        checks=checks,
        expected_files=expected_files,
        changed_files=all_changed,
        missing_artifacts=missing_artifacts,
    )
