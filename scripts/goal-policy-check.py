#!/usr/bin/env python3
"""Print the goal-policy verdict for a GitHub issue (#398).

    scripts/goal-policy-check.py 398
    scripts/goal-policy-check.py 398 --save accept

Fetches the issue with ``gh issue view``, builds the goal text the way an
experiment sees it (``solve github issue #N`` plus title and body), and prints
the verdict of ``_check_goal_policy`` (rejection) and ``_goal_secret_warning``
(secret-action warning) with the matched span in context.

``--save accept|reject`` writes the goal text to
``tests/fixtures/goal_policy/<folder>/issue_<N>.md``, so a false positive joins
the regression set of ``tests/test_goal_policy_corpus.py`` in one step.

Exit code: 0 when the goal passes both checks, 1 when it is rejected or
warned, 2 when the issue cannot be fetched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Support direct execution via scripts/goal-policy-check.py from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.experiment_service.policy import (  # noqa: E402
    _check_goal_policy,
    _goal_secret_match,
    _goal_secret_warning,
    _policy_rule_for_reason,
)
from modules.nexus_sources import issue_source  # noqa: E402

FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "goal_policy"
CONTEXT_CHARS = 80


def goal_text(issue_number: int, title: str, body: str) -> str:
    source = issue_source(issue_number, title=title, body=body)
    return f"solve github issue #{issue_number}\n\n{source.content}\n"


def fetch_issue(issue_number: int, repo: str | None) -> tuple[str, str]:
    cmd = ["gh", "issue", "view", str(issue_number), "--json", "title,body"]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, check=False, cwd=_REPO_ROOT
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited with {result.returncode}")
    data = json.loads(result.stdout)
    return str(data.get("title") or ""), str(data.get("body") or "")


def report(goal: str) -> int:
    """Print the verdict; return 0 when the goal passes, else 1."""
    rejection = _check_goal_policy(goal)
    warning = _goal_secret_warning(goal)
    if rejection is None and warning is None:
        print("verdict: pass")
        return 0
    for kind, reason in (("rejected", rejection), ("warning", warning)):
        if reason is not None:
            print(f"verdict: {kind} ({_policy_rule_for_reason(reason)}): {reason}")
    match = _goal_secret_match(goal)
    if match is not None:
        lowered = goal.lower()
        before = lowered[max(0, match.start() - CONTEXT_CHARS):match.start()]
        after = lowered[match.end():match.end() + CONTEXT_CHARS]
        print(f"span: {match.start()}-{match.end()} {match.group(0)!r}")
        print(f"context: ...{before}[[{match.group(0)}]]{after}...")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("issue", type=int, help="GitHub issue number")
    parser.add_argument("--repo", help="owner/name; default: the gh default repository")
    parser.add_argument(
        "--save", choices=("accept", "reject"),
        help="write the goal text to the regression corpus folder",
    )
    args = parser.parse_args(argv)

    try:
        title, body = fetch_issue(args.issue, args.repo)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"error: cannot fetch issue #{args.issue}: {exc}", file=sys.stderr)
        return 2
    goal = goal_text(args.issue, title, body)
    print(f"issue #{args.issue}: {title}")
    status = report(goal)
    if args.save:
        path = FIXTURES / args.save / f"issue_{args.issue}.md"
        path.write_text(goal, encoding="utf-8")
        print(f"saved: {path.relative_to(_REPO_ROOT)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
