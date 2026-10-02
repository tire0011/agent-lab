"""Issue #297: can the local model recognize when to escalate? Live probes.

Two scenarios with real texts:
- conflict: the current #288 issue and the outdated briefing earlier runs kept
  in memory. Every probe should escalate.
- control: the current #289 issue and memory that describes it correctly. No
  probe should escalate.

Nothing is escalated for real; each test records what the probe decided.
Approach 3 talks to llama-server directly (logprobs are not in the job queue
yet), so run this file while Nexus is idle.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from live_tests.conftest import live_budget
from modules.escalation_probes import (
    ESCALATE_COLLECTOR_RESPONSE_FORMAT,
    SAME_REQUIREMENT_RESPONSE_FORMAT,
    ask_with_logprobs,
    combine,
    escalate_collector_prompt,
    logprob_decision,
    parse_escalate_collector,
    parse_same_requirement,
    same_requirement_prompt,
)

ISSUE_288_TITLE = "Code change in ExperimentService: startup recovery reruns an interrupted experiment once"
ISSUE_288_BODY = """## Observed

After #281, the orphaned experiment (task for #277) was correctly marked `failed` with `interrupted by service restart`, and the parked Nexus session resumed with that result. Nexus then answered with a clarification instead of starting the experiment again, so the mailbox task ended `failed`.

An interruption by a restart says nothing about the change itself. It is an infrastructure hiccup of the experiment, so the experiment layer should absorb it. Neither Nexus nor the mailbox should have to know about it.

## Fix: handle it in `ExperimentService`

The startup recovery added in #281 (`modules/experiment_service/service.py`) currently marks experiments left in a running status as `failed`. Change it to:

1. First interruption: rerun the same experiment once. Same experiment id and goal (including the handover), a fresh worktree from the current base, the normal pipeline. Record on the experiment that it was restarted once, and log it.
2. Second interruption of the same experiment: mark it `failed` with `interrupted by service restart`, as today.

Because the experiment id stays the same, the Nexus session stays parked on it and receives only the final result. No change is needed in the Nexus orchestrator or in the mailbox.

Remove any mailbox-side handling of this case. The mailbox must not act as an experiment handler.

## Acceptance criteria

- After a restart, an experiment left in a running status is rerun once under the same id, and its parked Nexus session receives the rerun's final result.
- A second interruption of the same experiment marks it `failed` with `interrupted by service restart`.
- Experiments in a terminal status are not touched.
- The mailbox task service contains no restart-specific experiment handling."""
ISSUE_288_OLD_MEMORY = (
    "GitHub issue #288: An experiment interrupted by a restart should be resumed, not end the task. "
    "Known root cause: Interruption by service restart. Prior attempts: Fixing the issue by starting a "
    "new experiment directly. Acceptance criteria: A task whose experiment was interrupted by a restart "
    "gets a new experiment automatically and can still succeed. The automatic restart happens at most "
    "once per interrupted experiment and does not use up the 2-experiment limit. Other failures are still "
    "handed to Nexus. Tests cover all three cases."
)

ISSUE_289_TITLE = "Checkpoint fails when the issue branch is only behind the experiment worktree"
ISSUE_289_BODY = """## Observed

The second run for #277 ended `needs_review` before verification: GitHub checkpoint failed before verification: Issue branch no longer descends from the experiment worktree; manual reconciliation required.

The issue branch was created by the first, interrupted run. Its tip is an old commit of `main` and it has no commits of its own. The new experiment's worktree starts from the current `main`, which is ahead of that commit.

## Fix

In `GitHubWorkTracking.checkpoint_experiment` (`modules/github_work_tracking.py`): if the remote branch tip is an ancestor of the worktree HEAD, the branch has no work of its own. Use the worktree HEAD as the base for the checkpoint commit (a fast-forward) and skip the overlap check. A branch with commits of its own keeps today's behaviour.

## Acceptance criteria

- A first checkpoint on an issue branch that is behind the worktree succeeds, with the worktree HEAD as parent.
- A branch with its own commits that the worktree lacks is still rejected.
- Tests cover both cases."""
ISSUE_289_MEMORY = (
    "GitHub issue #289: Checkpoint fails when the issue branch is only behind the experiment worktree. "
    "Root cause: checkpoint_experiment requires the remote issue branch to descend from the worktree HEAD "
    "and rejects a branch that is merely behind. Fix: fast-forward when the branch tip is an ancestor of "
    "the worktree HEAD; keep rejecting branches with their own commits. Tests cover both cases."
)

# More pairs for approach 3 (#297 follow-up): real closed issues, each with an
# outdated memory (same item, earlier/other solution) and an accurate one.
MORE_ISSUES = {
    280: {
        "title": "Finalization restart still exits -15: hand it to a transient systemd unit",
        "body": (
            "## Observed\n\nAfter #275 (`systemctl --no-block restart`), the #224 run still ended `needs_review`: "
            "service restart failed: sudo -n systemctl --no-block restart nexus-mailbox agent-api exited -15.\n\n"
            "systemd starts stopping `agent-api` before the `sudo`/`systemctl` client has returned. The client is a "
            "child in `agent-api`'s control group, so it is killed with SIGTERM.\n\n"
            "## Fix: reuse the approach from `modules/nexus_refresh.py`\n\n"
            "Apply `systemd-run --collect --on-active=2 systemctl restart <service>` in "
            "`TaskFinalizationCoordinator._restart_services`:\n"
            "- Restart only `agent-api`; `nexus-mailbox` is a oneshot started by a timer.\n"
            "- Command: `sudo -n systemd-run --collect --on-active=2 systemctl restart agent-api`. Exit 0 means the "
            "restart is scheduled; any other exit code is still a failure.\n"
            "- `RESTART_SERVICES` and `scripts/setup-nexus-permissions.sh` grant exactly that command.\n\n"
            "## Acceptance criteria\n\n- Finalization reaches `restart_requested`, then live validation and "
            "`succeeded`.\n- A failing restart command is still reported as a failure.\n"
            "- Tests assert the command and that it matches the sudoers rule."
        ),
        "outdated": (
            "GitHub issue #280: Finalization restart exits -15. Fix: treat exit code -15 of "
            "`systemctl --no-block restart nexus-mailbox agent-api` as success, because SIGTERM only means the "
            "service is already stopping. Keep restarting both services. Tests cover the -15 case."
        ),
        "accurate": (
            "GitHub issue #280: Finalization restart still exits -15. Fix: hand the restart to a transient unit, "
            "`sudo -n systemd-run --collect --on-active=2 systemctl restart agent-api`, restart only agent-api, "
            "any non-zero exit is a failure, sudoers rule in setup-nexus-permissions.sh matches. Tests assert the command."
        ),
    },
    286: {
        "title": "Live validation after a restart runs before agent-api listens (connection refused)",
        "body": (
            "## Observed\n\nWith #280 the finalization restart works. The runs still ended `needs_review`: "
            "live validation failed: health endpoint unavailable: Connection refused.\n\n## Cause\n\n"
            "`api.py` calls `MailboxTaskService.recover_after_restart()` during startup, before uvicorn binds its "
            "port. It resumes the finalization in phase `restart_requested` and asks `/health` of the process itself, "
            "not yet listening.\n\n## Fix\n\nIn `TaskFinalizationCoordinator`: a health endpoint that is not "
            "reachable yet (connection refused / URL error) is not a failure. Finalization stays in "
            "`restart_requested` and returns `running`, so the next worker cycle tries again. The existing "
            "`restart_timeout_seconds` (600 s) still bounds the wait. A reachable endpoint with a wrong answer is "
            "still a failure.\n\n## Acceptance criteria\n\n- Connection refused keeps the finalization `running` "
            "without counting a retry.\n- Once the endpoint answers `ok`, it ends `succeeded`.\n"
            "- Unreachable beyond the restart timeout ends `needs_review`.\n- Tests cover all three cases."
        ),
        "outdated": (
            "GitHub issue #286: Live validation fails with connection refused after a restart. Fix: move "
            "`recover_after_restart()` out of startup into a background thread that sleeps 30 seconds before it "
            "runs, so uvicorn is listening by then. Tests check the delay."
        ),
        "accurate": (
            "GitHub issue #286: Live validation after a restart runs before agent-api listens. Fix: connection "
            "refused during live validation is not a failure; finalization stays in restart_requested and retries "
            "on the next worker cycle, bounded by restart_timeout_seconds. Wrong answers still fail. Tests cover all three cases."
        ),
    },
    279: {
        "title": "Tests create MagicMock/mock.db_path/* database files, and one landed on main",
        "body": (
            "## Problem\n\nPR #278 committed six SQLite files `MagicMock/mock.db_path/<id>` to main. "
            "`MailboxTaskService._with_task_store` opens the task store at "
            "`getattr(self.operations_registry, \"db_path\", DEFAULT_DB_PATH)`; tests pass a `MagicMock()` as "
            "`operations_registry`, so a real database file is created under `MagicMock/`.\n\n## Fix\n\n"
            "1. `_with_task_store` uses the service's own `self.db_path`.\n2. Add `MagicMock/` to `.gitignore`.\n"
            "3. Delete the six files.\n\n## Acceptance criteria\n\n- The full test suite leaves no `MagicMock/` "
            "directory.\n- A test asserts `_with_task_store` opens the store at the service's own `db_path`.\n"
            "- `MagicMock/` is in `.gitignore` and the files are gone."
        ),
        "outdated": (
            "GitHub issue #279: Stray MagicMock database files in the repository. Fix: add a pytest session "
            "fixture in conftest.py that deletes the MagicMock/ directory after the test run, and make the coding "
            "agent run `git clean` before committing. No change to the mailbox service."
        ),
        "accurate": (
            "GitHub issue #279: Tests create MagicMock/mock.db_path/* files. Fix: _with_task_store uses the "
            "service's own self.db_path instead of operations_registry.db_path, add MagicMock/ to .gitignore, "
            "delete the six files. A test asserts the db_path."
        ),
    },
    277: {
        "title": "Alternate between Codex and Claude instead of drawing at random",
        "body": (
            "## Problem\n\n`modules/coding_provider.py` draws the coding agent for each experiment at random "
            "(`choose_provider`). The last three experiments all ran on Codex.\n\n## Desired behavior\n\n"
            "Alternate strictly between the available providers: the next experiment uses the provider that did "
            "not run the previous one.\n- The last used provider is persisted, so the alternation survives "
            "restarts.\n- `AGENT_CODING_PROVIDER` (pin) and `AGENT_CODING_PROVIDERS` (allow list) keep working; "
            "with one available provider, that one is used.\n- One agent per experiment stays.\n"
            "- Update the docstring and `OPERATIONS.md`.\n\n## Acceptance criteria\n\n- Two consecutive "
            "experiments use different providers when both are available.\n- Alternation continues after a "
            "restart.\n- Pin and allow list take precedence.\n- Tests cover alternation, restart, pin and a single provider."
        ),
        "outdated": (
            "GitHub issue #277: Choose the coding provider better. Desired: pick the provider with the lowest "
            "failure rate over the last ten experiments, falling back to a random draw on a tie. Store the "
            "failure statistics in a new table."
        ),
        "accurate": (
            "GitHub issue #277: Alternate between Codex and Claude instead of drawing at random. The next "
            "experiment uses the provider that did not run the previous one; last provider persisted across "
            "restarts; pin and allow list still win. Tests cover alternation, restart, pin, single provider."
        ),
    },
}

SCENARIOS = {
    "conflict": {
        "number": 288, "title": ISSUE_288_TITLE, "body": ISSUE_288_BODY,
        "memory": ISSUE_288_OLD_MEMORY, "expect_escalate": True,
    },
    "control": {
        "number": 289, "title": ISSUE_289_TITLE, "body": ISSUE_289_BODY,
        "memory": ISSUE_289_MEMORY, "expect_escalate": False,
    },
}
for _number, _issue in MORE_ISSUES.items():
    for _kind, _expect in (("outdated", True), ("accurate", False)):
        SCENARIOS[f"{_number}-{_kind}"] = {
            "number": _number, "title": _issue["title"], "body": _issue["body"],
            "memory": _issue[_kind], "expect_escalate": _expect,
        }


def _current_text(scenario: dict) -> str:
    return f"GitHub issue #{scenario['number']}: {scenario['title']}\n\n{scenario['body']}"


def _collector_sources(scenario: dict) -> list[dict]:
    """The sources the production collector sees for `solve github issue #N` (#292 order)."""
    meta = {"number": scenario["number"], "title": scenario["title"], "state": "open", "url": "[REMOTE]"}
    body = scenario["body"]
    lines = [f"issue: {json.dumps(meta)}"] + [
        f"body[{i // 450 + 1}]: {body[i:i + 450]}" for i in range(0, len(body), 450)
    ]
    return [
        {
            "tool": "memory_search",
            "query": f"GitHub issue #{scenario['number']}",
            "note": (
                "Earlier project history. It may describe an older version of "
                "this issue; the current GitHub issue wins on any conflict."
            ),
            "results": [{"source": "task_event", "kind": "knowledge_collection", "content": scenario["memory"]}],
        },
        {
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": scenario["number"]},
            "result": {"operation": "github_issue", "success": True, "output_lines": lines},
        },
    ]


def _queued(orchestrator, prompt: str, response_format: dict, max_tokens: int) -> tuple[str, float]:
    session = orchestrator.task_store.get_or_create(
        source_type="task", source_id=f"live-297-{time.time_ns()}", title="escalation probe"
    )
    started = time.monotonic()
    raw, _provider = asyncio.run(
        orchestrator._schema_completion(
            prompt,
            session_id=session.id,
            max_tokens=max_tokens,
            response_format=response_format,
            budget_plan=live_budget(),
            role="knowledge_collector",
        )
    )
    return raw, round(time.monotonic() - started, 1)


def probe_escalate_action(orchestrator, scenario: dict):
    base = orchestrator._knowledge_collector_prompt(
        f"Gather all relevant factual information about GitHub issue #{scenario['number']}.",
        _collector_sources(scenario),
        live_budget(),
        force_briefing=True,
    )
    raw, seconds = _queued(orchestrator, escalate_collector_prompt(base), ESCALATE_COLLECTOR_RESPONSE_FORMAT, 650)
    result = parse_escalate_collector(raw)
    result.details.update(seconds=seconds, raw=raw[:2000])
    return result


def probe_yes_no(orchestrator, scenario: dict):
    prompt = same_requirement_prompt(_current_text(scenario), scenario["memory"])
    raw, seconds = _queued(orchestrator, prompt, SAME_REQUIREMENT_RESPONSE_FORMAT, 20)
    result = parse_same_requirement(raw)
    result.details.update(seconds=seconds, raw=raw[:200])
    return result


def probe_logprobs(llama_server: dict, scenario: dict):
    prompt = same_requirement_prompt(_current_text(scenario), scenario["memory"])
    started = time.monotonic()
    response = asyncio.run(ask_with_logprobs(llama_server["url"], llama_server["model"], prompt))
    result = logprob_decision(response)
    result.details.update(seconds=round(time.monotonic() - started, 1))
    return result


@pytest.mark.parametrize("scenario_name", ["conflict", "control"])
def test_escalate_action(orchestrator, live_result, scenario_name):
    scenario = SCENARIOS[scenario_name]
    result = probe_escalate_action(orchestrator, scenario)
    live_result.update(approach=1, scenario=scenario_name, result=result.as_dict())
    assert result.escalate == scenario["expect_escalate"], result.as_dict()


@pytest.mark.parametrize("scenario_name", ["conflict", "control"])
def test_yes_no(orchestrator, live_result, scenario_name):
    scenario = SCENARIOS[scenario_name]
    result = probe_yes_no(orchestrator, scenario)
    live_result.update(approach=2, scenario=scenario_name, result=result.as_dict())
    assert result.escalate == scenario["expect_escalate"], result.as_dict()


@pytest.mark.parametrize("scenario_name", list(SCENARIOS))
def test_logprobs(llama_server, live_result, scenario_name):
    scenario = SCENARIOS[scenario_name]
    result = probe_logprobs(llama_server, scenario)
    live_result.update(approach=3, scenario=scenario_name, result=result.as_dict())
    assert result.details.get("available"), result.as_dict()
    assert result.escalate == scenario["expect_escalate"], result.as_dict()


@pytest.mark.parametrize("scenario_name", ["conflict", "control"])
def test_combined(orchestrator, llama_server, live_result, scenario_name):
    scenario = SCENARIOS[scenario_name]
    result = combine(
        [
            probe_escalate_action(orchestrator, scenario),
            probe_yes_no(orchestrator, scenario),
            probe_logprobs(llama_server, scenario),
        ]
    )
    live_result.update(approach="combined", scenario=scenario_name, result=result.as_dict())
    assert result.escalate == scenario["expect_escalate"], result.as_dict()
