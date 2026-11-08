"""Free-form tasks get a planned GitHub issue (#294).

The local controller is good at steering and calling tools, weak at writing
plans. When a free-form task (``update ubuntu``, not ``solve github issue
#N``) is going to change something, one call to an external planner
(Claude or Codex, the ``planner`` role) turns the task and the facts Nexus
already collected into a proper issue: title, goal, current state, plan and
acceptance criteria. That issue replaces the ``Mailbox task <id>``
placeholder, so every change hangs on an issue a strong model wrote.

Questions and read-only lookups never reach the planner. When no external
provider is available or none returns a usable issue, the caller falls back
to the placeholder issue: planning is an improvement, never a blocker.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from modules.nexus_planner import _json_object
from modules.redaction import redact_outbound

logger = logging.getLogger(__name__)

ROLE_PLANNER = "planner"
ENV_TASK_PLANNER = "NEXUS_TASK_PLANNER"
WORK_CODE = "code"
WORK_MACHINE = "machine"

_PLANNER_TIMEOUT_SECONDS = 600
_PLANNER_STALL_SECONDS = 300
_TITLE_CHARS = 120
_SECTION_CHARS = 4000
_TASK_CHARS = 8000
_FACTS_CHARS = 12000
_ITEMS = 15
_ITEM_CHARS = 500


@dataclass(frozen=True, slots=True)
class IssueDraft:
    """A planned issue: what the planner intends, ready to open on GitHub."""

    title: str
    body: str
    planner: str = ""


def planner_prompt(task: str, facts: str = "", *, work: str = WORK_CODE, command: str = "") -> str:
    """The one prompt the external planner gets for a free-form task."""
    if work == WORK_MACHINE:
        kind = (
            "This is MACHINE WORK on Nexus' Ubuntu host, done with shell commands, "
            "not a code change. Nexus intends to run:\n\n"
            f"    {command.strip()}\n\n"
            "Plan the machine work; say what must be checked before and after."
        )
    else:
        kind = (
            "This is a CODE CHANGE in this repository. A coding agent will implement "
            "the issue on its own branch and open a pull request. Name the files, "
            "modules or tests that are involved when the facts show them."
        )
    facts_text = (facts or "").strip()[:_FACTS_CHARS] or "(Nexus collected no facts.)"
    return (
        "You are the planner for Nexus, an autonomous agent. Nexus' local controller "
        "model got the free-form task below. It will follow your issue step by step, "
        "so write a plan a small model can follow without interpreting.\n\n"
        f"{kind}\n\n"
        "Write one GitHub issue for the task. Do not change anything yourself; you "
        "may read files to check facts. Keep it short and concrete. Do not invent "
        "facts: what is unknown goes into the current state as unknown.\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"title": "<imperative, at most 100 characters>", '
        '"goal": "<what is true when the work is done>", '
        '"current_state": "<what is known now, from the facts>", '
        '"plan": ["<step>", "..."], '
        '"acceptance_criteria": ["<checkable criterion>", "..."]}\n\n'
        "TASK:\n\n" + (task or "").strip()[:_TASK_CHARS] + "\n\n"
        "FACTS NEXUS COLLECTED (read-only tools, may be incomplete):\n\n" + facts_text
    )


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"\n+", value)]
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = _line(item, _ITEM_CHARS)
        text = re.sub(r"^(?:[-*]\s+|\d+[.)]\s+|\[[ x]\]\s+)", "", text).strip()
        if text:
            items.append(text)
    return items[:_ITEMS]


def parse_issue_draft(raw: str, task: str, *, planner: str = "") -> IssueDraft | None:
    """The planner's answer as an issue, or ``None`` when it is not usable.

    Title, goal, a plan and acceptance criteria are required; the original
    task is always kept at the end so nothing the requester said is lost.
    """
    data = _json_object(raw or "")
    if data is None:
        return None
    title = _line(data.get("title"), _TITLE_CHARS)
    goal = str(data.get("goal") or "").strip()[:_SECTION_CHARS]
    state = str(data.get("current_state") or "").strip()[:_SECTION_CHARS]
    plan = _items(data.get("plan"))
    criteria = _items(data.get("acceptance_criteria"))
    if not title or not goal or not plan or not criteria:
        return None
    sections = [
        "## Goal\n\n" + goal,
        "## Current state\n\n" + (state or "Unknown."),
        "## Plan\n\n" + "\n".join(f"{n}. {step}" for n, step in enumerate(plan, 1)),
        "## Acceptance criteria\n\n" + "\n".join(f"- [ ] {c}" for c in criteria),
        "## Original task\n\n" + (task or "").strip()[:_TASK_CHARS],
        "---\nPlanned by " + (planner or "an external planner")
        + " for Nexus from a free-form task (#294).",
    ]
    return IssueDraft(
        title=redact_outbound(title),
        body=redact_outbound("\n\n".join(sections)),
        planner=planner,
    )


def _run_cli(provider: str, prompt: str, working_dir: str) -> str:
    from modules.claude_worker import ClaudeWorker
    from modules.codex_worker import CodexWorker

    worker = ClaudeWorker() if provider == "claude" else CodexWorker()
    result = worker.run(
        task=prompt, working_dir=working_dir, sandbox="read-only",
        timeout_seconds=_PLANNER_TIMEOUT_SECONDS, stall_seconds=_PLANNER_STALL_SECONDS,
        phase=ROLE_PLANNER,
    )
    if not result.success:
        raise RuntimeError(result.error or f"{provider} planning failed")
    return result.final_message or ""


def _default_runners(working_dir: str) -> dict[str, Callable[[str], str]]:
    """The external providers whose CLI is installed and allowed, in order."""
    from modules.coding_provider import available_providers, pinned_provider

    providers = available_providers()
    pinned = pinned_provider()
    if pinned in providers:
        providers = [pinned] + [p for p in providers if p != pinned]
    return {p: (lambda prompt, p=p: _run_cli(p, prompt, working_dir)) for p in providers}


def planner_enabled() -> bool:
    """``NEXUS_TASK_PLANNER=off`` keeps the placeholder issue."""
    return os.environ.get(ENV_TASK_PLANNER, "").strip().lower() not in {"off", "0", "false", "no"}


class TaskIssuePlanner:
    """Ask one external planner to write the issue for a free-form task.

    *runners* map a provider name to ``prompt -> raw answer``; the default is
    the installed Claude/Codex CLIs in read-only mode. The local model is never
    a runner: the planner role is always external. Providers are tried in
    order until one gives a usable issue, so a task costs one successful call.
    """

    def __init__(
        self,
        runners: dict[str, Callable[[str], str]] | None = None,
        *,
        working_dir: str = "/agent",
    ) -> None:
        self._runners = runners
        self._working_dir = working_dir

    def draft(
        self, task: str, facts: str = "", *, work: str = WORK_CODE, command: str = ""
    ) -> IssueDraft | None:
        if not (task or "").strip() or not planner_enabled():
            return None
        runners = self._runners if self._runners is not None else _default_runners(self._working_dir)
        if not runners:
            logger.info("task planner: no external provider available; using the placeholder issue")
            return None
        prompt = planner_prompt(task, facts, work=work, command=command)
        for name, runner in runners.items():
            try:
                raw = runner(prompt)
            except Exception as exc:  # noqa: BLE001 - try the next provider
                logger.warning("task planner: %s failed: %s", name, exc)
                continue
            draft = parse_issue_draft(raw, task, planner=name)
            if draft is not None:
                return draft
            logger.warning("task planner: %s gave no usable issue: %r", name, (raw or "")[:300])
        return None


class LazyIssueDraft:
    """Plan at most once, and only when a new issue is really opened.

    Passed to ``GitHubWorkTrackingService.ensure_for_source`` as the source's
    ``issue_draft``: a retried task that already has its issue, or a task that
    names an existing issue, never calls the planner.
    """

    def __init__(self, plan: Callable[[], IssueDraft | None]) -> None:
        self._plan = plan
        self.called = False
        self.result: IssueDraft | None = None

    def __call__(self) -> IssueDraft | None:
        if not self.called:
            self.called = True
            try:
                self.result = self._plan()
            except Exception as exc:  # noqa: BLE001 - fall back to the placeholder
                logger.warning("task planner: planning failed: %s", exc)
                self.result = None
        return self.result


def planned_goal(issue_number: int, draft: IssueDraft) -> str:
    """The experiment goal for a planned issue: the issue first, like ``solve #N``."""
    return f"GitHub Issue #{issue_number}: {draft.title}\n\n{draft.body}".strip()


# Commands whose first word only reads. Anything else, any redirection into
# a file, or ``sudo`` counts as changing something: a wrong "changes" costs
# one issue, a wrong "read-only" leaves a change untracked.
_READ_ONLY_PROGRAMS = frozenset({
    "cat", "head", "tail", "less", "grep", "rg", "egrep", "ls", "find", "stat", "wc",
    "du", "df", "free", "uptime", "uname", "whoami", "id", "hostname", "date", "ps",
    "top", "pgrep", "lsblk", "lscpu", "nproc", "printenv", "echo", "which",
    "type", "file", "journalctl", "dmesg", "ss", "netstat", "ip", "ping", "dig",
    "nslookup", "sort", "uniq", "cut", "jq", "tr", "true", "pwd",
    "vmstat", "iostat", "lsof", "sha256sum", "md5sum", "dpkg-query", "lsb_release",
    "realpath", "basename", "dirname", "test", "[",
})
_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "rev-parse", "worktree", "remote", "ls-files", "blame"},
    "systemctl": {"status", "is-active", "is-enabled", "list-units", "list-timers", "show", "cat"},
    "apt": {"list", "show", "policy", "search"},
    "apt-cache": {"policy", "show", "search", "depends"},
    "dpkg": {"-l", "-s", "-L", "--list", "--status"},
    "docker": {"ps", "images", "logs", "inspect", "stats"},
    "gh": {"issue", "pr", "run", "api"},
    "python3": {"--version"},
    "python": {"--version"},
}
_REDIRECT_RE = re.compile(r"(?<![0-9&])>{1,2}(?!\s*&|\s*/dev/null)|\btee\b")
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;\n]")


def command_changes_something(command: str) -> bool:
    """Whether a controller ``command`` changes the machine (#294).

    Conservative: only commands made entirely of known read-only programs
    count as a lookup. ``sed -i``, ``apt upgrade``, ``systemctl restart`` and
    anything unknown count as a change.
    """
    text = (command or "").strip()
    if not text:
        return False
    if _REDIRECT_RE.search(text) or "$(" in text or "`" in text:
        return True
    for segment in _SEGMENT_SPLIT_RE.split(text):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return True
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        program = os.path.basename(words[0])
        if program in _READ_ONLY_PROGRAMS:
            if program == "find" and any(w in {"-delete", "-exec", "-execdir"} for w in words):
                return True
            continue
        subcommands = _READ_ONLY_SUBCOMMANDS.get(program)
        if subcommands and len(words) > 1 and words[1] in subcommands:
            if program == "gh" and any(w in {"create", "edit", "close", "merge", "comment", "-X", "--method"} for w in words[2:]):
                return True
            continue
        return True
    return False


def machine_result_comment(command: str, result: str, *, succeeded: bool) -> str:
    """The comment that reports machine work back on its issue."""
    outcome = "succeeded" if succeeded else "failed"
    return redact_outbound(
        f"Nexus ran the machine work for this issue; it {outcome}.\n\n"
        f"Command:\n\n```\n{command.strip()[:2000]}\n```\n\n"
        f"Result:\n\n{(result or '').strip()[:20000]}"
    )


def open_machine_issue(
    tracking: Any,
    planner: Any,
    *,
    task: str,
    facts: str,
    command: str,
    placeholder_title: str,
) -> tuple[int | None, IssueDraft | None]:
    """Open the issue machine work hangs on, before the command runs (#294).

    Only for commands that change something. Without a usable planner answer
    the issue is a placeholder with the raw task. Best effort: GitHub being
    unreachable never stops the machine work itself.
    """
    if planner is None or not command_changes_something(command):
        return None, None
    if not getattr(tracking, "configured", False):
        return None, None
    draft = None
    try:
        draft = planner.draft(task, facts, work=WORK_MACHINE, command=command)
    except Exception as exc:  # noqa: BLE001 - fall back to the placeholder
        logger.warning("task planner: machine planning failed: %s", exc)
    issue_draft = draft or IssueDraft(
        title=placeholder_title,
        body=redact_outbound(
            "Created automatically by Nexus before machine work so it is traceable in GitHub.\n\n"
            f"## Nexus task\n\n{(task or '').strip()[:_TASK_CHARS]}\n\n"
            f"## Command\n\n```\n{command.strip()[:2000]}\n```"
        ),
    )
    try:
        issue = tracking.open_planned_issue(issue_draft)
        return int(issue["number"]), draft
    except Exception as exc:  # noqa: BLE001 - tracking is best effort here
        logger.warning("could not open the machine-work issue: %s", exc)
        return None, draft


def report_machine_result(
    tracking: Any, issue_number: int | None, command: str, result: str, *, succeeded: bool
) -> bool:
    """Comment the result on the machine-work issue; close it when it worked."""
    if not issue_number:
        return False
    try:
        tracking.comment(issue_number, machine_result_comment(command, result, succeeded=succeeded))
        if succeeded:
            tracking.close_issue(issue_number)
        return True
    except Exception as exc:  # noqa: BLE001 - the work itself already happened
        logger.warning("could not report machine work on issue #%s: %s", issue_number, exc)
        return False


__all__ = [
    "open_machine_issue",
    "report_machine_result",
    "ENV_TASK_PLANNER",
    "IssueDraft",
    "LazyIssueDraft",
    "ROLE_PLANNER",
    "TaskIssuePlanner",
    "WORK_CODE",
    "WORK_MACHINE",
    "command_changes_something",
    "machine_result_comment",
    "parse_issue_draft",
    "planned_goal",
    "planner_enabled",
    "planner_prompt",
]
