"""Explicit work-tracking constraints parsed out of a Nexus task instruction.

Issue #184: a mailbox task said "implement GitHub issue #180 ... do not
create another branch or issue". Nexus ignored both constraints and opened a
duplicate tracking Issue (#181), so one piece of work had two GitHub records
and the governance instruction was silently violated.

This module turns that free-form instruction into a small, inspectable value
object. It never talks to GitHub: deciding *whether* a named Issue is usable
belongs to ``modules.github_work_tracking``; deciding *what the author asked
for* belongs here.

Detection is deliberately conservative. A passing mention ("see issue #12 for
background") must not hijack the lifecycle, so an Issue only becomes the work
target when a directive verb points at it ("implement issue #180") - or when
the task forbids a new Issue and names exactly one, which leaves no other
reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["WorkDirectives", "parse_work_directives"]

_MAX_SCAN_CHARS = 32_000

# ``issue #180``, ``issue 180``, ``Issue: 180``, ``.../issues/180``.
_ISSUE_REF = (
    r"(?:(?:github\s+)?issues?\b[\s:#-]*#?"
    r"|github\.com/[^/\s]+/[^/\s]+/issues/)(\d{1,7})"
)
_TARGET_VERBS = (
    r"(?:implement|implementing|reuse|re-use|use|using|reopen|work\s+on|continue|"
    r"fix|fixes|resolve|resolves|address|close|closes|complete|finish|solve|"
    r"tackle|handle|attach\s+to|link\s+to|for)"
)

_ISSUE_TARGET_RE = re.compile(
    rf"\b{_TARGET_VERBS}\b[^.\n]{{0,40}}?{_ISSUE_REF}", re.IGNORECASE
)
_ISSUE_MENTION_RE = re.compile(_ISSUE_REF, re.IGNORECASE)
_ISSUE_HEADER_RE = re.compile(
    r"\A(?:[ \t]*(?:\r?\n|\r))*[ \t]*GitHub Issue #(\d{1,7})[ \t]*:",
    re.IGNORECASE,
)

_NEGATION = r"(?:do\s+not|do\s*n[o']t|don'?t|dont|never|no)"
_CREATE = r"(?:create|creating|open|opening|file|filing|raise|raising|make|making)"
_NEWNESS = (
    r"(?:new|another|second|additional|further|extra|duplicate|separate|other|tracking)"
)
_ARTICLE = r"(?:a|an|any|the|yet\s+another)\s+"
_KINDS = r"(?:github\s+|tracking\s+)*"


def _no_new_patterns(noun: str) -> tuple[re.Pattern[str], ...]:
    """Patterns for "do not create another <noun>" in its common phrasings.

    A "newness" word (new/another/...) or an immediately following noun is
    required so that an unrelated instruction such as "do not create a PR
    that closes the issue" is not read as a ban on tracking Issues.
    """
    return (
        re.compile(
            rf"\b{_NEGATION}\b[^.\n]{{0,40}}?\b{_CREATE}\b[^.\n]{{0,40}}?"
            rf"\b{_NEWNESS}\b[^.\n]{{0,30}}?\b{noun}s?\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b{_NEGATION}\b[^.\n]{{0,40}}?\b{_CREATE}\s+(?:{_ARTICLE})?{_KINDS}{noun}s?\b",
            re.IGNORECASE,
        ),
        re.compile(rf"\bno\s+{_NEWNESS}\s+{_KINDS}{noun}s?\b", re.IGNORECASE),
    )


_NO_NEW_ISSUE_RES = _no_new_patterns("issue")
_NO_NEW_BRANCH_RES = _no_new_patterns("branch")
# Deliberately broad, also for a quoted mention: a wrong match leaves a pull
# request open for a human, a missed one is an irreversible merge (#185).
_NO_MERGE_RE = re.compile(
    r"\b(?:do\s+not|don['\u2019]t|must\s+not|should\s+not|never)\s+"
    r"(?:auto(?:matically)?[\s-]*)?merge\b"
    r"|\bwithout\s+merging\b"
    r"|\bstop\s+before\s+(?:the\s+|any\s+)?merg(?:e|ing)\b"
    r"|\bleave\s+(?:the\s+|this\s+)?(?:pull\s+request|pr)\s+(?:open|unmerged)\b"
    r"|\bnicht\s+(?:automatisch\s+)?(?:mergen|zusammenf(?:\u00fc|ue)hren)\b",
    re.IGNORECASE,
)

_BRANCH_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}"
_BRANCH_TARGET_RES = (
    re.compile(
        r"\b(?:use|using|reuse|re-use|on|onto|push\s+to|commit\s+to|continue\s+on|"
        r"work\s+on|checkout|check\s+out|stay\s+on)\b[^.\n]{0,40}?"
        rf"\bbranch\b[\s:=]*[`'\"]?({_BRANCH_TOKEN})",
        re.IGNORECASE,
    ),
    re.compile(rf"\bbranch\b\s*[:=]\s*[`'\"]?({_BRANCH_TOKEN})", re.IGNORECASE),
)

# A branch name that carries no separator and no digit is almost always an
# ordinary English word swallowed from a sentence ("use the branch that
# already exists"), not a ref. These few are real branch names all the same.
_PLAIN_BRANCH_NAMES = frozenset({"main", "master", "develop", "development", "trunk"})


@dataclass(frozen=True, slots=True)
class WorkDirectives:
    """What a task instruction explicitly demands of GitHub work tracking."""

    issue_number: int | None = None
    header_issue_number: int | None = None
    mentioned_issue_numbers: tuple[int, ...] = ()
    forbid_new_issue: bool = False
    branch_name: str | None = None
    forbid_new_branch: bool = False
    forbid_merge: bool = False
    merge_constraint_reason: str | None = None



def parse_work_directives(text: str | None) -> WorkDirectives:
    """Extract Issue/branch reuse constraints from a free-form instruction."""
    source = (text or "")[:_MAX_SCAN_CHARS]
    if not source.strip():
        return WorkDirectives()

    targets = _unique_numbers(_ISSUE_TARGET_RE.findall(source))
    mentions = _unique_numbers(_ISSUE_MENTION_RE.findall(source))
    forbid_new_issue = any(pattern.search(source) for pattern in _NO_NEW_ISSUE_RES)
    header_match = _ISSUE_HEADER_RE.match(source)
    header_issue_number = int(header_match.group(1)) if header_match else None

    if header_issue_number is not None and all(
        target == header_issue_number for target in targets
    ):
        issue_number: int | None = header_issue_number
    elif header_issue_number is not None:
        # A verb-led target is more explicit than the standard goal heading,
        # but conflicting numbers remain ambiguous rather than silently
        # choosing either Issue.
        issue_number = None
    elif len(targets) == 1:
        issue_number: int | None = targets[0]
    elif not targets and forbid_new_issue and len(mentions) == 1:
        # "Do not create another issue" plus exactly one Issue in the task
        # leaves only one issue it can mean.
        issue_number = mentions[0]
    else:
        # No target, or several competing ones: the caller decides whether
        # that is a plain "no directive" or an ambiguity it must refuse.
        issue_number = None

    merge_match = _NO_MERGE_RE.search(source)
    return WorkDirectives(
        issue_number=issue_number,
        header_issue_number=header_issue_number,
        mentioned_issue_numbers=mentions,
        forbid_new_issue=forbid_new_issue,
        branch_name=_find_branch(source),
        forbid_new_branch=any(pattern.search(source) for pattern in _NO_NEW_BRANCH_RES),
        forbid_merge=merge_match is not None,
        merge_constraint_reason=(
            f"explicit task constraint: {merge_match.group(0)}" if merge_match else None
        ),
    )


def _unique_numbers(raw: list[str]) -> tuple[int, ...]:
    seen: list[int] = []
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.append(number)
    return tuple(seen)


def _find_branch(source: str) -> str | None:
    for pattern in _BRANCH_TARGET_RES:
        for match in pattern.finditer(source):
            candidate = _clean_branch(match.group(1))
            if candidate:
                return candidate
    return None


def _clean_branch(raw: str) -> str | None:
    name = (raw or "").strip().strip("`'\"").rstrip(".,;:)!?")
    if not name or len(name) > 100:
        return None
    if ".." in name or name.startswith("/") or name.endswith("/") or name.endswith(".lock"):
        return None
    if name.lower() in _PLAIN_BRANCH_NAMES:
        return name
    if not re.search(r"[/_-]|\d", name):
        return None
    return name
