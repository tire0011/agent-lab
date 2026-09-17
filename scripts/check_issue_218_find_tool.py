#!/usr/bin/env python3
"""Manual #218 check: drive the real local controller over the failing task.

The regression suite scripts llama-server's responses, so it proves the
orchestrator's behaviour but says nothing about what the real local model
decides. This runs the genuine path - real `NexusTaskOrchestrator`, real
`OperationsRegistry`, real llama-server - over the task text that produced
session ``1ca2d781836a481d8152744f5c1ff94d`` and reports whether the failure
signature is back.

The failure signature is:

* ``find_tool`` resolved more than once for the identical goal, or
* the task ended because the loop guard stopped it.

Either one exits 1. A run that never reached ``find_tool`` at all - because
llama-server was unreachable, or because the controller went straight to a
final action - exits 2: it verified nothing, and reporting that as a pass
would be the same mistake as trusting a test that never ran.

This is a real controller turn, so the controller may run read-only
operations and shell commands exactly as it would for any task. It writes to
a scratch database (``--db-path``) so production session state is untouched,
but the repository it inspects is the real one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Support direct execution via /agent/scripts/check_issue_218_find_tool.py as
# well as `python -m scripts.check_issue_218_find_tool` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.operations_registry import OperationsRegistry
from modules.task_orchestrator import ACTION_CLARIFICATION, NexusTaskOrchestrator

DEFAULT_TASK = "fix github issue #196"
LOOP_GUARD_EVENTS = {"duplicate_tool_call", "non_progressing_cycle", "retry_reason_required"}


def _experiment_service():
    """Resolves nothing, like a task with no experiment behind it.

    The check is about tool discovery, and a service that answers every lookup
    would let a commit sha in the task text masquerade as an experiment.
    """
    return SimpleNamespace(
        resolve_experiment=lambda experiment_id: (_ for _ in ()).throw(
            KeyError(f"unknown experiment {experiment_id!r}")
        )
    )


def _describe(observation: dict) -> str:
    tool = str(observation.get("tool", observation.get("event", "?")))
    if observation.get("event"):
        return f"{tool}: {observation['event']}  <- loop guard"
    if tool != "find_tool":
        return tool
    status = observation.get("status", "?")
    line = f"find_tool({observation.get('goal', '')!r}) -> {status}"
    if status == "found":
        return f"{line} [{observation.get('resolved_tool')}]"
    related = [
        item.get("operation") if isinstance(item, dict) else item
        for item in observation.get("related_operations") or []
    ]
    metrics = observation.get("metrics") or {}
    # The reason is the only thing that separates "the resolver said none of
    # these fit" from "the resolver's answer could not be used at all", and
    # every status but `found` carries one. Printing it is not optional: a
    # trace without it can say a lookup failed but never why.
    return (
        f"{line}\n"
        f"      reason:                  {observation.get('reason')}\n"
        f"      resolver_provider:       {metrics.get('resolver_provider', '-')}\n"
        f"      selected_operation:      {metrics.get('selected_operation', '-')}\n"
        f"      candidates offered:      {metrics.get('candidate_operations', [])}\n"
        f"      recommended_next_action: {observation.get('recommended_next_action')}\n"
        f"      recommended_operation:   {observation.get('recommended_operation', '-')}\n"
        f"      direct_capabilities:     {observation.get('direct_capabilities')}\n"
        f"      related_operations:      {related}"
    )


def _resolver_only(args) -> int:
    """One find_tool lookup against the real resolver, and what it answered.

    A whole controller run costs several minutes per turn on the VM, and most
    questions about the resolver ("can it resolve this goal at all?") need only
    this. Prints the raw model answer too, because when a response cannot be
    used, what it actually said is the only thing that explains why.
    """
    import asyncio

    registry = OperationsRegistry(
        experiment_service=_experiment_service(),
        db_path=str(args.db_path),
        repo_root=str(args.repo_root),
    )
    orchestrator = NexusTaskOrchestrator(
        operations_registry=registry,
        experiment_service=_experiment_service(),
    )
    print("#218 resolver probe")
    print(f"goal:     {args.resolver_only!r}")
    print(f"database: {args.db_path} (scratch)")
    print()
    try:
        session = orchestrator.task_store.get_or_create(
            source_type="task", source_id="issue-218-resolver-probe"
        )
        observation = asyncio.run(
            orchestrator._resolve_find_tool(args.resolver_only, session_id=session.id)
        )
    finally:
        orchestrator.close()

    print(_describe(observation))
    raw = observation.get("resolver_raw")
    if raw:
        print()
        print("what the model actually answered:")
        print(f"  {raw}")
    if args.json:
        print()
        print(json.dumps(observation, indent=2, sort_keys=True))

    status = observation.get("status")
    print()
    if status == "found":
        print(f"PASS: resolved to {observation.get('resolved_tool')}")
        return 0
    if status == "not_found":
        print("not_found: the resolver considered the candidates and rejected them")
        return 0
    print(f"FAIL: the resolver produced no usable answer (status={status})")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", default=DEFAULT_TASK, help="task text to drive the controller with")
    parser.add_argument("--db-path", type=Path, default=Path("/tmp/nexus-issue-218-check.db"))
    parser.add_argument("--repo-root", type=Path, default=Path("/agent"))
    parser.add_argument("--max-steps", type=int, default=4, help="controller turns before it must finish")
    parser.add_argument("--json", action="store_true", help="also dump the raw observations")
    parser.add_argument(
        "--resolver-only",
        metavar="GOAL",
        help=(
            "ask the tool resolver this one goal and print what came back, then stop. "
            "One model call instead of a whole controller run - seconds, not minutes."
        ),
    )
    args = parser.parse_args()

    if args.db_path.exists():
        args.db_path.unlink()

    registry = OperationsRegistry(
        experiment_service=_experiment_service(),
        db_path=str(args.db_path),
        repo_root=str(args.repo_root),
    )
    orchestrator = NexusTaskOrchestrator(
        operations_registry=registry,
        experiment_service=_experiment_service(),
        max_steps=args.max_steps,
    )

    if args.resolver_only:
        return _resolver_only(args)

    print("#218 manual check")
    print(f"task:      {args.task!r}")
    print(f"repo root: {args.repo_root}")
    print(f"database:  {args.db_path} (scratch)")
    print("provider:  local llama-server (the real controller path)")
    print()

    try:
        result = orchestrator.run(
            args.task,
            source_type="task",
            source_id="issue-218-manual-check",
        )
        redirects = orchestrator.task_store.recent_events(
            result.session_id, kind="find_tool_redirected"
        )
    finally:
        orchestrator.close()

    print("decision trace")
    for index, observation in enumerate(result.observations, start=1):
        print(f"  {index}. {_describe(observation)}")
    print()

    for event in redirects:
        metadata = event.get("metadata") or {}
        print(
            "redirect: "
            f"{metadata.get('goal')!r} -> {metadata.get('operation')} "
            f"({metadata.get('next_action')})"
        )
    print(f"final action: {result.action}")
    print(f"reason:       {result.reason}")
    print()

    resolved_goals = [
        str(observation.get("goal", ""))
        for observation in result.observations
        if observation.get("tool") == "find_tool" and not observation.get("event")
    ]
    repeated = sorted({goal for goal in resolved_goals if resolved_goals.count(goal) > 1})
    guard_events = sorted(
        {
            str(observation.get("event"))
            for observation in result.observations
            if str(observation.get("event", "")) in LOOP_GUARD_EVENTS
        }
    )
    ended_on_guard = result.action == ACTION_CLARIFICATION and "tool loop limit" in result.reason

    if args.json:
        print(json.dumps(result.observations, indent=2, sort_keys=True))
        print()

    failures = []
    if repeated:
        failures.append(f"identical find_tool goal resolved more than once: {repeated}")
    if ended_on_guard:
        failures.append("the loop guard is what ended the task")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    # A run that never got as far as find_tool proves nothing about the path
    # this check exists to exercise, and must not be reported as a pass.
    if not resolved_goals:
        print("INCONCLUSIVE: the controller never reached find_tool, so nothing was verified")
        if "local inference failed" in result.reason:
            print("  local inference failed - is llama-server running?")
            print("    systemctl status llama-server --no-pager")
            print("    journalctl -u llama-server -n 50 --no-pager")
        else:
            print(f"  it went straight to {result.action}: {result.reason}")
            print("  try a larger --max-steps, or a --task that needs a capability lookup")
        return 2

    if guard_events:
        # Refused but recovered from: worth seeing, not a failure by itself.
        print(f"note: loop guard refused a repeat ({', '.join(guard_events)}) but did not end the task")
    print("PASS: the #218 failure signature did not reappear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
