#!/usr/bin/env python3
"""Manual #223 check: does the controller reuse a successful github_issue result?

Session ``3e2e565e49134703934e59bd57ad9118`` fetched GitHub issue #196
successfully and then asked for the identical ``github_issue(196)`` operation
again. The successful observation was in the prompt, but wrapped in the whole
execution envelope - tool wrapper, grant id, operation schema, nested result -
so for a 7B local controller the issue content it had already obtained was
buried in orchestration metadata.

The unit tests script llama-server's answers, which proves what the
orchestrator does with a given decision but says nothing about what the real
model decides. This runs the genuine path - real ``NexusTaskOrchestrator``,
real ``OperationsRegistry``, real llama-server - over the same task text and
reports whether the failure signature is back.

The failure signature, all of it measured only once the operation has
succeeded at least once, is:

* ``github_issue`` executed more than once with the same arguments, or
* the controller requesting that identical operation again after it
  succeeded (even when the loop guard refuses the repeat), or
* the task ending because the loop guard stopped it.

Any of those exits 1. A run that never reached a successful
``github_issue(196)`` at all - llama-server unreachable, GitHub token missing,
or the controller going straight to a final action - exits 2: it verified
nothing, and reporting that as a pass would be the same mistake as trusting a
test that never ran.

This is a real controller turn, so the controller may run read-only operations
and shell commands exactly as it would for any task. It writes to a scratch
database (``--db-path``) so production session state is untouched, but the
repository it inspects is the real one.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Support direct execution via /agent/scripts/check_issue_196_controller_progress.py
# as well as `python -m scripts.check_issue_196_controller_progress` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# task_orchestrator first: importing operations_registry before it trips the
# registry <-> tool_router import cycle when this module is the entry point.
from modules.task_orchestrator import ACTION_CLARIFICATION, NexusTaskOrchestrator  # isort: skip
from modules.operations_registry import OperationsRegistry  # noqa: E402
from modules.redaction import redact_secrets  # noqa: E402

DEFAULT_TASK = "solve github issue #196"
DEFAULT_OPERATION = "github_issue"
DEFAULT_EXPECTED_ARGS = {"issue_number": 196}
LOOP_GUARD_EVENTS = {"duplicate_tool_call", "non_progressing_cycle", "retry_reason_required"}


def _experiment_service():
    """Resolves nothing, like a task with no experiment behind it.

    The check is about reusing a tool result, and a service that answered
    every lookup would let an issue number in the task text masquerade as an
    experiment id.
    """
    return SimpleNamespace(
        resolve_experiment=lambda experiment_id: (_ for _ in ()).throw(
            KeyError(f"unknown experiment {experiment_id!r}")
        )
    )


def _normalized_args(args: Any) -> str:
    """A stable signature for "the same call", independent of key order.

    ``{"issue_number": 196}`` and ``{"issue_number": "196"}`` are the same
    request as far as this check is concerned: the controller asking twice for
    one issue is the regression whether or not it retyped the number as a
    string.
    """
    if not isinstance(args, dict):
        return json.dumps(args, sort_keys=True, default=str)
    normalized = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                value = int(value.strip(), 10)
            except ValueError:
                pass
        normalized[str(key)] = value
    return json.dumps(normalized, sort_keys=True, default=str)


def _is_target_operation(operation: str, args: Any, target: "_Target") -> bool:
    """Is this the call the run is watching?

    Argument matching is a subset test, not equality: a capability may be
    called with extra defaults the check has no opinion about, and requiring
    an exact match would quietly stop recognising the call it exists to watch.
    """
    if operation != target.operation:
        return False
    if not target.expected_args:
        return True
    if not isinstance(args, dict):
        return False
    for key, expected in target.expected_args.items():
        if key not in args:
            return False
        if _coerce(args[key]) != _coerce(expected):
            return False
    return True


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return value.strip()
    return value


@dataclass(frozen=True)
class _Target:
    """The operation this run watches, and how it is named in output."""

    operation: str
    expected_args: dict[str, Any]

    def __str__(self) -> str:
        if not self.expected_args:
            return f"{self.operation}()"
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.expected_args.items()))
        return f"{self.operation}({rendered})"


def _operation_succeeded(observation: dict[str, Any]) -> bool:
    result = observation.get("result")
    if not isinstance(result, dict):
        return False
    return bool(result.get("success"))


def _requested_operation(decision: dict[str, Any]) -> tuple[str, Any] | None:
    """The operation a controller decision asks to execute, if any.

    ``call_tool`` names it directly. A ``find_tool`` goal does not, and is not
    counted here: asking the resolver a question is not the same as calling an
    operation, and the operation it auto-executes shows up as its own
    observation.
    """
    action = str(decision.get("action", "")).strip().lower()
    if action != "call_tool":
        return None
    tool = str(decision.get("tool", "")).strip()
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        arguments = decision.get("args") or {}
    return tool, arguments


def _describe_observation(observation: dict[str, Any]) -> str:
    if observation.get("event"):
        return f"{observation.get('tool', 'guard')}: {observation['event']}  <- loop guard"
    tool = str(observation.get("tool", "?"))
    event_id = observation.get("event_id", "-")
    if tool == "operation":
        operation = observation.get("operation", "?")
        args = json.dumps(observation.get("args") or {}, sort_keys=True, default=str)
        if observation.get("error"):
            return (
                f"operation {operation}{args} -> {observation['error']} "
                f"(event {event_id})\n      reason: {observation.get('reason')}"
            )
        result = observation.get("result") or {}
        status = "succeeded" if result.get("success") else "failed"
        lines = result.get("output_lines") or []
        return (
            f"operation {operation}{args} -> {status} (event {event_id}), "
            f"{len(lines)} output lines"
        )
    if tool == "find_tool":
        return (
            f"find_tool({observation.get('goal', '')!r}) -> {observation.get('status')} "
            f"[{observation.get('resolved_tool', '-')}] (event {event_id})"
        )
    return f"{tool} (event {event_id})"


def _print_prompt_observations(prompts: list[dict[str, Any]]) -> None:
    """What the controller actually read, turn by turn.

    The whole point of #223 is the gap between the stored event and the view
    the model is given, so a diagnostic that prints only the stored event
    cannot show the problem.
    """
    for turn, record in enumerate(prompts, start=1):
        section = record.get("observations_section") or "(no observations yet)"
        print(f"  turn {turn}: prompt {record['prompt_chars']} chars, "
              f"{record['observation_count']} observations, "
              f"omitted {record['omitted']}")
        for line in redact_secrets(section).splitlines():
            print(f"      {line[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help="task text to drive the controller with")
    parser.add_argument(
        "--operation",
        default=DEFAULT_OPERATION,
        help=(
            "the operation whose repetition is the regression. The default is the "
            "one #223 describes; another read-only capability lets the same path be "
            "exercised where github_issue cannot succeed (no GITHUB_TOKEN on the host)."
        ),
    )
    parser.add_argument(
        "--expect-arg",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "only count calls carrying this argument; repeatable. Defaults to "
            "issue_number=196 when --operation is left at its default, and to no "
            "filter otherwise."
        ),
    )
    parser.add_argument("--db-path", type=Path, default=Path("/tmp/nexus-issue-196-check.db"))
    parser.add_argument("--repo-root", type=Path, default=Path("/agent"))
    parser.add_argument("--max-steps", type=int, default=8, help="controller turns before it must finish")
    parser.add_argument("--json", action="store_true", help="also dump the raw observations")
    parser.add_argument(
        "--show-prompts",
        action="store_true",
        help="print the observation section of every controller prompt (redacted)",
    )
    args = parser.parse_args()

    if args.expect_arg:
        expected: dict[str, Any] = {}
        for pair in args.expect_arg:
            key, _, raw = pair.partition("=")
            if not key or not _:
                parser.error(f"--expect-arg must be KEY=VALUE, got {pair!r}")
            expected[key.strip()] = _coerce(raw)
    elif args.operation == DEFAULT_OPERATION:
        expected = dict(DEFAULT_EXPECTED_ARGS)
    else:
        # A substitute capability has no issue number; watching the operation
        # itself is the whole filter.
        expected = {}
    target = _Target(args.operation, expected)

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

    # Every decision the model produced, including ones the loop guard then
    # refused. A repeat that never executes is still the controller failing to
    # use a result it already has, which is exactly what #223 is about - so a
    # check that inspected observations alone would miss it.
    decisions: list[dict[str, Any]] = []
    original_parse = orchestrator._parse

    def _recording_parse(raw: str) -> dict[str, Any]:
        decision = original_parse(raw)
        decisions.append(decision)
        return decision

    orchestrator._parse = _recording_parse  # type: ignore[method-assign]

    prompts: list[dict[str, Any]] = []
    original_build_prompt = orchestrator._build_prompt

    def _recording_build_prompt(session, current_input, history, observations, budget_plan):
        prompt, meta = original_build_prompt(session, current_input, history, observations, budget_plan)
        _, _, tail = prompt.partition("OBSERVATIONS FROM TOOLS USED IN THIS RUN:\n")
        prompts.append(
            {
                "prompt_chars": len(prompt),
                "observation_count": len(observations),
                "omitted": meta.get("omitted_observation_indices"),
                "observations_section": tail.partition("\nDecide the next step now.")[0],
            }
        )
        return prompt, meta

    orchestrator._build_prompt = _recording_build_prompt  # type: ignore[method-assign]

    print("#223 manual check: controller reuse of a successful tool result")
    print(f"task:      {args.task!r}")
    print(f"watching:  {target}")
    print(f"repo root: {args.repo_root}")
    print(f"database:  {args.db_path} (scratch)")
    print("provider:  local llama-server (the real controller path)")
    print()

    try:
        result = orchestrator.run(
            args.task,
            source_type="task",
            source_id="issue-223-manual-check",
        )
    finally:
        orchestrator.close()

    print(f"session id:   {result.session_id}")
    print()

    print("controller decisions")
    for index, decision in enumerate(decisions, start=1):
        action = str(decision.get("action", "?"))
        detail = decision.get("tool") or decision.get("goal") or decision.get("query") or ""
        arguments = decision.get("arguments")
        if isinstance(arguments, dict) and arguments:
            detail = f"{detail}{json.dumps(arguments, sort_keys=True, default=str)}"
        print(f"  {index}. {action} {redact_secrets(str(detail))[:300]}")
        print(f"      reason: {redact_secrets(str(decision.get('reason', '')))[:300]}")
    print()

    print("tool events")
    for index, observation in enumerate(result.observations, start=1):
        print(f"  {index}. {_describe_observation(observation)}")
    print()

    if args.show_prompts:
        print("compact observations given to the controller")
        _print_prompt_observations(prompts)
        print()

    print(f"final action: {result.action}")
    print(f"reason:       {redact_secrets(result.reason)}")
    print()

    # --- verdict -----------------------------------------------------------
    executions = [
        observation
        for observation in result.observations
        if observation.get("tool") == "operation"
        and _is_target_operation(str(observation.get("operation", "")), observation.get("args"), target)
    ]
    successes = [obs for obs in executions if _operation_succeeded(obs)]

    first_success_index = None
    for index, observation in enumerate(result.observations):
        if observation in successes:
            first_success_index = index
            break

    repeated_requests: list[dict[str, Any]] = []
    if successes:
        success_signature = _normalized_args(successes[0].get("args"))
        seen_success = False
        for decision in decisions:
            requested = _requested_operation(decision)
            if requested is None:
                continue
            tool, arguments = requested
            if not _is_target_operation(tool, arguments, target):
                continue
            if not seen_success:
                # The call that produced the success itself is not a repeat.
                seen_success = True
                continue
            if _normalized_args(arguments) == success_signature:
                repeated_requests.append(decision)

    # Only counted once something succeeded. Repeating a call that keeps
    # failing is a loop, but not this one: there is no result to reuse, so it
    # says nothing about whether the controller reuses one. That run is
    # inconclusive, and reporting it as the #223 regression would send the
    # next reader looking for a prompt bug behind a broken GitHub token.
    repeated_executions = executions[1:] if successes else []
    guard_events = sorted(
        {
            str(observation.get("event"))
            for observation in result.observations
            if str(observation.get("event", "")) in LOOP_GUARD_EVENTS
        }
    )
    ended_on_guard = result.action == ACTION_CLARIFICATION and "tool loop limit" in result.reason

    if args.json:
        print(json.dumps(result.observations, indent=2, sort_keys=True, default=str))
        print()

    failures = []
    if repeated_requests:
        failures.append(
            f"the controller asked for {target} again after it succeeded "
            f"({len(repeated_requests)}x)"
        )
    if repeated_executions:
        failures.append(
            f"{target} executed {len(executions)} times with the same arguments"
        )
    if ended_on_guard:
        failures.append("the loop guard is what ended the task")

    if failures and successes:
        for failure in failures:
            print(f"FAIL: {failure}")
        for decision in repeated_requests:
            print(f"  repeated request: {redact_secrets(json.dumps(decision, sort_keys=True, default=str))[:500]}")
        if not args.show_prompts:
            print()
            print("compact observations given to the controller")
            _print_prompt_observations(prompts)
        return 1

    if not successes:
        print(f"INCONCLUSIVE: {target} never succeeded, so nothing was verified")
        repeats = [
            decision
            for decision in decisions
            if (requested := _requested_operation(decision)) is not None
            and _is_target_operation(*requested, target)
        ]
        if len(repeats) > 1 or ended_on_guard:
            # Still worth saying loudly: the controller looping on a failing
            # call is a real problem, just not the one this check decides.
            print(
                f"  note: the controller requested {target} "
                f"{len(repeats)} times"
                + (" and the loop guard ended the task" if ended_on_guard else "")
            )
        if "local inference failed" in result.reason:
            print("  local inference failed - is llama-server running?")
            print("    systemctl status llama-server --no-pager")
            print("    journalctl -u llama-server -n 50 --no-pager")
        elif executions:
            print("  it was executed but did not succeed:")
            for observation in executions:
                print(f"    {_describe_observation(observation)}")
            print("  a missing/expired GitHub token looks exactly like this")
        else:
            print(f"  the controller went to {result.action} without ever fetching the issue")
            print("  try a larger --max-steps")
        return 2

    if guard_events:
        # Refused but recovered from: worth seeing, not a failure by itself.
        print(f"note: loop guard refused a repeat ({', '.join(guard_events)}) but did not end the task")
    print(
        f"PASS: {target} succeeded in event "
        f"{successes[0].get('event_id')} and was not requested again; "
        f"the controller continued with {len(result.observations) - (first_success_index or 0) - 1} "
        "further observation(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
