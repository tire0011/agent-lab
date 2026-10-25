# Ideas to issues (#340)

Step 3 of moving all planning to GitHub issues (steps 1 and 2 were #334 and
#336). This file only proposes issues; nothing is created on GitHub until it
has been reviewed and merged. Afterwards ideas live only as issues, and the
`## Ideas` section is gone from `IDEAS.md` (its full text stays in git history
and in the list below).

Source: every idea under `## Ideas` in `IDEAS.md` on `main` (60aec40) still
marked `new` or `exploring` (56 of 69; the other 13 were `adopted`, `tested` or
`rejected` after #334). Each idea was checked against the code on `main` and
against `gh issue list --state open` on 2026-10-24. Near-duplicates are merged
into one proposed issue; the `create` line names it.

## Proposed issues

### P1 Fixed benchmark task set for experiments, with a baseline to compare against

Merges idea-4, idea-5, idea-6, idea-8, idea-13, idea-21, idea-25, idea-40, idea-41.

There is no fixed task set to tell whether a model, provider, coding CLI or
prompt change makes `new_experiment` better or worse. `scripts/model-benchmark.sh`
(#302) only compares local models on the controller. Add a small frozen set of
representative tasks (for example `benchmarks/tasks/*.md`, each with a goal, a
pinned base commit and a scoring rubric: tests green, expected files touched,
`modules/experiment_evaluation.py` verdict) and a runner that executes them
through `ExperimentService` with a chosen provider/model/prompt and stores per
task: score, tokens, runtime, estimated cost and peak memory in a new SQLite
table (`benchmark_runs`). A report compares one run against a stored baseline
run side by side. The first baseline is the current Claude/Codex setup; later
runs compare cheaper Azure deployments, other providers and prompt changes.
Test: unit tests for the runner and the report with a fake coding worker and
two fake runs (better / worse than the baseline).

### P2 Cancel a running experiment

From idea-23 (retargeted from the old Codex task API to experiments).

Once an experiment is `implementing` or `verifying` it cannot be stopped; the
only way out is a timeout or a service restart. Add `POST
/experiments/{id}/cancel` in `api.py` and a cancel path in
`modules/experiment_service/service.py` that terminates the coding worker or
test subprocess (`modules/claude_worker.py`, `modules/codex_worker.py`,
`modules/verification_runner.py`), keeps the worktree, and sets a new terminal
status `cancelled` (`modules/experiment_service/models.py`) with who cancelled
and when. Test: a fake worker that blocks until killed; cancel moves the
experiment to `cancelled`, the process is gone, cancelling a finished
experiment returns 409, and the endpoint requires the API key.

### P3 Bounded web research operation backed by SearXNG

Merges idea-27 to idea-32.

The Nexus controller cannot search the web itself. Add a `web_research`
operation (`modules/operations_registry.py`, new `modules/web_research.py`) that
queries a configurable SearXNG JSON endpoint, fetches at most N result pages
with request, size, timeout and concurrency limits, respects robots.txt, and
returns a short answer with source URLs. Log search count, fetched pages,
tokens, runtime, failures and estimated cost per call (`operation_log`).
Installing SearXNG on the VM is a separate, approved deployment step. A
follow-up measurement compares it with Gemini grounding on a small fixed
question set, counting Google Search requests separately from token cost.
Test: mocked HTTP for search and fetch; limits, robots.txt refusal and timeouts
are enforced and logged.

### P4 Make the docs agree on the deployed VM size

From idea-42.

`README.md`, `AGENTS.md`, `OPERATIONS.md`, `ARCHITECTURE.md` and `COSTS.md` all
say `Standard_B1s`, while `PROJECT_HISTORY.md` records a `Standard_B2ms` and
calls that measurement historical rather than a statement of the current SKU. The owner confirms the size once
(`az vm show -g agent-lab-rg -n agent-lab-vm --query hardwareProfile.vmSize`),
then all five files are updated to it. Test: a doc test that every one of those
files names the same SKU, so the next resize cannot leave one behind.

### P5 Token and cost view per experiment and per day in the Web UI

From idea-44 (and the cost part of idea-14).

The data exists (`runs.estimated_cost`/`actual_cost`, the `*_usage_json`
columns on `experiments`, `provider_usage`, `AzureCostMonitor` in
`modules/cost_monitor.py`), but no endpoint or view shows it. Add an
authenticated `GET /costs/summary` in `api.py` (tokens and estimated cost per
experiment and per day, Azure month-to-date actual cost when available) and a
card in the Übersicht tab (`static/index.html`, `static/overview.js`). Test:
`tests/test_api.py` with a seeded SQLite file checks the totals, the empty case
and the API key requirement.

### P6 Per-experiment token budget for coding agents, shadow mode first

From idea-54.

Token use is only reported after an experiment ends (`implementation_usage_json`);
one task used over a million tokens. Add a configurable budget per experiment
(`NEXUS_EXPERIMENT_TOKEN_BUDGET`) that `modules/experiment_service/service.py`
checks against the usage the Claude worker already streams (`stream-json` in
`modules/claude_worker.py`) and the Codex JSON events. In shadow mode it only
logs where it would have stopped; in enforce mode it stops the worker and ends
the experiment in `needs_review` with the stop reason, budget and consumed
amount. Test: a fake worker stream that crosses the budget; shadow mode logs and
continues, enforce mode stops with the recorded reason.

### P7 Bounded reflection after each experiment that proposes at most three improvements

Merges idea-11 and idea-55.

`modules/failure_analysis_service.py` diagnoses failures, but nothing asks after
a finished experiment what would make the next one cheaper or better. After an
experiment reaches a terminal state, send a compact summary (goal, plan, changed
files, phase timings, token usage, test result, error) to a cheap model with a
small token budget and ask for at most three suggestions, each with expected
benefit, cost, risk and how to measure it; "no useful improvement found" is a
valid answer. Store results in a new `experiment_reflections` table, deduplicate
against open issue titles, and never act on them automatically. Test: fake LLM
responses for zero, three and too many suggestions; a reflection failure never
changes the experiment status.

### P8 Report-only VM maintenance and resource report

Merges idea-57 and idea-58.

`nexus_reboot_check.py` (#332) handles `reboot-required`, but nothing reports
pending updates or resource pressure. Add a read-only `vm_maintenance_report`
operation (`modules/operations_registry.py`) that collects upgradable and
security packages, failed systemd units and timers, disk and swap use, memory
pressure, the largest processes and certificate expiry, and writes a short
report. It changes nothing; any removal, upgrade or service change stays a
separate approved action. Test: fake command outputs produce the expected
report, and a failing command degrades to an "unknown" line instead of an error.

### P9 Multi-line idea input in the Web UI

From idea-68.

The idea field is a single-line `<input id="idea-input" type="text">`
(`static/index.html`, styled in `static/styles.css` `.idea-form`), too small for
ideas of several sentences. Replace it with a `<textarea>` (about three rows,
`maxlength="500"`, full width on mobile) and keep submission in
`static/overview.js` working. Test: a static-content test that `idea-input` is a
textarea with `maxlength="500"`.

## Decision per idea

### Benchmarking V0 vs V1

- **idea-4** Create a fixed benchmark task set with a quality rubric/scorer. `new`
  - **create:** P1
- **idea-5** Track tokens, runtime, estimated cost, and resource usage per benchmark run. `new`
  - **create:** P1
- **idea-6** Produce a side-by-side V0/V1 benchmark report. `new`
  - **create:** P1

### Model/provider abstraction

- **idea-7** Define a provider/model interface; implement an Azure adapter first, then at least one non-Azure adapter. `new`
  - **drop:** Done: `modules/coding_provider.py` (codex/claude), `modules/nexus_delegation.py` (local, Azure OpenAI, Codex, Claude) and the `provider_usage`/`model_consultations` tables already put several providers behind one interface.
- **idea-8** Evaluate cheaper Azure model options and at least one additional provider for quality/cost/runtime tradeoff. `new`
  - **create:** P1

### Evaluation gate before self-modification

- **idea-9** Define pass/fail criteria before any self-modification is allowed. `new`
  - **drop:** Done: verification runs the test suite, `modules/experiment_evaluation.py` checks the diff against the plan, and adoption (`modules/experiment_adoption_service.py`) only integrates verified experiments after approval.
- **idea-10** Block changes that fail evaluation; log outcomes in the structured store. `new`
  - **drop:** Done: failed verification or evaluation ends in `failed`/`needs_review` and is stored on the `experiments` row (`test_exit_code`, `quality_evaluation_json`), so the change is never adopted.

### Controlled self-improvement loop

- **idea-11** Agent reviews previous runs and identifies one measurable weakness. `new`
  - **create:** P7
- **idea-12** Agent proposes one bounded improvement before being allowed to modify itself automatically. `new`
  - **drop:** Done: every experiment starts with a selection phase that writes a bounded plan (`plan_json`), and nothing is adopted without approval.
- **idea-13** Evaluate each candidate against a fixed baseline; accept only changes that pass quality/cost/runtime gates, otherwise roll back. `new`
  - **create:** P1
- **idea-14** Persist experiment hypothesis, change, metrics, result, decision, and cost. `new`
  - **drop:** Done except cost: the `experiments` table stores goal, plan, diff, test result, quality evaluation and per-phase usage, `experiment_adoptions` stores the decision; cost is covered by P5.

### Azure GPU worker VM

- **idea-16** Evaluate a separate on-demand Azure GPU VM as Nexus's model-compute worker: start it only for local-LLM inference, benchmarking, fine-tuning, or other GPU experiments; measure model quality, tokens/second, startup time, VRAM/RAM use, energy/resource use, and total cost against hosted APIs and non-GPU execution; enforce spending limits and automatically stop/deallocate it when idle. `new`
  - **drop:** Too vague for an issue: it is a spending decision for the owner, not a code change, and comparing local models on the current VM already exists in `scripts/model-benchmark.sh` (#302).

### Test isolation / second VM

- **idea-17** Investigate a second Azure VM as an ephemeral test worker; compare cost of local tests on the main VM vs a disposable test VM for risky changes. `new`
  - **drop:** Outdated: GitHub Actions (`.github/workflows/tests.yml`) already runs the suite on disposable runners, and experiments verify in isolated worktrees.
- **idea-18** Preferred flow if adopted: create/start test VM -> fresh checkout -> tests/benchmarks -> return result -> deallocate/delete. `new`
  - **drop:** Outdated: same as the idea above; disposable test runs already happen on GitHub-hosted runners.
- **idea-19** Don't keep a second VM running 24/7 unless measurements justify it. `new`
  - **drop:** Outdated: no second VM exists or is planned, so there is nothing to keep from running.

### Coding CLI as a replaceable tool

- **idea-21** Later compare multiple coding CLIs/models by task quality, cost, tokens, and runtime, and route tasks accordingly. `new`
  - **create:** P1

### Codex task API follow-ups

- **idea-22** Add an approval workflow (currently any `queued` task can be run immediately by an authenticated caller). `new`
  - **drop:** Outdated: tasks now go through the mailbox approval (`modules/mailbox/task_service.py`) and `new_experiment`, not the Codex task API, and #264 removes that approval step.
- **idea-23** Add task cancellation (currently a running task cannot be stopped once started). `new`
  - **create:** P2
- **idea-24** Move execution off the request thread once background execution is deliberately in scope (currently strictly synchronous; a long Codex call blocks the HTTP response). `new`
  - **drop:** Done: `ExperimentService` and the mailbox task worker run in background threads; the synchronous Codex task API is no longer the path in use.

### Baseline measurement

- **idea-25** Run one reproducible end-to-end baseline task and persist quality, tokens, runtime, estimated cost, and resource usage. `new`
  - **create:** P1

### Web research worker

- **idea-27** Evaluate a self-hosted SearXNG instance as a low-cost search backend. `new`
  - **create:** P3
- **idea-28** Build a small `WebResearchWorker` that searches through SearXNG, fetches a bounded number of result pages, and returns answers with source URLs. `new`
  - **create:** P3
- **idea-29** Add strict request, page-count, download-size, timeout, and concurrency limits; respect robots.txt, provider terms, rate limits, and site access restrictions — do not scrape Google AI Overviews as a production dependency. `new`
  - **create:** P3
- **idea-30** Log search count, fetched pages, tokens, runtime, failures, and estimated cost per research task. `new`
  - **create:** P3
- **idea-31** Compare SearXNG against Gemini Grounding with Google Search on a small fixed question set for answer quality, source quality, freshness, runtime, and total cost. `new`
  - **create:** P3
- **idea-32** When testing Gemini 3.x, account separately for the monthly included Google Search request allowance and the model input/output token charges — one prompt may trigger multiple search requests. `new`
  - **create:** P3

### Memory and self model

- **idea-33** Add a persistent self model (`SELF.json` or SQLite-backed equivalent) containing version, capabilities, known weaknesses, resources, goals, and current constraints. `new`
  - **drop:** Too vague: `NEXUS.md` already holds identity, goals and constraints, `nexus_version.py` the version, and `modules/nexus_memory.py` known facts; a separate SELF.json names no concrete gap.
- **idea-34** Separate memory into current task context, project memory, experience/experiment memory, and self model. `new`
  - **drop:** Duplicate of open issue #248 (separate the event journal from curated long-term memory).
- **idea-35** Load only relevant memory into LLM context instead of continuously growing one large prompt. `new`
  - **drop:** Done: `modules/nexus_memory.py` exposes memory as a bounded search tool and `modules/prompt_budget.py` compacts the controller prompt.
- **idea-36** Reuse the existing SQLite store for structured memories and experiment history before adding embeddings/RAG. `new`
  - **drop:** Done: `modules/nexus_memory.py` searches the existing SQLite `knowledge` rows and task events, without embeddings.

### Prompt library and work modes

- **idea-37** Add a versioned prompt library for distinct work modes such as `explore`, `research`, `plan`, `implement`, and `evaluate`. `new`
  - **drop:** Done: `modules/prompt_library.py` loads versioned prompts from `prompts/` (front matter with `version` and `role`).
- **idea-38** Let the orchestrator select the appropriate prompt and permitted tools per task instead of assuming every task is a code change. `new`
  - **drop:** Outdated: the controller now chooses operations through the tool router (`modules/nexus_tool_router.py`), and code changes run as `new_experiment` with their own prompts.
- **idea-39** Allow the agent to propose, create, and improve its own prompts and add new work modes when a capability gap is identified. `new`
  - **drop:** Too vague: it names no prompt, gap or measurement; any prompt change can already be proposed as an issue and must pass the baseline from P1.
- **idea-40** Evaluate every prompt change against a baseline for quality, cost, tokens, and runtime before adoption; keep prompt history and automatically roll back regressions or constraint violations. `new`
  - **create:** P1
- **idea-41** Store prompt versions, hypotheses, evaluations, and adoption decisions in SQLite. `new`
  - **create:** P1

### Documentation accuracy

- **idea-42** Periodically re-check that README.md/AGENTS.md describe the actual deployed VM size, API endpoints, clients, and Web UI rather than a stale snapshot. `exploring`
  - **create:** P4

### Communication interface

- **idea-43** Extend the existing Problems system into a persistent two-way communication channel: the agent can report blockers or needs with a concrete question, humans can reply in a problem-specific thread, the agent can read and respond to those replies, and the conversation remains available until the problem is resolved. `new`
  - **drop:** Outdated: the Git mailbox (`inbox/`/`outbox/`), the chat and the outbox PR comments (`modules/outbox_pr_notifier.py`) already give Nexus a two-way channel with humans.
- **idea-44** Add experiment, cost, and approval views once the corresponding backend contracts exist. `new`
  - **create:** P5
- **idea-45** Keep communication adapters separate so messenger integrations such as WhatsApp can be added later without changing the agent core. `new`
  - **drop:** Done: messaging lives in `modules/mailbox/`, `modules/outbox_publisher.py` and `modules/outbox_pr_notifier.py`, apart from the controller; a WhatsApp adapter can be proposed once someone needs it.
- **idea-46** Explore using the same communication contract for agent-to-agent communication. `exploring`
  - **drop:** Too vague: ChatGPT already talks to Nexus through the same mailbox contract, and no further agent is named.

### Autonomy stages (framework idea, not a plan of record)

- **idea-48** Level 1: agent proposes its own next experiment. `new`
  - **drop:** Done: `modules/nexus_initiative.py` picks Nexus's own next work item (integration backlog, open issues) each tick.
- **idea-49** Level 2: agent executes cheap, reversible, bounded experiments automatically. `new`
  - **drop:** Done: the initiative tick starts bounded experiments on its own within a daily budget (`NEXUS_INITIATIVE_DAILY_BUDGET`).
- **idea-50** Level 3: continuous improvement loop with automatic evaluation and rollback. `new`
  - **drop:** Too vague: a framework level, not a change; its parts are P1 (baseline) and P7 (reflection).
- **idea-51** Level 4: architecture/model/resource changes within explicit budgets and safety limits. `new`
  - **drop:** Too vague: a framework level with no concrete change; resource changes stay owner decisions under `COSTS.md`.

### Economic sustainability experiments

- **idea-52** Investigate whether Nexus can generate enough measurable economic value to offset or fund part of its own operating costs: search public repositories and communities for small problems with demonstrated demand, paid open-source bounties, or opportunities for useful tools and services; estimate effort, expected value, implementation risk, and API/cloud cost; then propose one bounded low-risk experiment at a time. Track revenue or verified value generated minus all operating costs as the net project contribution. External communication, account creation, publication, pricing, contracts, payment handling, and spending remain subject to explicit human approval; income is legally received and controlled by the human project owner, who may allocate an agreed portion back to Nexus's budget. `new`
  - **drop:** Too vague: every step (accounts, publication, pricing, payment) needs the owner's approval and there is no code change to describe.

### Later / no timeline

- **idea-53** Azure actual-cost reconciliation against estimated cost. `new`
  - **drop:** Done: `modules/cost_monitor.py` reads the Azure Cost Management actual cost and agent.py reports the actual cost delta next to the estimate (`modules/cli_output.py`).

### Mailbox submissions

- **idea-54** Add a Token Governor that enforces explicit per-phase and per-experiment budgets instead of merely reporting token usage after completion.  Define task-size classes and initial configurable budgets for research, selection, implementation, retries, and total usage. Before each LLM call, check remaining budget. When a soft limit is reached, require context compaction, narrower file retrieval, a cheaper model, or a revised smaller plan. At the hard limit, stop safely with needs_review rather than continuing uncontrolled.  Record tokens by model, call, phase, cached/uncached category when available, and useful output such as accepted changed lines or successful tests. Expose budget, consumed amount, remaining amount, and stop reason in logs and the Web UI. Start in shadow mode to learn realistic limits, then enforce limits for small tasks. Use the million-token Logs experiments as baseline evidence. `new`
  - **create:** P6
- **idea-55** After every completed, failed, rejected, or timed-out task, run a bounded post-task reflection that asks an LLM what could improve Nexus's process, tools, prompts, context selection, tests, safety, cost, runtime, or architecture.  Give the reflection only a compact structured summary of the goal, plan, changed files, lifecycle timings, token usage, test outcome, failure evidence, and final decision; do not resend the full conversation, repository, prompts, diffs, or raw logs. Require at most three concrete evidence-backed suggestions, each with expected benefit, cost, risk, measurement method, and smallest experiment. Allow "no useful improvement found" as a valid result.  Use a strict small token budget and preferably the cheap local decision model when available. Deduplicate suggestions against existing ideas and previous reflections. Store new candidates with provenance and measurable hypotheses, but never implement them automatically merely because the reflecting model suggested them. Track which suggestions are later tested, adopted, rejected, or repeatedly rediscovered so reflection quality itself can be evaluated. `new`
  - **create:** P7
- **idea-56** Evaluate a Context Curator that selects a small evidence-backed set of relevant files, symbols, and line ranges before an expensive coding model receives a task.  Reuse existing open-source approaches before building a custom index. Compare at least: a simple rg plus Python/JavaScript symbol-outline baseline, Aider's Tree-sitter repository-map approach, and Serena's LSP/MCP symbol retrieval. Consider Zoekt only when repository size or multi-repository search justifies its additional service and index cost.  Give each curator a strict context budget, initially 4K or 8K tokens, and return file paths, relevant symbols or ranges, dependency clues, confidence, and reasons. Never include file contents merely because a file is large or central. Let the coding model request bounded context expansion when evidence is insufficient.  Evaluate on frozen representative tasks using relevant-file recall/precision, missing-required-file rate, context tokens, total task tokens, runtime, memory, implementation success, and test results. Compare against the current unconstrained exploration baseline, including the 1,102,844-token Logs task. Adopt only if quality remains stable while total tokens and latency improve. `new`
  - **drop:** Outdated: the #336 triage closed the same Context Curator issues (#24, #25, #26) as outdated, because `new_experiment` now runs Codex/Claude CLI agents that gather their own context.
- **idea-57** Let Nexus continuously study and improve the operating-system environment it runs on, with measurable resource optimization and safe rollback.  Periodically inventory CPU, memory, swap, disk, I/O, processes, systemd services, startup time, installed packages, caches, logs, open ports, and resource pressure. Identify services, packages, timers, caches, and configuration that appear unused or wasteful. First produce evidence-backed proposals with expected savings, dependencies, risks, and rollback steps. Measure a baseline and compare CPU, RAM, disk, boot/service time, reliability, and cost after each approved change.  Never remove or disable components only because they look inactive. Protect SSH, networking, firewall, Git, Python, systemd, logging, time synchronization, certificate handling, agent services, recovery access, and cloud-management components. Require human approval initially for package removal, service disabling, firewall changes, storage cleanup beyond bounded caches, or other system mutations. Apply one small reversible change at a time, verify health afterward, log everything, and automatically roll back when health checks regress. `new`
  - **create:** P8
- **idea-58** Add a continuous, controlled operating-system maintenance and update workflow for the Nexus VM.  Regularly check package updates, security advisories, unsupported packages, Ubuntu release support, pending reboot state, disk health, filesystem usage, failed services, timer failures, certificate expiry, clock synchronization, and recent system errors. Produce a concise maintenance report and distinguish observation, safe routine maintenance, security updates, disruptive upgrades, and reboot-required changes.  Use maintenance windows, package and cost limits, pre-change health checks, recoverable snapshots or equivalent rollback where appropriate, post-change service/API/SSH checks, and structured logs. Never perform a distribution upgrade, kernel transition, mass package removal, firewall/network change, reboot, or change that could remove remote access without explicit approval and a tested recovery path. Start in report-only mode, then allow narrowly approved security updates, and consider broader automation only after repeated successful measured runs. `new`
  - **create:** P8
- **idea-59** Add an authenticated "Sync Inbox now" action so users do not need to wait for the one-minute mailbox timer.  When the Inbox refresh button is used, first trigger one safe mailbox synchronization cycle: acquire the existing mailbox lock, fetch/pull new Git mailbox messages using the existing guarded workflow, process eligible messages once, and then refresh the Inbox/Outbox/task views. Reuse the timer's implementation rather than duplicating processing logic.  Expose a small authenticated POST endpoint with rate limiting, a short timeout, structured status such as processed_count and already_running, and sanitized errors. Never start a second processor while the timer or another manual sync holds the lock. Do not run when the main working tree is unsafe. Make the button show syncing/succeeded/already-running/failed states and remain mobile-friendly. Keep the periodic timer as a fallback and add concurrency, idempotency, authentication, dirty-tree, pull-failure, and UI tests. `new`
  - **drop:** Outdated: the signed GitHub webhook (`POST /mailbox/webhook/github` in `api.py`) already starts processing as soon as `inbox/` changes, so nobody waits for the timer.
- **idea-64** Evaluate whether a small local LLM can run continuously on the main VM as Nexus's low-cost decision layer: selecting the next action, classifying messages, choosing tools or larger models, and recognizing when human help is needed. Compare it against rule-based routing for decision quality, runtime, memory usage, energy/resource consumption, and avoided API cost. `new`
  - **drop:** Done: the local llama model has been the controller's decision layer (one shared job path, D-019; model comparison in #302), and #337 moved the controller's judgement to Azure because the local model invented reasons, keeping it as fallback.

### Inbox

- **idea-66** Testausgaben tokenoptimiert reduzieren Der Test-Runner soll standardmäßig möglichst kompakte Ausgaben erzeugen. Bei erfolgreichen Tests reichen Status, Anzahl der Tests und Laufzeit. Bei Fehlern sollen nur relevante Fehlermeldungen und Stacktraces erhalten bleiben. Ziel ist, gespeicherte und später an LLMs übergebene Testausgaben deutlich zu verkleinern, ohne Diagnoseinformationen zu verlieren. Messbar: Output-Größe/Tokens vor und nach der Änderung vergleichen. `new`
  - **drop:** Done: the verification runner strips the per-test lines back to `-q` output plus one line per failing test and caps it (`modules/experiment_service/service.py`, `modules/test_progress.py`).
- **idea-67** Tool-Ausgaben als Teil des Token-Budgets behandeln Alle Tools von Nexus sollten berücksichtigen, dass ihre Ausgaben später in einen LLM-Context gelangen können. Lange Logs, Git-Ausgaben, Testresultate, API-Antworten usw. sollen deshalb möglichst gefiltert, zusammengefasst oder begrenzt werden. Vollständige Rohdaten können bei Bedarf separat gespeichert und gezielt abgerufen werden. Grundprinzip: Nicht möglichst viele Daten an das LLM geben, sondern möglichst viel relevante Information pro Toke `new`
  - **drop:** Duplicate of open issues #232 (staged view for large tool results) and #230 (prompt budget).
- **idea-68** Ideen eingabefeld auf der Website ist zu klein `new`
  - **create:** P9
