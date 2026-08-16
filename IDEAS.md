# IDEAS.md

An open, continuously extensible idea repository for agent-lab.

This is **not** a task list, a roadmap, or a commitment. An idea being
listed here does not mean it will be built, and its absence does not mean
it is rejected. Ideas exist to be explored, tested, adopted, or dropped as
evidence comes in.

For a record of what has actually been *built and verified*, see
`ARCHITECTURE.md` ("Current architecture (implemented)") and
`DECISIONS.md` (accepted/deferred decisions). This file is intentionally
forward-looking and speculative; that historical record is not.

## Anyone can add ideas, any time

Humans and agents (Nexus, Codex, Copilot, or others) may append a new idea
to this file at any time, in any section, without prior approval. New
ideas do not need a priority, an owner, or a fully worked-out plan — a
short description is enough. Keep each idea to one line so it stays easy
to scan and to parse; expand in prose elsewhere only once an idea moves
into `exploring` or later.

## States

Each idea may carry a lightweight state, written as a trailing
`` `state` `` marker. A state is optional — an idea with no marker is
implicitly `new`.

- `new` — captured, not yet looked at.
- `exploring` — actively being investigated or discussed, no commitment yet.
- `selected` — chosen as worth a concrete attempt, not yet built/tested.
- `tested` — a version exists and has been run/measured somehow.
- `adopted` — kept as part of the system based on evidence.
- `rejected` — tried or considered and deliberately not pursued (a reason
  is helpful but not required).

Ideas are not required to have a priority. Ordering within a section is
not a significance ranking.

## Ideas

### V1 / MCP agent

- Finish and test the MCP-based V1 agent path; define its scope (capabilities and limits) explicitly. `exploring`
- Add tests for MCP path success/failure cases. `new`
- Validate V1 behavior on both local machine and VM. `new`

### Benchmarking V0 vs V1

- Create a fixed benchmark task set with a quality rubric/scorer. `new`
- Track tokens, runtime, estimated cost, and resource usage per benchmark run. `new`
- Produce a side-by-side V0/V1 benchmark report. `new`

### Model/provider abstraction

- Define a provider/model interface; implement an Azure adapter first, then at least one non-Azure adapter. `new`
- Evaluate cheaper Azure model options and at least one additional provider for quality/cost/runtime tradeoff. `new`

### Evaluation gate before self-modification

- Define pass/fail criteria before any self-modification is allowed. `new`
- Block changes that fail evaluation; log outcomes in the structured store. `new`

### Controlled self-improvement loop

- Agent reviews previous runs and identifies one measurable weakness. `new`
- Agent proposes one bounded improvement before being allowed to modify itself automatically. `new`
- Evaluate each candidate against a fixed baseline; accept only changes that pass quality/cost/runtime gates, otherwise roll back. `new`
- Persist experiment hypothesis, change, metrics, result, decision, and cost. `new`
- Fix the experiment diff collector to also detect and include new, untracked files, so newly created files show up in the stored diff/diff_stat and are available for evaluation and reports. `new`

### Azure GPU worker VM

- Evaluate a separate on-demand Azure GPU VM as Nexus's model-compute worker: start it only for local-LLM inference, benchmarking, fine-tuning, or other GPU experiments; measure model quality, tokens/second, startup time, VRAM/RAM use, energy/resource use, and total cost against hosted APIs and non-GPU execution; enforce spending limits and automatically stop/deallocate it when idle. `new`

### Test isolation / second VM

- Investigate a second Azure VM as an ephemeral test worker; compare cost of local tests on the main VM vs a disposable test VM for risky changes. `new`
- Preferred flow if adopted: create/start test VM -> fresh checkout -> tests/benchmarks -> return result -> deallocate/delete. `new`
- Don't keep a second VM running 24/7 unless measurements justify it. `new`

### Coding CLI as a replaceable tool

- Let the orchestrator request a code change from a CLI, capture diff/test results, then independently evaluate the result. `exploring`
- Later compare multiple coding CLIs/models by task quality, cost, tokens, and runtime, and route tasks accordingly. `new`

### Codex task API follow-ups

- Add an approval workflow (currently any `queued` task can be run immediately by an authenticated caller). `new`
- Add task cancellation (currently a running task cannot be stopped once started). `new`
- Move execution off the request thread once background execution is deliberately in scope (currently strictly synchronous; a long Codex call blocks the HTTP response). `new`

### Baseline measurement

- Run one reproducible end-to-end baseline task and persist quality, tokens, runtime, estimated cost, and resource usage. `new`
- Keep the VM as the primary persistent home of the agent, repository, memory, orchestration, tests, and CLI tooling. `adopted`

### Web research worker

- Evaluate a self-hosted SearXNG instance as a low-cost search backend. `new`
- Build a small `WebResearchWorker` that searches through SearXNG, fetches a bounded number of result pages, and returns answers with source URLs. `new`
- Add strict request, page-count, download-size, timeout, and concurrency limits; respect robots.txt, provider terms, rate limits, and site access restrictions — do not scrape Google AI Overviews as a production dependency. `new`
- Log search count, fetched pages, tokens, runtime, failures, and estimated cost per research task. `new`
- Compare SearXNG against Gemini Grounding with Google Search on a small fixed question set for answer quality, source quality, freshness, runtime, and total cost. `new`
- When testing Gemini 3.x, account separately for the monthly included Google Search request allowance and the model input/output token charges — one prompt may trigger multiple search requests. `new`

### Memory and self model

- Add a persistent self model (`SELF.json` or SQLite-backed equivalent) containing version, capabilities, known weaknesses, resources, goals, and current constraints. `new`
- Separate memory into current task context, project memory, experience/experiment memory, and self model. `new`
- Load only relevant memory into LLM context instead of continuously growing one large prompt. `new`
- Reuse the existing SQLite store for structured memories and experiment history before adding embeddings/RAG. `new`

### Prompt library and work modes

- Add a versioned prompt library for distinct work modes such as `explore`, `research`, `plan`, `implement`, and `evaluate`. `new`
- Let the orchestrator select the appropriate prompt and permitted tools per task instead of assuming every task is a code change. `new`
- Allow the agent to propose, create, and improve its own prompts and add new work modes when a capability gap is identified. `new`
- Evaluate every prompt change against a baseline for quality, cost, tokens, and runtime before adoption; keep prompt history and automatically roll back regressions or constraint violations. `new`
- Store prompt versions, hypotheses, evaluations, and adoption decisions in SQLite. `new`

### Documentation accuracy

- Periodically re-check that README.md/AGENTS.md describe the actual deployed VM size, API endpoints, clients, and Web UI rather than a stale snapshot. `exploring`

### Communication interface

- Extend the existing Problems system into a persistent two-way communication channel: the agent can report blockers or needs with a concrete question, humans can reply in a problem-specific thread, the agent can read and respond to those replies, and the conversation remains available until the problem is resolved. `new`
- Add experiment, cost, and approval views once the corresponding backend contracts exist. `new`
- Keep communication adapters separate so messenger integrations such as WhatsApp can be added later without changing the agent core. `new`
- Explore using the same communication contract for agent-to-agent communication. `exploring`

### Autonomy stages (framework idea, not a plan of record)

- Level 0: human assigns every task. `adopted`
- Level 1: agent proposes its own next experiment. `new`
- Level 2: agent executes cheap, reversible, bounded experiments automatically. `new`
- Level 3: continuous improvement loop with automatic evaluation and rollback. `new`
- Level 4: architecture/model/resource changes within explicit budgets and safety limits. `new`

### Economic sustainability experiments

- Investigate whether Nexus can generate enough measurable economic value to offset or fund part of its own operating costs: search public repositories and communities for small problems with demonstrated demand, paid open-source bounties, or opportunities for useful tools and services; estimate effort, expected value, implementation risk, and API/cloud cost; then propose one bounded low-risk experiment at a time. Track revenue or verified value generated minus all operating costs as the net project contribution. External communication, account creation, publication, pricing, contracts, payment handling, and spending remain subject to explicit human approval; income is legally received and controlled by the human project owner, who may allocate an agreed portion back to Nexus's budget. `new`

### Later / no timeline

- Azure actual-cost reconciliation against estimated cost. `new`

### Mailbox submissions
- Add a Token Governor that enforces explicit per-phase and per-experiment budgets instead of merely reporting token usage after completion.  Define task-size classes and initial configurable budgets for research, selection, implementation, retries, and total usage. Before each LLM call, check remaining budget. When a soft limit is reached, require context compaction, narrower file retrieval, a cheaper model, or a revised smaller plan. At the hard limit, stop safely with needs_review rather than continuing uncontrolled.  Record tokens by model, call, phase, cached/uncached category when available, and useful output such as accepted changed lines or successful tests. Expose budget, consumed amount, remaining amount, and stop reason in logs and the Web UI. Start in shadow mode to learn realistic limits, then enforce limits for small tasks. Use the million-token Logs experiments as baseline evidence. `new`
- After every completed, failed, rejected, or timed-out task, run a bounded post-task reflection that asks an LLM what could improve Nexus's process, tools, prompts, context selection, tests, safety, cost, runtime, or architecture.  Give the reflection only a compact structured summary of the goal, plan, changed files, lifecycle timings, token usage, test outcome, failure evidence, and final decision; do not resend the full conversation, repository, prompts, diffs, or raw logs. Require at most three concrete evidence-backed suggestions, each with expected benefit, cost, risk, measurement method, and smallest experiment. Allow "no useful improvement found" as a valid result.  Use a strict small token budget and preferably the cheap local decision model when available. Deduplicate suggestions against existing ideas and previous reflections. Store new candidates with provenance and measurable hypotheses, but never implement them automatically merely because the reflecting model suggested them. Track which suggestions are later tested, adopted, rejected, or repeatedly rediscovered so reflection quality itself can be evaluated. `new`
- Evaluate a Context Curator that selects a small evidence-backed set of relevant files, symbols, and line ranges before an expensive coding model receives a task.  Reuse existing open-source approaches before building a custom index. Compare at least: a simple rg plus Python/JavaScript symbol-outline baseline, Aider's Tree-sitter repository-map approach, and Serena's LSP/MCP symbol retrieval. Consider Zoekt only when repository size or multi-repository search justifies its additional service and index cost.  Give each curator a strict context budget, initially 4K or 8K tokens, and return file paths, relevant symbols or ranges, dependency clues, confidence, and reasons. Never include file contents merely because a file is large or central. Let the coding model request bounded context expansion when evidence is insufficient.  Evaluate on frozen representative tasks using relevant-file recall/precision, missing-required-file rate, context tokens, total task tokens, runtime, memory, implementation success, and test results. Compare against the current unconstrained exploration baseline, including the 1,102,844-token Logs task. Adopt only if quality remains stable while total tokens and latency improve. `new`
- Let Nexus continuously study and improve the operating-system environment it runs on, with measurable resource optimization and safe rollback.  Periodically inventory CPU, memory, swap, disk, I/O, processes, systemd services, startup time, installed packages, caches, logs, open ports, and resource pressure. Identify services, packages, timers, caches, and configuration that appear unused or wasteful. First produce evidence-backed proposals with expected savings, dependencies, risks, and rollback steps. Measure a baseline and compare CPU, RAM, disk, boot/service time, reliability, and cost after each approved change.  Never remove or disable components only because they look inactive. Protect SSH, networking, firewall, Git, Python, systemd, logging, time synchronization, certificate handling, agent services, recovery access, and cloud-management components. Require human approval initially for package removal, service disabling, firewall changes, storage cleanup beyond bounded caches, or other system mutations. Apply one small reversible change at a time, verify health afterward, log everything, and automatically roll back when health checks regress. `new`
- Add a continuous, controlled operating-system maintenance and update workflow for the Nexus VM.  Regularly check package updates, security advisories, unsupported packages, Ubuntu release support, pending reboot state, disk health, filesystem usage, failed services, timer failures, certificate expiry, clock synchronization, and recent system errors. Produce a concise maintenance report and distinguish observation, safe routine maintenance, security updates, disruptive upgrades, and reboot-required changes.  Use maintenance windows, package and cost limits, pre-change health checks, recoverable snapshots or equivalent rollback where appropriate, post-change service/API/SSH checks, and structured logs. Never perform a distribution upgrade, kernel transition, mass package removal, firewall/network change, reboot, or change that could remove remote access without explicit approval and a tested recovery path. Start in report-only mode, then allow narrowly approved security updates, and consider broader automation only after repeated successful measured runs. `new`
- Add an authenticated "Sync Inbox now" action so users do not need to wait for the one-minute mailbox timer.  When the Inbox refresh button is used, first trigger one safe mailbox synchronization cycle: acquire the existing mailbox lock, fetch/pull new Git mailbox messages using the existing guarded workflow, process eligible messages once, and then refresh the Inbox/Outbox/task views. Reuse the timer's implementation rather than duplicating processing logic.  Expose a small authenticated POST endpoint with rate limiting, a short timeout, structured status such as processed_count and already_running, and sanitized errors. Never start a second processor while the timer or another manual sync holds the lock. Do not run when the main working tree is unsafe. Make the button show syncing/succeeded/already-running/failed states and remain mobile-friendly. Keep the periodic timer as a fallback and add concurrency, idempotency, authentication, dirty-tree, pull-failure, and UI tests. `new`
- Add a controlled experiment-adoption workflow so successful Nexus experiments can be integrated without manually repeating Git commands.  Initially require explicit human approval through the Web UI or authenticated API. A succeeded experiment should become ready_for_adoption and expose a sanitized diff summary, changed files, test result, runtime, token usage, and risks.  On adoption, use a repository-wide Git lock and verify that the experiment succeeded, tests passed, its worktree and branch still match the recorded experiment, the main working tree is clean, and the remote state is known. Create a deterministic commit on the experiment branch, integrate it into a temporary validation worktree based on current main, rerun the full test suite with the isolated verification environment, and only update main when integration tests pass without conflicts. Never resolve conflicts automatically. On conflict, failed tests, concurrent repository changes, or push failure, leave main unchanged and move the experiment to needs_review with complete structured logs and a recoverable retry path.  Keep adoption idempotent and auditable. Add preview, adopt, status, and retry capabilities plus a mobile-friendly Adopt button. Do not enable fully automatic adoption until several manually approved adoptions have been measured and shown safe. `new`
- Add a safe read-only diagnostic request type to the Git-backed mailbox so humans and ChatGPT can inspect Nexus without SSH.  Use predefined allowlisted actions rather than arbitrary shell commands. Initial actions could include git_status, git_diff_stat, git_recent_commits, git_worktrees, experiment_status, experiment_log_tail, service_status, disk_usage, and test_summary.  Execute fixed argument lists with shell disabled, a fixed repository root, validated experiment IDs, short timeouts, bounded output, central redaction, structured logging, and automatic sanitized results in communication/outbox. Never accept free-form commands, paths, environment variables, or unrestricted options from mailbox messages. `new`
- Add a bounded retention policy for the Git-backed mailbox so communication/inbox and communication/outbox remain small and easy to inspect.  Keep at most the newest five processed messages/results visible in each active mailbox folder. Never delete pending, unprocessed, retrying, or otherwise unresolved inbox messages. Preserve traceability through Git history and preferably move older processed entries to an archive before considering permanent deletion. Make retention deterministic, configurable, logged, tested, and safe during concurrent mailbox processing or failed Git pushes. `new`
- Improve the Logs tab auto-refresh behavior so reading logs on desktop or iPhone is not interrupted every ten seconds.  Add an accessible Auto-refresh on/off control, preserve the user's scroll position and expanded log-entry state during refresh, and avoid replacing the rendered DOM when data is unchanged. When the user has scrolled away from the newest entries, do not jump the page; show a small "new log entries available" indicator that lets the user move to them explicitly. Manual refresh must remain available. Keep the behavior mobile-friendly and test success, failure, paused, unchanged-data, scroll-preservation, and new-entry cases. `new`
- Evaluate whether a small local LLM can run continuously on the main VM as Nexus's low-cost decision layer: selecting the next action, classifying messages, choosing tools or larger models, and recognizing when human help is needed. Compare it against rule-based routing for decision quality, runtime, memory usage, energy/resource consumption, and avoided API cost. `new`

### Inbox

- Make Git-backed inbox communication event-driven: when inbox/ changes, GitHub should trigger Nexus via a signed webhook or a path-filtered GitHub Action instead of relying only on polling. Nexus should translate the external notification into a bounded internal event such as inbox_changed, verify authenticity, and avoid executing arbitrary webhook content directly. Later, evaluate a fire-and-forget workflow such as Nexus -> GitHub Cloud Agent -> PR/result -> GitHub event -> Nexus evaluation `new`
- Testausgaben tokenoptimiert reduzieren Der Test-Runner soll standardmäßig möglichst kompakte Ausgaben erzeugen. Bei erfolgreichen Tests reichen Status, Anzahl der Tests und Laufzeit. Bei Fehlern sollen nur relevante Fehlermeldungen und Stacktraces erhalten bleiben. Ziel ist, gespeicherte und später an LLMs übergebene Testausgaben deutlich zu verkleinern, ohne Diagnoseinformationen zu verlieren. Messbar: Output-Größe/Tokens vor und nach der Änderung vergleichen. `new`
- Tool-Ausgaben als Teil des Token-Budgets behandeln Alle Tools von Nexus sollten berücksichtigen, dass ihre Ausgaben später in einen LLM-Context gelangen können. Lange Logs, Git-Ausgaben, Testresultate, API-Antworten usw. sollen deshalb möglichst gefiltert, zusammengefasst oder begrenzt werden. Vollständige Rohdaten können bei Bedarf separat gespeichert und gezielt abgerufen werden. Grundprinzip: Nicht möglichst viele Daten an das LLM geben, sondern möglichst viel relevante Information pro Toke `new`
- Ideen eingabefeld auf der Website ist zu klein `new`
- Wurde ein Experiment übernommen sollte auch gleich ein restart durchgeführt werden damit die Änderungen aktiv werden `new`


## Open unknowns

Unknowns worth tracking without necessarily turning each into an idea above:

- Which evaluation strategy is robust enough for automated gating.
- Which low-cost model mix offers acceptable quality.
- How much autonomy is safe on current infrastructure limits.
- Which VM size gives the best cost/performance for coding CLI workloads.
- Whether an ephemeral second VM is cheaper and safer than testing on the main VM.
- Which memory retrieval strategy remains simple enough while still scaling with accumulated experience.
- Which stable Codex CLI output or event format exposes usage and session metadata reliably.
