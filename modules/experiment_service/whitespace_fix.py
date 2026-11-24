"""Whitespace autofix for ``git diff --check`` findings (#817).

A ``git diff --check`` report is purely mechanical dirt: trailing spaces, tabs
before indentation or blank lines at the end of a file. Coding agents produce
such lines now and then, and before #817 every one of them ended a verified
run as ``needs_review`` and burned an attempt of the issue.

:func:`fix_whitespace` rewrites **only the lines git reported**: it strips
trailing spaces/tabs on those lines and removes the extra blank lines at the
end of the file. It never touches a line ``git diff --check`` did not report,
a binary file, a file outside the worktree, or a ``.md`` line that ends in
exactly two spaces (a Markdown hard break). Leftover conflict markers are
never fixed automatically - they stay in the remaining report.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


# One `git diff --check` finding is two output lines: the finding itself,
# "<path>:<line>: <message>", followed by the offending diff line ("+...").
FINDING_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+): (?P<message>.*)$")

TRAILING_WHITESPACE = "trailing whitespace"
SPACE_BEFORE_TAB = "space before tab in indent"
BLANK_LINE_AT_EOF = "new blank line at EOF"
# Never auto-fixed: a conflict marker is a real problem, not dirt.
CONFLICT_MARKER = "leftover conflict marker"

_TRAILING_WS_RE = re.compile(r"[ \t]+$")
_INDENT_RE = re.compile(r"[ \t]*")


@dataclass
class WhitespaceFixResult:
    """Outcome of one :func:`fix_whitespace` call.

    ``fixed_files`` maps a repo-relative path to the number of lines changed
    in it (trailing whitespace stripped, indent fixed, blank EOF lines
    removed). ``remaining_report`` is the ``git diff --check`` report after
    the fix; conflict markers and anything unfixable stay in it.
    """

    fixed_files: dict[str, int] = field(default_factory=dict)
    remaining_report: str = ""

    @property
    def fixed_line_count(self) -> int:
        return sum(self.fixed_files.values())


def parse_diff_check_report(report: str) -> dict[str, list[tuple[int, str]]]:
    """Parse ``git diff --check`` output into ``path -> [(line, message)]``.

    The echoed diff line ("+...") that follows each finding is skipped, so it
    can never be mistaken for a finding itself.
    """
    findings: dict[str, list[tuple[int, str]]] = {}
    lines = report.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        match = FINDING_RE.match(line)
        if match:
            path = match.group("path")
            findings.setdefault(path, []).append(
                (int(match.group("line")), match.group("message"))
            )
            if (BLANK_LINE_AT_EOF not in match.group("message")
                    and index + 1 < len(lines) and lines[index + 1].startswith("+")):
                index += 1  # the offending line echoed by git
        index += 1
    return findings


def _safe_target(worktree: Path, rel_path: str) -> Path | None:
    """Absolute path of a reported file, or None when it leaves the worktree."""
    candidate = Path(rel_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = worktree.resolve()
    target = (root / candidate).resolve()
    if target != root / candidate or not target.is_relative_to(root):
        return None
    return target


@contextmanager
def _worktree_index(worktree_path: str, base_ref: str):
    """Snapshot the worktree, including untracked files, in a throwaway index."""
    with tempfile.TemporaryDirectory(prefix="whitespace-fix-index-") as index_dir:
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
        subprocess.run(
            ["git", "read-tree", base_ref],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
            check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=300,
            check=True,
        )
        yield env


def _run_diff_check(worktree_path: str, base_ref: str, env: dict[str, str]) -> str:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--check", base_ref],
        cwd=worktree_path, capture_output=True, env=env, timeout=120,
    )
    proc.stdout = os.fsdecode(proc.stdout)
    proc.stderr = os.fsdecode(proc.stderr)
    return diff_check_report(proc)


def _added_lines(worktree_path: str, base_ref: str, env: dict[str, str], path: str) -> set[int]:
    """Return destination line numbers from zero-context diff hunks."""
    patch = subprocess.run(
        ["git", "--literal-pathspecs", "diff", "--cached", "--no-ext-diff",
         "--no-textconv", "--no-renames", "--unified=0", "--inter-hunk-context=0",
         base_ref, "--", path],
        cwd=worktree_path, capture_output=True, text=True, errors="surrogateescape",
        env=env, timeout=120, check=True,
    ).stdout
    return {
        line
        for start, count in re.findall(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", patch, re.M)
        for line in range(int(start), int(start) + int(count or "1"))
    }


def diff_check_report(proc: subprocess.CompletedProcess) -> str:
    """Read findings and reject Git failures, including failures with no output."""
    report = proc.stdout.rstrip("\n")
    # Git diff --check exits 2 when it emits whitespace findings.
    if proc.returncode != 2 or not report or proc.stderr.strip():
        proc.check_returncode()
    return report


def _is_markdown_hard_break(body: str) -> bool:
    """True when the line's trailing whitespace is exactly two spaces."""
    match = _TRAILING_WS_RE.search(body)
    return match is not None and match.group() == "  "


def _fix_space_before_tab(body: str) -> str:
    """Drop spaces that directly precede a tab in the leading indentation."""
    indent = _INDENT_RE.match(body).group()
    if " \t" not in indent:
        return body
    fixed = indent
    while " \t" in fixed:
        fixed = fixed.replace(" \t", "\t")
    return fixed + body[len(indent):]


def _fix_file(
    target: Path, rel_path: str, findings: list[tuple[int, str]], git_dir: Path,
    eof_added_lines: set[int] | None = None,
) -> int:
    """Rewrite one file atomically and count each changed or deleted line once."""
    raw = target.read_bytes()
    if b"\0" in raw:
        return 0
    # Git counts LF-delimited lines, including when the file contains other
    # control characters or Unicode line separators.
    lines = raw.decode("utf-8", errors="surrogateescape").split("\n")
    is_markdown = rel_path.lower().endswith(".md")
    messages_by_line: dict[int, list[str]] = {}
    for line_no, message in findings:
        messages_by_line.setdefault(line_no, []).append(message)
    changed_lines: set[int] = set()
    eof_start = len(lines)
    for line_no, messages in messages_by_line.items():
        index = line_no - 1
        if not 0 <= index < len(lines):
            continue
        if any(CONFLICT_MARKER in message for message in messages):
            continue
        line = lines[index]
        body = line.removesuffix("\r")
        ending = line[len(body):]
        if any(BLANK_LINE_AT_EOF in message for message in messages):
            eof_start = min(eof_start, index)
        if is_markdown and _is_markdown_hard_break(body):
            continue
        new_body = body
        if any(SPACE_BEFORE_TAB in message for message in messages):
            new_body = _fix_space_before_tab(new_body)
        if any(TRAILING_WHITESPACE in message for message in messages):
            new_body = new_body.rstrip(" \t")
        if new_body != body:
            lines[index] = new_body + ending
            changed_lines.add(index)

    # The EOF diagnostic may point at a pre-existing blank. Use the actual
    # additions in the snapshot to preserve every unchanged blank in the suffix.
    deleted_lines: set[int] = set()
    if eof_added_lines:
        end = len(lines) - int(lines[-1] == "")
        for index in range(end - 1, eof_start - 1, -1):
            body = lines[index].removesuffix("\r")
            if body.strip(" \t"):
                break
            if index + 1 in eof_added_lines and not (is_markdown and _is_markdown_hard_break(body)):
                deleted_lines.add(index)
        changed_lines.update(deleted_lines)
        if len(lines) - 1 in deleted_lines:
            lines.append("")  # Keep the preceding line terminator.
        lines = [line for index, line in enumerate(lines) if index not in deleted_lines]
    if changed_lines:
        # Keep temporary files in Git metadata so a killed worker cannot leave
        # a partial file that a subsequent git add -A would pick up.
        with tempfile.TemporaryDirectory(prefix="whitespace-fix-", dir=git_dir) as temp_dir:
            replacement = Path(temp_dir) / "replacement"
            replacement.write_bytes("\n".join(lines).encode("utf-8", errors="surrogateescape"))
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            replacement.replace(target)
    return len(changed_lines)


def fix_whitespace(worktree_path: str, base_ref: str = "HEAD") -> WhitespaceFixResult:
    """Fix the whitespace findings of ``git diff --check <base_ref>`` in the worktree.

    Parses the check output, rewrites only the reported lines of each reported
    text file inside the worktree, then re-runs the check: the returned
    ``remaining_report`` is what still stands afterwards (conflict markers,
    unfixable or skipped findings). Raises when the git plumbing itself fails -
    callers then keep the original report.
    """
    worktree = Path(worktree_path)
    with _worktree_index(worktree_path, base_ref) as env:
        report = _run_diff_check(worktree_path, base_ref, env)
        if not report:
            return WhitespaceFixResult()
        paths = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z", "--no-renames", base_ref],
            cwd=worktree_path, capture_output=True, env=env, timeout=120, check=True,
        ).stdout
        # Git prints raw filenames in --check, so a filename containing LF
        # makes the corresponding report ambiguous.
        if b"\n" in paths:
            return WhitespaceFixResult(remaining_report=report)
        findings = parse_diff_check_report(report)
        eof_added_lines = {
            path: _added_lines(worktree_path, base_ref, env, path)
            for path, messages in findings.items()
            if any(BLANK_LINE_AT_EOF in message for _, message in messages)
        }
    fixed_files: dict[str, int] = {}
    if not findings:
        return WhitespaceFixResult({}, report)
    git_dir = Path(subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=worktree_path,
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout.rstrip("\n"))
    for rel_path, file_findings in findings.items():
        target = _safe_target(worktree, rel_path)
        if target is None or not target.is_file():
            continue  # outside the worktree or gone: leave it to the report
        try:
            changed = _fix_file(target, rel_path, file_findings, git_dir, eof_added_lines.get(rel_path))
        except OSError:
            continue
        if changed:
            fixed_files[rel_path] = changed
    if not fixed_files:
        return WhitespaceFixResult({}, report)
    with _worktree_index(worktree_path, base_ref) as env:
        remaining = _run_diff_check(worktree_path, base_ref, env)
    return WhitespaceFixResult(fixed_files, remaining)
