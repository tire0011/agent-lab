# Project History

This document is durable project memory for the **distributed self-improving LLM agents** project and agents such as Nexus. It complements `PROJECT.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `IDEAS.md`, and `COSTS.md`. It is not a fixed roadmap: it preserves why the system evolved, what was learned, and which questions remain open.

## Research question

How far can an agent system with a limited budget independently investigate, measure, improve, and extend its own capabilities?

The project deliberately avoids dependence on company/proprietary code. Improvements should be measured against previous states with attention to quality, cost, token use, runtime, and resource consumption.

## Guiding principles

- Start small; build, run, measure, understand, then extend.
- Prefer cheap, local, open-source, and reusable solutions before custom or paid infrastructure.
- Keep experiments reversible and compare improvements against a baseline.
- Allow models, tools, architecture, prompts, and strategies to change when evidence supports it.
- Give agents meaningful freedom while protecting credentials, systems, accounts, infrastructure, budget, and third parties.
- Use versioning, logging, limits, approvals, and rollback where appropriate.
- Treat safety, observability, and risk management as improvable capabilities rather than static restrictions.

## Development timeline

### 1. Initial idea: a self-improving agent under a real budget

The project began with the idea of an independent agent that continuously improves more than prompts: tools, context, memory, model selection, evaluation, compression, orchestration, and eventually useful external work. Early investigation covered Azure credits, local/cloud models, cheap providers, MCP/RAG, fine-tuning/quantization, and multi-agent approaches.

A recurring conclusion was that continuous autonomy should not depend on expensive external API calls. Small local models therefore remain important for low-cost reasoning, routing, context preparation, and routine work.

### 2. Azure VM as the persistent laboratory

A small Ubuntu VM became Nexus's always-on home because it provides predictable Linux tooling, continuous operation independent of a workstation, access to Azure credits, and a controlled place for APIs, schedules, experiments, logs, and self-modification. Capacity has been increased only when experiments justified it.

### 3. Baseline agent V0

The first deliberately small baseline runs locally and on the Azure VM, can call an Azure-hosted LLM, and writes structured run artifacts. It exists as a measurable reference point; self-improvement is only meaningful relative to a previous state.

### 4. MCP and V1 direction

MCP-style tools were introduced so model reasoning and external capabilities can evolve independently rather than becoming one monolithic process. The architecture remains intentionally replaceable.

### 5. Web API and browser control surface

FastAPI and a small web UI evolved from simple health/status access into a remote operational surface for problems, ideas, chat, Codex tasks, experiment state, mailbox approvals, activity, diagnostics, and sanitized logs. A core design rule is to expose enough evidence to understand Nexus without publicly leaking raw prompts, source code, unrestricted logs, paths, or secrets.

### 6. Controlled coding through Codex

Codex CLI became the first controlled implementation path. Important lessons were that CLI flags and sandbox combinations must be tested, read-only analysis and workspace-write implementation need different boundaries, task IDs/status must survive connection failures, and timeout/working-directory constraints must be enforced server-side.

### 7. ExperimentService and measurable self-modification

`ExperimentService` turned improvement ideas into isolated experiments with research/selection, implementation, verification, and terminal states such as success, failure, rejection, timeout, or `needs_review`. Git worktrees isolate changes; verification uses allowlisted commands; risky actions are bounded.

The key conceptual step was separating **proposing a change** from **adopting a change**. A self-improving system must be able to reject its own bad changes.

### 8. Better selection and diagnostics

Weak or placeholder experiment selections led to tighter validation requiring meaningful affected files and implementation scope. Selection/reformat failures became visible through diagnostics instead of silently disappearing. Observability is treated as part of intelligence: failures that cannot be explained cannot be systematically improved.

### 9. Human approval loop

Mailbox tasks initially gained a human approval gate before autonomous code modification:

`message -> pending approval -> approved -> submitted -> running -> result`

This was intentionally treated as an evolvable boundary rather than permanent architecture; it was later removed after the trusted development path matured (see section 35).

### 10. Git-backed Nexus mailbox

A Git-backed inbox/outbox created a simple, human-readable, versioned protocol between humans, ChatGPT, Nexus, and future agents. It also exposed an important systems lesson: communication commits have a different write pattern from source-code integration and can create non-fast-forward/contention problems.

### 11. Public activity and diagnostics

Sanitized activity/diagnostic APIs expose lifecycle timing, changed filenames, test state, bounded messages, usage, estimated cost where available, and deployment revision. Nexus should increasingly be evaluated by evidence of what it did, what it cost, and whether the outcome improved measurable behavior.

### 12. Operation chains

The system began moving from one-shot operations toward sequential operation chains. Integration conflicts when `origin/main` moved during adoption correctly produced `needs_review` rather than forcing a merge, validating conservative adoption behavior.

### 13. 2026-08-15: code/mailbox repository split

The communication/source split was implemented. `/agent` is the source repository and `/var/lib/nexus/mailbox-repo` the mailbox repository; compatibility with the older layout remained temporarily. Cross-repository regression coverage included an end-to-end two-repository mailbox test.

**Lesson:** communication state and source-integration state should be isolated when their Git histories interfere.

### 14. 2026-08-15: crash-safe outbox publication

Task and operation-chain results use a persistent `OutboxPublisher` lifecycle such as:

`pending_write -> written -> committing -> committed -> pushed`

Intent is persisted before the atomic file write; publication is serialized; recovery retries unfinished work; a recorded commit SHA is reused for push retry. Git diagnostics are redacted.

**Lesson:** asynchronous communication requires idempotent delivery semantics; writing an outbox file is not the same as reliably delivering it.

### 15. 2026-08-15: event-driven mailbox wake-up

A signed GitHub webhook adapter was merged for mailbox changes, sharing the same single-poll processor as the timer-based path. The timer remains a fallback rather than the only wake-up mechanism.

**Lesson:** event-driven wake-up can reduce latency without removing the simpler recovery path.

### 16. 2026-08-16: single-call experiment pipeline

The normal Codex experiment path was reduced from roughly **3-5 LLM calls to one implementation call**. The model receives the raw goal and investigates/plans/implements/tests within one call; safety checks remain before execution and implementation quality/verification remain after it.

A regression showed that explicit tests-only goals need separate classification: legitimate test-only experiments may succeed, while ordinary implementation goals that modify only tests still go to `needs_review`.

The key research trade-off is measurable: **how much cost/token reduction is gained, and does implementation quality remain acceptable?**

### 17. 2026-08-16: token-efficient verification with bounded failure evidence

Default `unittest` verification uses quiet mode (`-q`). Success keeps only useful summary output; failures retain a bounded/redacted tail. Focused regression tests cover truncation, redaction, API/operation/UI behavior, omission on success, and safe rendering.

**Lesson:** tool output should be shaped for its consumer—compact on success, minimal actionable evidence on failure.

### 18. 2026-08-17 to 2026-08-19: local-first routing developed and validated

Repeated external-provider quota pressure strengthened the local-first design: keep one small local model available continuously for routing, context gathering, planning, and small/medium tasks; escalate only when a stronger external provider is justified.

Execution is deliberately **sequential**: one model/provider active at a time, with roles such as router, context/research, planner, developer, and reviewer/tester. This bounds RAM, concurrency, token use, and causal complexity.

PR #21 evolved through real-VM testing and review. Important corrections included role/provider separation, model-agnostic configuration, deterministic repo context before expensive delegation, clean-worktree handling, read-only verification behavior, real Codex delegation, actual-provider accounting, and objective verification through real tests and `git diff`.

The preferred default became **Qwen2.5-Coder-7B-Instruct Q4_K_M** via native `llama.cpp`/`llama-server`, while the orchestration layer remains model-agnostic.

### 19. 2026-08-18: B2ms resource envelope for persistent local inference

The Azure VM was resized to **Standard_B2ms: 2 vCPUs and 8 GiB RAM**. Conservative local-server defaults target roughly 8k context, one parallel slot, two CPU threads, local-only binding, and the `agent` service user.

Real deployment exposed a missing OpenBLAS build dependency (`pkg-config`), which was fixed before merge.

**Lesson:** installation scripts must be validated on the actual target environment, not only through unit tests or static review.

### 20. 2026-08-18: self-healing operations adopted on main

PR #22 added authenticated `/operations/*` capabilities for bounded Git operations, service status/restart, log inspection, diagnostics/self-healing, local-model status/testing, and restricted configuration operations. Invocations are centrally dispatched and persisted in an operation log.

Review tightened stale-lock cleanup, path/symlink boundaries, service allowlists, configuration output, recursive redaction, and safety tests.

**Lesson:** increasing autonomy should come with narrower, testable capabilities and auditability—not unrestricted shell access.

### 21. 2026-08-19: local-first multi-provider orchestration adopted on main

PR #21 was merged. Nexus gained a model-agnostic sequential pipeline:

`Router -> Context Gatherer -> Planner -> Developer -> real-tool Verification -> Reviewer`

The persistent local model is the default low-cost decision layer, while Azure OpenAI and Codex are escalation providers when configured. No quality/latency/cost benchmark has yet established that the current Qwen model is optimal for B2ms; the merge adopted the capability architecture, not a permanent model choice.

### 22. 2026-08-20: read-only MCP HTTP adapter adopted on main

PR #27 added a Streamable HTTP MCP surface at `/mcp/`, reusing existing FastAPI/service logic and API-key authentication. It exposes compact read-only inspection tools and deliberately excludes write endpoints, shell-backed operations, webhooks, and new filesystem capabilities.

**Lesson:** MCP is most useful here as a thin capability adapter over tested domain services; interoperability does not require duplicating business logic.

### 23. 2026-08-20: bootstrap MCP write capabilities adopted on main

PR #29 added write-capable MCP bootstrap tools behind `MCP_WRITE_ENABLED=true`, including Git pull, service status/restart, bounded tests, experiment start, local-router execution, and a temporary shell tool. Shell execution remains time/output bounded, redacted, audited, and fails closed when reliable process confinement cannot be established.

**Lesson:** temporary broad bootstrap capability can remain testable and observable when explicitly gated and built on existing operations.

### 24. 2026-08-20: API dependency synchronization became automatic

A real deployment failure exposed stale Python dependencies after source updates. PR #32 added one reusable dependency-sync helper for `requirements-api.txt` and wired it into service startup, refresh/deploy, and operations-driven Git pull paths. Dependency installation failure prevents uvicorn from starting and remains visible in the journal.

**Lesson:** source deployment and runtime dependency state are one atomic operational concern.

### 25. 2026-08-21: secret-policy false positive for LLM token metrics fixed

An experiment was rejected because `output tokens` was interpreted as a credential action. Issue #33/PR #35 introduced a narrow context-specific exception for LLM token metrics while credential-qualified access/auth/API/bearer token actions remain rejected.

**Lesson:** safety-policy quality must include false-positive measurement. Narrow semantic exceptions are preferable to globally weakening protective vocabulary.

### 26. 2026-08-21: trusted development mailbox became a command interface

PR #34 added a mailbox `command` message type and optional task auto-approval in development mode. PR #36 then removed the development-mode execution gate for command messages in the trusted development setup. Timeout/output/redaction/environment protections remain.

This turns the mailbox from a typed request channel into a practical remote development/control interface during the experimental phase.

**Lesson:** trusted-channel policy and command-content policy are separate design choices.

### 27. 2026-08-21 to 2026-08-22: mailbox Git synchronization became self-healing for normal divergence

The separate mailbox repository exposed a deadlock pattern: Nexus could have a local outbox commit while a new inbox commit arrived remotely. A push-first strategy followed by `pull --ff-only` could then fail in both directions because the histories had diverged.

PR #38 replaced this path with a bounded **fetch -> rebase onto upstream -> push** loop. The working tree must be clean first; push races retry up to a small bound; a real rebase conflict triggers `rebase --abort` and fails safely; force-push/reset/discard are not used. A real-Git regression test reproduces simultaneous local outbox and remote inbox commits and verifies both survive and the histories converge. The timer-triggered mailbox oneshot is also classified as healthy when it completed successfully even though systemd reports it as inactive/dead afterward.

**Lesson:** append-oriented distributed Git writers need reconciliation semantics, not only push/pull commands. A safe rebase retry loop is sufficient for the normal non-overlapping inbox/outbox case while preserving explicit failure for real conflicts.

### 28. 2026-08-22: verification timeout became a first-class outcome

Issue #39 changed verification semantics so a subprocess timeout is no longer folded into a normal failed-test/`needs_review` path with `exit_code=None`. Nexus now records a distinct `test_timeout` event and terminal timed-out state while real non-zero test failures remain unchanged.

**Lesson:** different failure classes must remain distinguishable if the system is expected to learn different responses to them.

### 29. 2026-08-22: Nexus learned and persisted an adaptive verification timeout

Issue #40 / PR #45 introduced the first small persisted runtime adaptation based directly on an observed failure class. Verification starts at 300 seconds; after `test_timeout`, the next timeout is increased by a configurable factor (currently 1.5) up to a 1,800-second cap. The value is stored in SQLite `service_settings`, survives process restarts, and is reused by future experiments. Successful or ordinary failing tests do not increase it. The full suite reported **905 tests, OK** after the change.

This is deliberately simple rather than an ML mechanism, but it is an important self-improvement milestone: Nexus observes a repeated operational limit, changes one bounded parameter, persists the change, and reuses it automatically.

**Lesson:** useful self-improvement can begin with small evidence-driven parameter adaptation before attempting complex self-modifying policies.

### 30. 2026-08-22: GitHub and MCP emerged as the next durable coordination/control-plane direction

Several new issues define a coherent next stage rather than isolated features:

- **#41:** integrate the official GitHub MCP server so Nexus can work directly with Issues and repository metadata; migrate still-open local todos/problems once, then use GitHub Issues as the durable shared work list while SQLite remains runtime/experiment state.
- **#42:** publish experiment branches to GitHub and automatically create/reuse Draft PRs for `needs_review`, making remote human review possible without VM access.
- **#43:** post concise experiment/task progress to the associated GitHub Issue at meaningful state transitions, with experiment IDs and PR/result references, while detailed logs stay in Nexus.
- **#44:** experiment with a **global MCP gateway** exposed as the single MCP endpoint to agents. A cheap local LLM would route requests to specialized MCPs/tools, reducing tool-schema context in stronger models and providing a natural place for discovery, logging, permissions, result compression, and detection of missing capabilities. If a useful capability is missing, the gateway could create a structured issue/proposal and later drive small self-extension experiments.

None of these issues should be treated as adopted architecture yet. The first global-MCP experiment should stay small: 2-3 capabilities, direct selection versus gateway routing, measuring routing accuracy, task success, main-model tool-schema tokens, local-model tokens, latency, cost, and wrong-tool failures.

**Research question:** can a cheap local LLM act as an effective **tool-control plane** for stronger models, reducing MCP/context complexity while allowing the system to discover and expand missing capabilities over time?

### 31. 2026-08-22 to 2026-08-24: persistent local-LLM controller adopted

A real resume request exposed a deeper orchestration flaw: Nexus could understand that an existing experiment should be continued yet still create a new experiment/worktree because workflow selection happened too late. The first fix (PR #47) added local-LLM intent classification before experiment creation, but this was still too shallow for the intended architecture.

Issue #46 was therefore reframed around a stronger principle: **the local Nexus model should remain the high-level controller, not merely classify a task once and hand control to a fixed pipeline.** PR #48 implemented a bounded ReAct-style loop in which the local model chooses a tool or final action, receives the real observation, and decides again.

The adopted controller unifies natural-language sources such as mailbox tasks, GitHub issues supplied by an adapter, ideas, and follow-up discussion replies. Durable SQLite sessions preserve source identity and task/discussion history. Its tool surface includes existing Nexus operations, a bounded `memory_search` over SQLite knowledge/task history plus project documents, and `delegate` calls to a local worker, read-only Codex, or Azure OpenAI when available. Delegation returns an observation to the local controller rather than transferring ownership of the task.

Valid final outcomes include `new_experiment`, `resume_experiment`, `command`, `answer`, `clarification`, and `missing_capability`. Missing capabilities become structured requirements instead of being forced through an unrelated workflow; clarification can pause a persistent session and resume it later. Resume behavior is explicit: queued experiments reuse the same ID, verification-related states reuse the existing worktree and rerun verification without implementation, while ambiguous lifecycle states are rejected rather than guessed.

PR #48 was merged to `main` on 2026-08-24 and Issue #46 was closed as completed. Before adoption, focused controller/mailbox/local-LLM-job tests passed on the real Nexus VM, and the same persistent task session completed an end-to-end Laguna run without creating an experiment. The controller executed `memory_search`, then `git_status`, and produced a final successful outbox response.

**Lesson:** intent classification is not orchestration. If the local model is meant to be the agent, control should return to it after tool and delegate observations so it can decide the next step from evidence.

### 32. 2026-08-23 to 2026-08-24: long local inference became persistent asynchronous work

Real Laguna controller turns on the B2ms VM took roughly **7.5 to 14 minutes**; a synchronous controller request had already exceeded 90 seconds and later hit an arbitrary 600-second HTTP read timeout while `llama-server` itself remained healthy. This showed that inference lifetime and controller-request lifetime must not be the same thing.

Nexus now persists serialized local inference as SQLite jobs with durable states such as `queued`, `running`, `succeeded`, `failed`, and `cancelled`, while the Nexus task session can wait in `waiting_for_llm`. The controller later resumes the same session from the stored result instead of holding one HTTP request open or creating a replacement task/experiment. This keeps the mailbox/control loop responsive during slow local reasoning and makes restart recovery possible.

Issue #49 remains the explicit research/measurement surface for this architecture, including job-level runtime/token metrics, watchdog/cancellation behavior, restart recovery, and comparison with the former synchronous baseline.

**Lesson:** slow local inference is normal work, not necessarily a hung request. Persist its identity and lifecycle instead of guessing a short transport timeout.

### 33. 2026-08-24: mailbox observability and repository separation were tightened

PR #51 added focused, content-free diagnostics around the shared mailbox poll runner so webhook- and timer-triggered runs can be distinguished in journald. Logs now capture bounded poll start/end state, thread/run identity, fetch/rebase/push attempts, processed-message count, and safe `status:basename` summaries without mailbox bodies, command output, secrets, or repository paths. Instrumentation also exposed a stale `pull_failed` versus `sync_failed` status mismatch for follow-up.

PR #52 removed the obsolete `communication/` inbox/outbox tree from `agent-lab` after the separate `agent-lab-mailbox` repository became authoritative. Git history preserves the old data; source and communication repositories no longer pretend to be dual active locations.

**Lesson:** once an architectural migration is complete, remove compatibility state that can mislead operators and agents; diagnostics should identify lifecycle failures without exposing payloads.

### 34. 2026-08-25: experiment adoption and normal verification now share one runner

Nexus refactored experiment verification and adoption/integration verification to use the same isolated verification runner. Adoption now uses the same persisted adaptive timeout, timeout classification, isolated test environment, duration/output handling, and future-timeout increase behavior as normal experiment verification.

**Lesson:** adoption is part of the same evidence pipeline as experimentation. Duplicated verification implementations drift and can give different answers for the same code.

### 35. 2026-08-25: manual mailbox task approval was removed

The trusted development workflow no longer requires the user to press an approval button after sending a task. Mailbox task messages are immediately approved and handed to the existing worker/controller; the UI approval affordance was removed. The trusted command channel remains bounded by its existing timeout, output, redaction, environment, and self-disruption protections.

This is a deliberate autonomy-boundary change rather than a generic claim that all future inputs should be trusted. The separate mailbox repository, durable sessions, controller decisions, experiment isolation, verification, and operational safety mechanisms now carry more of the control burden that the original human gate provided.

**Lesson:** human approval gates are useful scaffolding, but once a trusted channel has stronger observability, persistence, verification, and recovery semantics, removing redundant manual steps can improve fire-and-forget operation without requiring unrestricted execution everywhere.

## What Nexus currently learns

Nexus accumulates durable information from:

- code and tests,
- project/idea/decision/history documents,
- SQLite experiment, controller/session, local-LLM-job, operation, and small adaptive service state,
- mailbox `context` knowledge,
- experiment outcomes, diagnostics, activity, usage, and cost data where available.

It does **not automatically inherit the full reasoning history from ChatGPT conversations**. `PROJECT_HISTORY.md` preserves durable decisions, rejected alternatives, cost concerns, failures, and long-running research directions that would otherwise be lost.

## Recommended use as Nexus project memory

Do not inject this entire file into every model call. Compare at least:

1. no history context,
2. a short generated history summary,
3. retrieval of only relevant history sections.

Measure relevance of proposed ideas, duplicate work, consistency with previous decisions, token use, latency, and cost. Project memory should itself be an experiment.

## Current architectural picture

Current `main` includes:

- persistent Standard_B2ms Azure VM,
- persistent local `llama.cpp` model capability plus model-agnostic routing and a persistent local-LLM task controller,
- durable serialized local-inference jobs that allow the same Nexus session to wait/resume across long model turns,
- Azure OpenAI/Codex escalation paths with provider accounting,
- single-call Codex experiment execution,
- experiment orchestration, worktree isolation, quality gating, shared real-tool verification/adoption runner, distinct timeout state, and persisted adaptive verification timeout,
- SQLite state and durable project/idea/decision/history documents,
- separate Git-backed mailbox repository with crash-safe outbox publication and bounded fetch/rebase/push synchronization for normal concurrent writers,
- signed GitHub webhook wake-up with timer fallback and bounded content-free poll diagnostics,
- API/web UI, public/authenticated diagnostics, and compact failure evidence,
- authenticated, audited, allowlisted self-healing/operations capabilities,
- authenticated MCP HTTP surface with read-only inspection plus explicitly enabled bootstrap write tools,
- trusted-development mailbox task auto-execution plus `command` execution with bounded/redacted output,
- automatic API dependency synchronization across service startup and deploy paths,
- tests and policy boundaries.

The persistent local-controller design is adopted on `main`. The GitHub-Issue workflow and global MCP gateway remain research/development directions rather than adopted architecture.

## Important lessons so far

1. **Autonomy requires observability.** A result without evidence is hard to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether to adopt it.
3. **Different state types may need different repositories.** Mailbox traffic and source integration had incompatible Git write patterns.
4. **Communication needs delivery semantics.** Persistence, locking, retry, recovery, idempotence, and safe reconciliation matter.
5. **Cheap reasoning matters.** Local/small models should handle work that does not justify an expensive provider.
6. **Context is a resource.** Retrieve or compress relevant memory instead of blindly increasing context.
7. **Failures are research data.** CLI mistakes, merge conflicts, stale assumptions, deployment failures, rejected experiments, and timeouts should influence the next iteration.
8. **Optimize orchestration before buying more capacity.** Fewer LLM calls and quieter tools can reduce cost before changing models.
9. **Real tools are the source of truth for verification.** LLM reviewers may interpret evidence but should not invent pass/fail state.
10. **Separate capability architecture from model identity.** The routing layer should survive model replacement.
11. **Target-environment validation matters.** Deployment scripts can fail despite clean architectural review and unit tests.
12. **Safety can enable more autonomy.** Narrow boundaries, redaction, liveness checks, and audit logs can support stronger capabilities.
13. **Adapters should reuse domain logic.** MCP/API surfaces should expose existing tested capabilities rather than fork behavior.
14. **Keep the loop small.** Build, run, measure, understand, then choose the next improvement.
15. **Deployment includes dependencies.** Pulling source without synchronizing the runtime environment is incomplete.
16. **Safety policies need precision, not only strictness.** False positives can block useful experiments; narrow contextual guards preserve protection better than broad exemptions.
17. **Capability trust is layered.** A trusted development channel can allow broad commands while runtime/output/secrets remain bounded.
18. **Failure taxonomy enables adaptation.** `test_timeout` must be distinct from `test_failed` if Nexus should react differently.
19. **Simple persisted adaptation counts.** Automatically increasing a bounded timeout after measured timeouts is a real self-improvement loop, even without another LLM call.
20. **A control plane may reduce model context.** Tool discovery/routing is itself a candidate task for a cheap local model instead of exposing every tool schema to every stronger model.
21. **Routing is not control.** A persistent agentic controller should regain control after tool/delegate observations rather than making one early classification and disappearing behind a fixed workflow.
22. **Long inference should be durable work.** Persist model jobs and resume the owning task instead of coupling slow inference to one HTTP timeout.
23. **Verification semantics should be shared.** Experiment and adoption paths should use one runner so timeout, isolation, and evidence rules cannot drift.
24. **Approval gates are scaffolding, not the architecture.** In a trusted channel, stronger persistence, verification, observability, and recovery can eventually replace redundant manual clicks.

## Open research directions

- project-memory retrieval from this history,
- measured token/cost/quality impact of the single-call pipeline and compact tool output,
- benchmark the current local model and alternative quantized models inside the B2ms envelope,
- measure real controller task success, tool choice, memory use, bounded delegation, clarification/resume, missing-capability behavior, and failure recovery over a larger task sample,
- measure persistent local-LLM jobs: queue time, generation time, total runtime, token rate, restart recovery, cancellation/watchdog behavior, and resource use versus the synchronous baseline,
- test what the merged local model/controller can accomplish on genuinely small and medium tasks before granting broader autonomous decision authority,
- route by task difficulty, expected value, provider limits, historical quality, and resource cost,
- measure how much local context preparation reduces external-provider tokens,
- evaluate whether merged self-healing operations materially reduce human intervention without increasing incidents,
- evaluate MCP as the standard integration/control path and determine which temporary bootstrap write capabilities can later be replaced by narrower operations,
- integrate GitHub Issues/PRs as the durable remote work/review surface and measure whether it reduces manual coordination,
- prototype the global MCP gateway with only 2-3 tools and measure routing accuracy, schema-token reduction, latency, total cost, and failures,
- investigate missing-capability detection and structured issue creation before any automatic tool generation,
- measure reliability and safety after removing manual mailbox task approval,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- context compression/retrieval strategies,
- quantization/fine-tuning where evidence justifies it,
- adaptive two-pass context research / richer repo-map approaches when small context is insufficient,
- multi-agent specialization only when measurable benefit exceeds complexity,
- controlled exploration of useful products, services, public open-source work, and eventually partial cost recovery.

## Current next-step candidates

1. **Controller/autonomy measurement:** run a representative sample of small and medium real tasks through the adopted persistent controller and measure completion rate, tool loops, clarification/resume, external escalation, human intervention, and failures.
2. **Persistent local-LLM-job measurement:** quantify Laguna/local-model queue/runtime/token behavior and verify task recovery across API/controller restarts; decide whether polling remains sufficient.
3. **Local-model benchmark:** compare the current local model with at least one plausible alternative under identical B2ms limits.
4. **Single-call before/after measurement:** repeat a representative early experiment and compare quality, tokens, runtime, and cost with the historical multi-stage path.
5. **Project-memory experiment:** expose only relevant history to Nexus and measure whether idea selection becomes more relevant and less repetitive.
6. **Operational-autonomy measurement:** measure successful self-recovery, human interventions avoided, and safety/false-positive incidents using the merged operations/MCP/mailbox control surfaces, including the no-manual-approval path.
7. **GitHub work-surface experiment:** integrate the official GitHub MCP, migrate only open actionable work, and test whether issue/branch/PR visibility improves fire-and-forget development and review.
8. **Global MCP gateway experiment:** route 2-3 known tool types through the local model and compare directly against main-model tool selection before building dynamic discovery or self-extension.
9. **Adaptive-runtime measurement:** observe whether the learned verification timeout prevents repeated false timeouts without masking genuinely hung tests or creating excessive runtime/cost.

Choose the next step by expected learning value, effort, risk, and measurable benefit—not architectural completeness.

## Maintenance rule

Update this document only when development changes the project's understanding of itself: major experiments, significant failures/lessons, architectural changes, persistent capabilities, autonomy/safety boundary changes, infrastructure changes, or meaningful research-direction changes. Routine implementation details belong in commits, issues, and specialized documentation.