# Open issue triage (#336)

Step 2 of moving all planning to GitHub issues (step 1 was #334). This list
only proposes decisions; nothing is closed until it has been reviewed and
merged.

Source: `gh issue list --state open --limit 100` on 2026-10-24. Excluded are
#336 itself and issues created in the last 7 days (#322, created 2026-10-19).
Decisions are based on the code on `main` and the merged PRs; when unsure the
decision is `keep`.

| Issue | Decision | Reason |
| --- | --- | --- |
| #294 Free-form tasks: an external planner writes the issue, Nexus works it off | keep | No `planner` role exists on the Nexus controller path; `planner` only appears in the legacy `modules/multi_provider_orchestrator.py` and `modules/llama_router.py`. |
| #288 Code change in ExperimentService: startup recovery reruns an interrupted experiment once | keep | `ExperimentService.mark_interrupted_experiments_failed` (`modules/experiment_service/service.py`) still marks every interrupted experiment `failed` and nothing reruns it or counts restarts. |
| #276 Local model as a coding agent for new_experiment, chosen by task complexity | keep | `modules/coding_provider.py` only knows the `codex` and `claude` providers and `new_experiment` has no `complexity` argument. |
| #264 Remove the mailbox task approval step | keep | `pending_approval` is still the entry state in `modules/mailbox/task_service.py` (`_STATUS_PENDING`) and `modules/mailbox/processing.py`. |
| #263 Make new_experiment a Nexus tool: knowledge handover, pause, result back to Nexus | keep | Steps 1-4 landed in PRs #262, #265, #268 and #271, but the call sites still special-case it (`modules/nexus_controller_service.py`, `modules/nexus_chat_service.py`, `MailboxTaskService._start_new_experiment`), which the acceptance criteria rule out. |
| #257 Teach Nexus to respect knowledge_collector used_sources | close-done | PR #261 added the `used_sources` rule to the controller prompt (`modules/task_orchestrator.py`, "used_sources lists sources already consulted"). |
| #248 Separate Nexus event journal from curated long-term memory | close-done | The near-term rule is in `modules/nexus_memory.py` (`ORCHESTRATION_EVENT_KINDS`, `is_memory_eligible_event()`), the end-to-end recursion test is in `tests/test_nexus_memory.py`, and ARCHITECTURE.md ("Event journal vs. curated memory") plus D-028 describe EventStore vs. MemoryStore. A separate MemoryStore with explicit promotion is still future work. |
| #239 A mailbox command has no timeout, and the `timeout:` front-matter key is read nowhere | keep | `process_command` in `modules/mailbox/development.py` still re-defers while the pid is alive with no deadline, and no `timeout` front-matter key is read. |
| #236 Bound local controller calls so timeout cleanly triggers external fallback | keep | `NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS` still defaults to 3600 in `modules/task_orchestrator.py` and `modules/local_llm_jobs.py`. |
| #235 Self-restart has no fallback when systemd-run is unavailable | keep | `modules/nexus_refresh.py` still restarts the own unit only through `systemd-run` with no `systemctl --no-block` fallback or three-state report. |
| #232 Staged controller view for large tool results, and a way to page back into one | keep | No semantic compaction rung and no `read_tool_result` capability exist anywhere in `modules/`. |
| #230 Prompt budget: bound against the context window, stop pretending to manage host memory | keep | `compute_budget` in `modules/prompt_budget.py` still splits by fixed shares (instructions 12 %, ...) and still derives a `memory_budget_chars`. |
| #221 Mailbox worker: handle busy as success and drain inbox before exiting | close-done | PR #225: `modules/mailbox/poll_runner.py` returns `busy` as a skipped poll (`mailbox_processor.py` exits 0) and `_drain_inbox` rescans with `max_passes`/`max_seconds` bounds. |
| #220 Mailbox: accept all commands and redact secrets only in output | keep | `MailboxMessage.load` in `modules/mailbox/core.py` still raises "Potential secret detected in message content" on the input. |
| #196 Publish Nexus Outbox messages to communication PR | close-done | PR #242 added `modules/outbox_pr_notifier.py`, which posts `[NEXUS-OUTBOX]` comments to PR #194 idempotently with retries. |
| #185 Nexus must not merge a pull request when the task explicitly says do not merge | keep | No no-merge constraint is parsed or checked anywhere in `modules/` (`task_finalization.py`, `experiment_adoption_service.py`). |
| #179 Idea: Use GitHub events to wake ChatGPT when Nexus finishes a task | close-outdated | Superseded by #196, whose implementation in PR #242 (`modules/outbox_pr_notifier.py`) is that GitHub wake-up signal. |
| #163 Diagnostics: classify task outcome using heartbeat and terminal state | keep | PR #198 (`modules/nexus_diagnostics.py`) classifies running/succeeded/failed/abandoned/unknown on the #162 heartbeat, but there are no separate cancelled or interrupted-by-restart states yet. |
| #158 Architecture: inverted model roles, chain length, denylist policy, missing heartbeat | keep | Heartbeat (#162) and hermetic tests (PR #161) are done, but the plan keyword denylist is still in `modules/experiment_service/policy.py` (`_GIT_ACTION_KEYWORDS`) and the controller still runs on the local model. |
| #156 Autonomy: why Nexus never ran on its own, and what is left | close-done | PR #157 fixed the three blockers, PR #161 closed the test collection hole, heartbeats came with #162, and the remaining risks are tracked in #158. |
| #150 Ensure FailureAnalysisService initializes and runs for finalization needs_review | close-done | PR #154 showed every `needs_review` path reaches `_transition_finalization_terminal` and added a restart regression test; PRs #155 and #169 added observability and the shared job path. |
| #120 Automatically recover from missing development tools and create capability issues | keep | `modules/task_orchestrator.py` reports `missing_executable` as `missing_capability`, but nothing creates a capability issue or installs the tool. |
| #119 Distinguish verified implementation from integrated/deployed task completion | close-done | PR #122 keeps verified tasks non-terminal through `verified`/`merging`/`deploying`/`validating_live` before `succeeded` (`modules/task_finalization.py`). |
| #118 Expose Nexus tool router as standalone MCP stdio server | keep | There is no `modules/tool_router_mcp` and `nexus_mcp.py` does not expose `find_tool`/`call_tool`. |
| #117 Do not treat find_tool related_tools as granted operations | close-done | PR #166 made `not_found` terminal and kept related operations as suggestions only, and PR #219 made the alternatives actionable (`modules/nexus_tool_router.py`, `terminal_not_found_guidance`). |
| #86 Collapse all log entries and load details on demand | keep | `renderLogEntries` in `static/logs.js` renders collapsible rows, but the details arrive with the list payload instead of being loaded on demand. |
| #84 Show a unified Nexus log feed in the Logs tab | close-done | PR #88 added the merged feed, and PRs #94 and #106 replaced the Logs UI with one unified timeline (`static/logs.js`, `GET /logs`). |
| #75 Show linked Inbox/Outbox messages as one conversation in the web UI | keep | `static/mailbox.js` still renders Inbox and Outbox as two separate lists. |
| #74 Experiment: Reduce LLM context noise from redundant comments and docstrings | keep | No measurement or cleanup was done; the named `modules/experiment_service.py` became a package in PR #87, but the experiment still applies. |
| #43 Post experiment and task progress as GitHub Issue comments | keep | `modules/github_work_tracking.py` posts no issue comments; outbox comments go only to the communication PR (#196). |
| #41 Integrate official GitHub MCP server for Nexus | keep | Nexus only has the read-only `github_issue` operation from PR #203 (`modules/operations_registry.py`); the official GitHub MCP server is not integrated. |
| #26 Add adaptive two-pass context research before development | close-outdated | It targets the PR #21 `MultiProviderOrchestrator`, which only `agent.py` still uses; `new_experiment` now runs Codex/Claude CLI agents (`modules/coding_provider.py`) that gather their own context. |
| #25 Evaluate Aider RepoMap as Nexus Context Curator | close-outdated | It targets the PR #21 `MultiProviderOrchestrator`, which only `agent.py` still uses; `new_experiment` now runs Codex/Claude CLI agents (`modules/coding_provider.py`) that gather their own context. |
| #24 feat: add token-budgeted Context Curator after PR #21 | close-outdated | It targets the PR #21 `MultiProviderOrchestrator`, which only `agent.py` still uses; `new_experiment` now runs Codex/Claude CLI agents (`modules/coding_provider.py`) that gather their own context. |
