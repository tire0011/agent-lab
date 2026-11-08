"""Deterministic (non-LLM) quality evaluation for Nexus experiments.

An extra gate between "tests passed" and "succeeded": classifies the experiment's
actual changes and compares them against the plan's declared change_kind, named
non-test files, and required_artifacts (fallback: file paths named in the task).
Entry points: ``validate_plan`` (pre-implementation) and
``evaluate_implementation`` (post-tests; never promotes a failed test run).
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Change-kind vocabulary

VALID_CHANGE_KINDS: tuple[str, ...] = ("implementation", "tests_only", "documentation", "analysis")

# Default for legacy plans without change_kind: "implementation" is the strictest
# kind, so defaulting can only make legacy evaluation stricter, never looser.
DEFAULT_CHANGE_KIND = "implementation"

# Bounds on every list written to logs/DB so a pathological diff cannot blow up storage.
MAX_EVAL_LIST_ITEMS = 50
MAX_EVAL_ITEM_CHARS = 300


# Test-path classification

_TEST_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)[^/]+\.(?:test|spec)\.[jt]sx?$"),
)


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip('"').replace("\\", "/")
    # Strip a leading "./" (repeatedly), but never a genuine dot of a dotfile -
    # lstrip("./") would eat that leading dot too.
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_test_path(path: str) -> bool:
    """True if *path* is a test file/directory by fixed naming convention (no
    content scan)."""
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


# Noise filtering: paths that must never count as implementation evidence
# (defense in depth; .gitignore normally excludes them already).

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


# Changed-file collection: parses captured `git status --short` text only; the
# subprocess elsewhere runs with shell=False - this module never invokes a shell.


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
        # Renames/copies render as "old -> new"; only the new path reflects the worktree.
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


# Explicit file paths extracted from task goals - a conservative fallback artifact
# source when the plan names nothing concrete; never overrides the plan, never
# approves anything by itself.

# At least one path separator, so bare doc mentions ("see README.md") are not
# artifacts; matches project-relative paths like "static/logs.js".
_GOAL_PATH_RE = re.compile(r"\b[\w][\w-]*(?:/[\w.-]+)+\.[A-Za-z0-9]{1,8}\b")


# A section heading at the start of a line: "Change:", "**Change**", "## Change".
_SECTION_HEADING_RE = re.compile(
    r"^[ \t]*(?:#+[ \t]*|\*\*)?(problem|change|changes|test|tests|why|context|background|notes?)\b"
    r"(?:\*\*|[ \t]*:)",
    re.IGNORECASE | re.MULTILINE,
)


def _change_section(task: str) -> str:
    """The Change section of a sectioned issue, else the whole text - Problem and
    Test sections name existing files or fixtures (#416, #403)."""
    headings = list(_SECTION_HEADING_RE.finditer(task))
    for index, heading in enumerate(headings):
        if heading.group(1).lower() in ("change", "changes"):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(task)
            return task[heading.start():end]
    return task


def extract_explicit_goal_artifacts(goal: str) -> list[str]:
    # Only the task itself names files to create; the appended handover (#263)
    # quotes listings/output and once made ".git/hooks/*.sample" an artifact (#349).
    from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING, HANDOVER_HEADING

    task = (goal or "").split(HANDOVER_HEADING, 1)[0].split(EARLIER_ATTEMPTS_HEADING, 1)[0]
    task = _change_section(task)
    found: list[str] = []
    for match in _GOAL_PATH_RE.finditer(task):
        path = match.group(0)
        if match.start() > 0 and task[match.start() - 1] == ".":
            # "\b" starts after a leading dot: ".git/hooks/x" would become
            # "git/hooks/x". Dot paths are never artifacts (#403).
            continue
        if path.startswith("git/"):
            # Gate-output paths without the dot; the repo has no git/ directory (#403).
            continue
        if is_test_path(path) or is_ignored_support_path(path) or _is_placeholder_path(path):
            continue
        if path not in found:
            found.append(path)
    return found[:10]


# Example names in an issue ("modules/a.py", "new_thing.py") are not files
# the change must create (#433, #455).
_PLACEHOLDER_STEMS = frozenset({"foo", "bar", "baz", "example", "new_thing", "some_file", "my_module"})


def _is_placeholder_path(path: str) -> bool:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return len(stem) == 1 or stem in _PLACEHOLDER_STEMS


# "tests_only" justification: only an explicit, narrow tests-only request or
# structurally test-only paths may declare tests_only; a concrete non-test path is
# stronger evidence that implementation work was requested.

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


def goal_names_only_test_paths(goal: str) -> bool:
    """True when every path named by *goal* is test/support-only; at least one must
    be a real test path - naming no paths is not evidence to relax the gate."""
    paths = [match.group(0) for match in _GOAL_PATH_RE.finditer(goal or "")]
    return (
        any(is_test_path(path) for path in paths)
        and all(is_test_path(path) or is_ignored_support_path(path) for path in paths)
    )


# Backward-compatible plan view


class PlanLike:
    """Minimal read-only view over a plan (dataclass or dict from plan_json).

    Plans predating change_kind/required_artifacts get the safe defaults - the
    equivalent of a plain ``implementation`` plan, the strictest kind.
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


# Pre-implementation plan validation


def validate_plan(plan: Any, goal: str) -> str | None:
    """Deterministic pre-implementation completeness check; returns a rejection
    reason or ``None``.

    Additional to ``plan_violates_policy`` - never loosens an existing safety
    rule; rejects insufficient plans before an implementation call is spent.
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
        if not (goal_is_explicit_tests_only(goal) or goal_names_only_test_paths(goal)):
            return (
                "plan declares change_kind='tests_only' but the original task did "
                "not explicitly request a tests-only change or name only test paths"
            )
        return None

    if p.change_kind in ("implementation", "documentation"):
        if not non_test_affected and not p.required_artifacts:
            return (
                f"plan declares change_kind={p.change_kind!r} but names no concrete "
                "non-test file or required artifact"
            )
        return None

    # change_kind == "analysis": a read-only investigation need not name a file
    # to change; the upstream "must touch a concrete file" rule still applies
    # before any workspace-write phase.
    return None


# Post-implementation evaluation


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


# Checks whose failure, with green tests, goes to the reviewer instead of
# stopping the run (#519).
SOFT_CHECKS = frozenset({"required_artifacts_present", "non_test_evidence", "plan_diff_evidence"})


@dataclass
class EvaluationResult:
    outcome: str  # "pass" or "needs_review"
    reason: str
    checks: list[EvaluationCheck] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)

    def soft_findings(self) -> str | None:
        """The reason when only artifact-evidence checks failed, else ``None``.

        Those checks read paths from free task text and false-alarmed often (#461,
        #499, #513); with green tests they go to the reviewer (#519). Any other
        failed check, or an empty diff, stays a hard stop.
        """
        failed = [c for c in self.checks if not c.passed]
        if self.outcome == "pass" or not failed or not self.changed_files:
            return None
        if any(c.name not in SOFT_CHECKS for c in failed):
            return None
        return self.reason

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


def _artifact_exists(artifact: str, worktree_path: str | None) -> bool:
    """True if *artifact* exists as a real file inside *worktree_path*.

    Refuses absolute paths and ``..`` traversal (outside the worktree never
    satisfies a worktree requirement) and never raises - a filesystem error
    counts as missing, never as present.
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

    Never overrides a *failed* test run, and callers must treat any exception
    raised here as ``needs_review``, never as ``succeeded``.
    """
    p = PlanLike.from_any(plan)
    all_changed = changed.all_paths()
    test_changed, non_test_changed = split_test_and_non_test(all_changed)
    # A plan-less run, or a plan naming only test files, may be promoted when the
    # goal asks for tests only; any affected non-test file keeps the stricter
    # non-test-evidence gate (#493: the planner may list non-test files it only
    # had to read).
    affected_files_are_tests_only = (
        bool(p.affected_files) and all(is_test_path(path) for path in p.affected_files)
    )
    explicit_tests_only = goal_is_explicit_tests_only(goal)
    goal_allows_tests_only = explicit_tests_only or goal_names_only_test_paths(goal)
    only_tests_changed = bool(test_changed) and not non_test_changed
    if p.change_kind == DEFAULT_CHANGE_KIND and (
        ((plan is None or affected_files_are_tests_only) and goal_allows_tests_only)
        or (explicit_tests_only and only_tests_changed)
    ):
        p.change_kind = "tests_only"

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
        # "implementation" or "documentation" (defensively any other value -
        # validate_plan() rejects unknown kinds before implementation).
        non_test_affected = p.expected_non_test_files()

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

        required_artifacts = list(p.required_artifacts)
        if not required_artifacts:
            required_artifacts = list(non_test_affected)
        if not required_artifacts:
            required_artifacts = extract_explicit_goal_artifacts(goal)
        expected_files = required_artifacts

        # A git-ignored path (runtime state such as state/mailbox.lock) can never
        # be committed, so it cannot be a required artifact (#185).
        required_artifacts = [a for a in required_artifacts if not _git_ignored(a, worktree_path)]
        missing_artifacts = [a for a in required_artifacts if not _artifact_exists(a, worktree_path)]
        if missing_artifacts:
            checks.append(EvaluationCheck(
                "required_artifacts_present", False, f"missing on disk: {missing_artifacts}",
            ))
            for artifact in missing_artifacts:
                reasons.append(f"erforderliches Artefakt {artifact} fehlt")
        else:
            checks.append(EvaluationCheck(
                "required_artifacts_present", True,
                "all required artifacts present" if required_artifacts else "no required artifacts declared",
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
        reason = "; ".join(dict.fromkeys(reasons))
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


# Compact history view of a stored evaluation error (#444)

_ERROR_PREFIX = "quality evaluation: "
_MISSING_ARTIFACT_RE = re.compile(r"erforderliches Artefakt (.+) fehlt")
_MAX_EXAMPLE_PATHS = 3
_MAX_PATH_GROUPS = 3


def _cut_at_word(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut at a word boundary if it must be."""
    if len(text) <= limit:
        return text
    head, space, _ = text[:limit].rpartition(" ")
    # A single word longer than the limit has no boundary to land on.
    return (head.rstrip(" ;,") if space else text[: limit - 1]) + "…"


def summarize_error(error: str, limit: int = 200) -> str:
    """``error`` in one line of at most ``limit`` characters.

    Repeated "artifact <path> missing" items become a total plus per-directory
    counts and a few examples, so one real gap stays visible beside dozens of
    git/ false positives; other reasons are kept. An error that fits needs no
    summary.
    """
    text = " ".join(str(error or "").split())
    if len(text) <= limit:
        return text

    items = [item.strip() for item in text.removeprefix(_ERROR_PREFIX).split(";")]
    matches = [_MISSING_ARTIFACT_RE.fullmatch(item) for item in items]
    paths = list(dict.fromkeys(match.group(1) for match in matches if match))
    if len(paths) < 2:
        return _cut_at_word(text, limit)

    first_in_dir: dict[str, str] = {}
    for path in paths:
        first_in_dir.setdefault(path.split("/", 1)[0], path)
    examples = list(dict.fromkeys([*first_in_dir.values(), *paths]))

    dirs = Counter(path.split("/", 1)[0] for path in paths if "/" in path)
    groups = ", ".join(f"{name}/: {n}" for name, n in dirs.most_common(_MAX_PATH_GROUPS) if n > 1)
    head = f"{_ERROR_PREFIX}{len(paths)} required artifacts missing" + (f" ({groups})" if groups else "")
    others = "".join(f"; {item}" for item, match in zip(items, matches) if item and not match)

    # Long paths give way before the counts and the other reasons do.
    for shown in range(min(_MAX_EXAMPLE_PATHS, len(paths)), 0, -1):
        rest = len(paths) - shown
        more = f" (+{rest} more)" if rest else ""
        summary = f"{head}, e.g. {', '.join(examples[:shown])}{more}{others}"
        if len(summary) <= limit:
            return summary
    return _cut_at_word(f"{head}{others}", limit)
