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
import subprocess
import tempfile
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


def _unquote_path(path: str) -> str:
    """Undo git's C-style path quoting for the common escapes."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
        return re.sub(
            r"\\(.)", lambda m: escapes.get(m.group(1), m.group(1)), path[1:-1]
        )
    return path


def parse_diff_check_report(report: str) -> dict[str, list[tuple[int, str]]]:
    """Parse ``git diff --check`` output into ``path -> [(line, message)]``.

    The echoed diff line ("+...") that follows each finding is skipped, so it
    can never be mistaken for a finding itself.
    """
    findings: dict[str, list[tuple[int, str]]] = {}
    lines = report.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = FINDING_RE.match(line)
        if match and not line.startswith("+"):
            path = _unquote_path(match.group("path"))
            findings.setdefault(path, []).append(
                (int(match.group("line")), match.group("message"))
            )
            if index + 1 < len(lines) and lines[index + 1].startswith("+"):
                index += 1  # the offending line echoed by git
        index += 1
    return findings


def _safe_target(worktree: Path, rel_path: str) -> Path | None:
    """Absolute path of a reported file, or None when it leaves the worktree."""
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return None
    root = worktree.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        return None
    return target


def _run_diff_check(worktree_path: str, base_ref: str) -> subprocess.CompletedProcess:
    """Run ``git diff --check <base_ref>`` over every worktree change.

    A throwaway index (``read-tree <base>`` then ``add -A``) mirrors what the
    verification phase does, so the report also covers brand-new files without
    ever touching the worktree's real index.
    """
    with tempfile.TemporaryDirectory(prefix="whitespace-fix-index-") as index_dir:
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
        read_tree = subprocess.run(
            ["git", "read-tree", base_ref],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
        )
        if read_tree.returncode != 0:
            raise RuntimeError(
                f"git read-tree {base_ref} failed: {read_tree.stderr.strip() or read_tree.stdout.strip()}"
            )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=300,
            check=True,
        )
        return subprocess.run(
            ["git", "diff", "--cached", "--check", base_ref],
            cwd=worktree_path, capture_output=True, text=True, env=env, timeout=120,
        )


def _report_of(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.strip() or (proc.returncode != 0 and proc.stderr.strip()) or ""


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


def _fix_file(target: Path, rel_path: str, findings: list[tuple[int, str]]) -> int:
    """Rewrite only the reported lines of one file; returns the lines changed."""
    raw = target.read_bytes()
    if b"\0" in raw:
        return 0  # binary file: never touched
    text = raw.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)
    is_markdown = rel_path.lower().endswith(".md")
    wants_eof_trim = any(BLANK_LINE_AT_EOF in message for _, message in findings)
    changed = 0
    # Highest line numbers first, so edits cannot shift a later index.
    for line_no, message in sorted(set(findings), key=lambda item: item[0], reverse=True):
        if CONFLICT_MARKER in message:
            continue  # conflict markers are never auto-fixed
        if BLANK_LINE_AT_EOF in message:
            continue  # handled once per file below
        index = line_no - 1
        if index < 0 or index >= len(lines):
            continue
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if is_markdown and _is_markdown_hard_break(body):
            continue  # two trailing spaces in Markdown are a hard break, not dirt
        new_body = body
        if SPACE_BEFORE_TAB in message:
            new_body = _fix_space_before_tab(new_body)
        if TRAILING_WHITESPACE in message or SPACE_BEFORE_TAB in message:
            new_body = new_body.rstrip(" \t")
        if new_body != body:
            lines[index] = new_body + ending
            changed += 1
    if wants_eof_trim:
        # git reports only the first blank line at EOF; remove the extra ones too.
        end = len(lines)
        while end > 0 and not lines[end - 1].strip(" \t\r\n"):
            end -= 1
        if end < len(lines):
            changed += len(lines) - end
            lines = lines[:end]
    if changed:
        target.write_bytes("".join(lines).encode("utf-8", errors="surrogateescape"))
    return changed


def fix_whitespace(worktree_path: str, base_ref: str = "HEAD") -> WhitespaceFixResult:
    """Fix the whitespace findings of ``git diff --check <base_ref>`` in the worktree.

    Parses the check output, rewrites only the reported lines of each reported
    text file inside the worktree, then re-runs the check: the returned
    ``remaining_report`` is what still stands afterwards (conflict markers,
    unfixable or skipped findings). Raises when the git plumbing itself fails -
    callers then keep the original report.
    """
    worktree = Path(worktree_path)
    report = _report_of(_run_diff_check(worktree_path, base_ref))
    findings = parse_diff_check_report(report)
    fixed_files: dict[str, int] = {}
    for rel_path, file_findings in findings.items():
        target = _safe_target(worktree, rel_path)
        if target is None or not target.is_file():
            continue  # outside the worktree or gone: leave it to the report
        try:
            changed = _fix_file(target, rel_path, file_findings)
        except OSError:
            continue
        if changed:
            fixed_files[rel_path] = changed
    if not fixed_files:
        return WhitespaceFixResult({}, report)
    remaining = _report_of(_run_diff_check(worktree_path, base_ref))
    return WhitespaceFixResult(fixed_files, remaining)
