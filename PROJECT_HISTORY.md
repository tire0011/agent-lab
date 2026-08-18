# Project History

This document captures the development path of the **distributed self-improving LLM agents** project and is intended as durable project memory for humans and agents such as Nexus.

It complements `PROJECT.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `IDEAS.md`, and `COSTS.md`. It is not a fixed roadmap. The purpose is to preserve why the system evolved as it did, what was learned, and which open questions remain.

## Research question

How far can an agent system with a limited budget independently investigate, measure, improve, and extend its own capabilities?

The project deliberately avoids dependence on company or proprietary code. Improvements should be measurable against the previous state, with attention to quality, cost, token consumption, runtime, and resource usage.

## Guiding principles

- Start small and learn from running systems instead of designing a large architecture up front.
- Prefer cheap, local, open-source, and reusable solutions before building new components or paying for services.
- Measure improvements and keep experiments reversible.
- Allow architecture, models, tools, and strategies to change when evidence supports it.
- Give agents substantial freedom to investigate and propose improvements while protecting credentials, systems, accounts, infrastructure, budget, and third parties.
- Use versioning, logging, limits, approvals, and rollback where appropriate.
- Treat safety and risk management as capabilities that can themselves improve.

## Development timeline

### 1. Initial idea: a self-improving agent under a real budget

The project started from the idea of running an independent agent continuously and letting it improve itself in multiple dimensions rather than only optimizing prompts.

Early investigation covered:

- Azure resources available through monthly Visual Studio subscription credits.
- Local and cloud LLMs.
- Cheap external LLM providers.
- GPU availability and whether a local model could become the agent's own always-available reasoning component.
- Tool use, MCP, RAG/memory, evaluation, model selection, compression, fine-tuning, and multi-agent approaches.
- The longer-term question of whether the system could discover useful products, services, open-source contributions, or other ways to offset its own operating costs.

A recurring conclusion was that the project should not depend permanently on expensive external API calls. Small local models remain an important research direction for low-cost continuous reasoning and action selection.

### 2. Azure VM as the persistent laboratory

A small Ubuntu VM was created as the always-on execution environment.

The VM became the first persistent home of the agent because it provided:

- predictable Linux tooling,
- continuous operation independent of a developer workstation,
- access to Azure credits,
- a place for APIs, scheduled jobs, experiments, and logs,
- and a controlled environment for self-modification experiments.

Memory and VM sizing were adjusted incrementally rather than provisioning expensive hardware immediately.

### 3. Baseline agent V0

The first implementation established a deliberately small baseline agent.

Important properties:

- runs locally and on the Azure VM,
- can call an Azure-hosted LLM,
- writes structured run artifacts,
- provides a baseline against which later versions can be measured.

This baseline matters because self-improvement only becomes meaningful when there is a previous state to compare with.

### 4. MCP and V1 direction

The next architectural direction introduced MCP-style tools so that model reasoning and external capabilities could be separated cleanly.

The working hypothesis was that an agent should not need every capability inside one monolithic process. Tools can evolve independently, and the model can select or compose them.

V1 remains an evolving direction rather than a finished architecture.

### 5. Web API and browser interface

A FastAPI service and small web interface were added so humans and external tools could inspect and interact with the agent remotely.

The API evolved from simple health/status access into interfaces for:

- health and status,
- problems and ideas,
- chat over current project state,
- Codex tasks,
- experiment state,
- mailbox task approvals,
- public activity,
- diagnostics and sanitized logs.

The browser UI became an operational control surface rather than only a demo page.

A key design decision was to expose enough observability to understand what Nexus is doing without exposing secrets, raw prompts, source code, unrestricted logs, or internal paths publicly.

### 6. Controlled coding through Codex

Codex CLI integration was introduced so the system could inspect and modify its own repository through a controlled execution path.

Important lessons included:

- CLI flags and sandbox modes must be tested rather than assumed; an incompatible `--approve-for-me` / `--sandbox` combination initially caused immediate failures.
- Read-only analysis and workspace-write implementation need different execution boundaries.
- Tasks need persistent IDs and status so connection failures do not cause accidental duplicate execution.
- Timeouts and working directories must be enforced by the server, not trusted from clients.

This became the foundation for more autonomous experiments.

### 7. ExperimentService and measurable self-modification

An `ExperimentService` was added to turn improvement ideas into controlled experiments.

The service introduced a lifecycle around self-modification, including phases such as:

- research / selection,
- implementation,
- verification,
- success, failure, rejection, timeout, or manual review.

Changes are isolated in Git worktrees. Tests are executed through an allowlisted command path. Risky operations such as arbitrary installation, credential access, uncontrolled shell execution, direct push/merge, and destructive operations are restricted.

The important conceptual step was separating **proposing a change** from **adopting a change**.

A self-improving system must be able to reject its own bad changes.

### 8. Better selection and diagnostics

Early selection attempts sometimes produced weak or placeholder changes. The selection prompts and validation were tightened so proposed experiments need real affected files and meaningful implementation scope.

Diagnostic information was added for selection and reformatting failures. This made failures inspectable instead of silently appearing as rejected work.

The project increasingly treats observability as part of intelligence: if the system cannot explain why an experiment failed, it cannot systematically improve its experiment process.

### 9. Human approval loop

Because autonomous code modification can have larger consequences than read-only analysis, mailbox tasks gained an explicit human approval state.

The flow became approximately:

`message -> pending approval -> approved -> submitted -> running -> result`

This preserves autonomy for research and implementation while retaining a simple human gate before externally requested changes execute.

The approval boundary is intentionally evolvable: future experiments may measure whether some categories can safely be auto-approved.

### 10. Git-backed Nexus mailbox

A Git-backed inbox/outbox was introduced as a simple asynchronous communication protocol between humans, ChatGPT, Nexus, and potentially future agents.

Message types include ideas, tasks, questions, and context.

This was attractive because it is:

- human-readable,
- versioned,
- inspectable,
- easy for agents and scripts to use,
- independent of a particular messaging vendor.

It also revealed a practical problem: communication commits can interfere with normal source-code Git history and can create push/non-fast-forward conflicts.

An active design idea is therefore to move inbox/outbox communication into a separate repository or otherwise decouple high-frequency agent communication from the source repository.

### 11. Public activity and diagnostics

The website and API gained a sanitized activity model so the operator can observe Nexus remotely.

The system now distinguishes public operational information from sensitive internal details.

Useful observable information includes:

- experiment lifecycle,
- durations,
- changed file names,
- test status,
- bounded diagnostic messages,
- token usage,
- estimated cost where available,
- current service revision and deployment state.

This moves the project toward evidence-based autonomy: Nexus should increasingly be judged by what it did, what it cost, and whether it improved measurable outcomes.

### 12. Operation chains

The agent began evolving beyond isolated one-shot operations toward operation chains that can connect multiple actions.

This is an important transition toward more capable autonomous workflows, but it also exposed integration problems when `origin/main` changed while an experiment was being adopted.

The resulting `needs_review` state is useful rather than a failure of the architecture: the system correctly avoided forcing an unsafe merge when the base branch had moved.

### 13. 2026-08-15: code/mailbox repository split

The earlier communication-repository idea was implemented. Nexus can now use separate repositories for source code and mailbox traffic through `MailboxConfig`, while retaining the legacy single-repository layout for compatibility.

The deployed VM uses `/agent` as the code repository and `/var/lib/nexus/mailbox-repo` as the mailbox repository. In separate-repository mode, inbox/outbox live directly below the mailbox root instead of under `communication/`.

The cutover included path-resolution and cross-repository regression tests, including an end-to-end two-repository mailbox test. This removes routine inbox/outbox commits from the source repository and directly addresses the earlier non-fast-forward/contention problem.

The durable lesson is that **communication state and source-integration state have different write patterns and should be isolated when their Git histories interfere**.

### 14. 2026-08-15: crash-safe outbox publication

Task-approval results and operation-chain results now share a common persistent `OutboxPublisher` lifecycle rather than maintaining separate Git publication logic.

Publication progress is persisted through states such as:

`pending_write -> written -> committing -> committed -> pushed`

The result content and publish intent are stored before the atomic file write, publication is serialized with a repository lock, and pending publication is retried during processor/API recovery. A recorded commit SHA is reused for push retry instead of creating duplicate commits.

This makes result delivery resilient to crashes between database update, file write, commit, and push. Push failures are also handled with stricter URL/credential redaction so Git diagnostics do not leak remote credentials.

A key lesson is that **asynchronous agent communication needs an idempotent, recoverable publication protocol; writing an outbox file is not equivalent to reliably delivering it**.

### 15. 2026-08-16: single-call experiment pipeline

The Codex experiment pipeline was simplified from roughly **3-5 LLM calls per approved experiment to one implementation call**. The earlier research, selection/plan, reformat retry, and policy-replan calls were removed from the normal path; Codex now receives the raw goal and is asked to investigate, plan, implement, and add tests in one call.

Safety checks were moved to the raw goal before the Codex call, while the existing implementation quality gate and verification remain after the change. Legacy database fields and state transitions were retained where useful for compatibility.

This materially changes the project's cost model: orchestration complexity and prompt/token overhead are reduced before trying cheaper models. The trade-off is now explicit and measurable: **does a cheaper single-call pipeline preserve enough implementation quality compared with the former multi-stage reasoning pipeline?**

A follow-up regression exposed an evaluation edge case: explicit tests-only goals were incorrectly treated as ordinary implementation work when no plan object existed. Goal-based tests-only classification was added so legitimate test-only experiments can succeed while ordinary implementation goals with only test-file changes still go to `needs_review`. A later stale regression expectation was also corrected to preserve this boundary.

### 16. 2026-08-16: token-efficient verification with bounded failure evidence

Default `unittest` verification now runs in quiet mode (`-q`). Successful runs retain the final `Ran ...` / `OK` summary without progress-dot noise, while failed runs retain traceback information and the final failure summary. Explicit custom test commands are not rewritten.

To avoid trading token efficiency for diagnosability, failed experiments gained a bounded, redacted failure tail. The implementation limits evidence to the final 30 lines, at most 500 characters per line and 4,000 characters total, with existing secret/path redaction applied. The failure tail is available through authenticated experiment detail, operation output, and the web UI; an initial public-diagnostics exposure was deliberately removed in follow-up hardening.

The feature was backed by 22 focused regression tests covering truncation, redaction, API/operation/UI behavior, successful-run omission, and safe DOM rendering.

The durable lesson is that **tool output should be shaped for the consumer**: normal success paths should be compact, while failures should surface the smallest bounded evidence needed for diagnosis. This is now an implemented optimization rather than only a research idea.

A related usability improvement exposes the full experiment ID with a copy action in the detail UI, reducing ambiguity when diagnosing failures or building operation chains remotely.

### 17. 2026-08-17 to 2026-08-18: local-first routing and broader operational autonomy under review

A stronger architectural direction emerged from repeated concern that external provider quotas and token budgets can become the limiting resource for continuous operation. The preferred direction is now to keep a local model available on the VM even when it is relatively slow, use it for small/medium tasks, context gathering, routing, and role changes, and delegate only work that clearly needs a stronger external model.

The desired execution model remains deliberately sequential rather than a large concurrent multi-agent system: one active model/agent at a time, with prompt-defined roles such as router, planner, developer, or tester. This keeps RAM, token use, concurrency, and causal debugging bounded while still allowing specialization.

Two draft pull requests prototype parts of this direction but are **not yet adopted on `main`**:

- PR #21 proposes a local-model routing/orchestration layer with persisted routing/provider-usage decisions and a sequential route -> plan -> develop -> test flow. Its concrete Llama 2 13B choice should be treated as an implementation experiment, not as a settled model decision.
- PR #22 proposes authenticated operational/self-healing capabilities for Git, services, logs, diagnosis, model testing, and configuration, with explicit allowlists, path validation, redaction, and operation logging. This explores whether Nexus can safely perform more of its own routine operation and recovery work.

The durable design lesson is to separate **capability architecture** from **specific model choice**: Nexus benefits from an always-available local decision layer and measured provider routing even if the best local model changes frequently.

A measurement decision was also made to re-run the earliest simple webpage-change experiment using the newer pipeline and compare it with the original baseline. This is intended to provide a concrete before/after check on whether the many orchestration changes improved cost, token use, runtime, and quality. No new measured result is recorded here yet.

### 18. 2026-08-18: B2ms resource envelope for persistent local inference

The Azure VM was resized to **Standard_B2ms (8 GB RAM)**. This is a deliberate low-cost infrastructure step toward keeping a local model continuously available for routing, context gathering, and small/medium tasks instead of consuming external-provider quota for every decision.

The 8-GB envelope is now an explicit experimental constraint rather than an abstract local-model idea. Model choice remains open: the concrete model should be selected by measured fit, context capacity, latency, quality, and memory headroom for Nexus itself rather than by the provisional Llama 2 13B choice in PR #21. This creates a useful near-term benchmark question: **which current quantized model gives the best routing/coding usefulness per GB and per second while coexisting with the agent services on B2ms?**

## What Nexus currently learns

Nexus already accumulates several kinds of machine-readable or repository-visible information:

- code and tests,
- `IDEAS.md`, project documentation, and decision records,
- SQLite state and experiment history,
- mailbox `context` messages stored as knowledge,
- experiment outcomes and diagnostics,
- activity and usage data.

However, Nexus does **not automatically know the full reasoning and development history from ChatGPT conversations**.

That means important context can currently be lost, for example:

- why a design was chosen,
- alternatives that were considered and rejected,
- recurring cost concerns,
- lessons from failed attempts,
- long-term research ideas that have not yet become code or an `IDEAS.md` entry.

`PROJECT_HISTORY.md` is intended to close that gap.

## Recommended use as Nexus project memory

The next small experiment should be to make Nexus read this document as high-level project context when it performs research or selects improvement ideas.

Do **not** inject the entire document into every model call by default. Instead compare a small number of strategies, for example:

1. no history context,
2. a short generated project-history summary,
3. retrieval of only relevant sections.

Measure whether project-memory context improves:

- relevance of proposed ideas,
- duplicate/repeated work,
- consistency with previous decisions,
- token consumption,
- latency,
- cost.

This turns "Nexus should remember our development" into a measurable experiment rather than an assumption.

## Current architectural picture

At the current stage the project contains several cooperating layers:

- persistent Azure VM, currently Standard_B2ms with 8 GB RAM,
- baseline and MCP-oriented agent code,
- LLM/provider access,
- single-call Codex experiment execution,
- experiment orchestration and quality gating,
- Git/worktree-based isolation and adoption,
- SQLite state,
- ideas and decision documents,
- separate Git-backed mailbox repository for human/agent communication,
- persistent recoverable outbox publication,
- API and browser UI,
- public and authenticated diagnostics,
- token-efficient verification with bounded failure evidence,
- tests and policy boundaries.

Draft work is exploring a persistent local routing layer and broader self-healing operations, but these are not yet current `main` capabilities.

The system is still intentionally small enough to understand.

## Important lessons so far

1. **Autonomy requires observability.** A result without enough evidence to understand the path to it is difficult to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether it should be adopted.
3. **Git is useful state, but not every kind of state belongs in the same repository.** The separate mailbox repository validated this by removing communication commits from the source history.
4. **Agent-to-agent/human communication needs delivery semantics.** Persisted publication state, locking, retry, and recovery are necessary to make Git-backed messaging reliable across crashes.
5. **Cheap reasoning matters.** Continuous autonomy will probably need a hierarchy of models, including small/local models for simple decisions and expensive models only when justified.
6. **Context itself is a resource.** More memory is not automatically better; project memory should be retrieved or summarized and measured.
7. **Failures are research data.** Selection failures, CLI incompatibilities, merge conflicts, timeouts, rejected experiments, and stale regression assumptions are useful inputs to the next iteration.
8. **Optimize orchestration before buying more model capacity.** Collapsing 3-5 LLM calls to one and reducing routine tool output can lower cost without changing providers; quality must then be measured explicitly.
9. **Compact output and useful diagnostics are compatible.** Quiet success output plus bounded/redacted failure evidence gives a better signal-to-token ratio than verbose logs everywhere.
10. **Separate routing architecture from model identity.** An always-available local decision layer can remain useful while the concrete local model is replaced as better small models appear.
11. **Keep the loop small.** Build, run, measure, understand, then choose the next improvement.

## Open research directions

Current directions worth investigating include:

- project memory and retrieval from this history,
- event-driven mailbox wake-up (for example via GitHub-triggered notification) so Nexus can react immediately to new inbox commits while retaining the timer as fallback,
- measuring the real token/cost reduction and implementation-quality impact of the single-call pipeline and quiet verification output,
- persistent local-first routing for small/medium work, context gathering, and delegation decisions,
- model routing by task difficulty, expected value, provider limits, and measured quality,
- benchmark current quantized local models within the B2ms 8-GB RAM envelope, including usable context size and memory headroom for Nexus services,
- safe autonomous operations/self-healing with narrow allowlists and audit logs,
- stronger automatic evaluation of proposed changes,
- measuring improvement quality per euro and per token,
- automatic discovery of recurring operational problems,
- agent-generated experiments for its own prompts/tools/context,
- multi-agent communication and specialization only when a measurable benefit exists,
- context compression and retrieval strategies,
- local model quantization/fine-tuning where cost-effective,
- controlled exploration of useful external projects, services, or open-source contributions,
- eventually testing whether Nexus can recover part of its operating cost through useful work.

## Current next-step candidates

The immediate work should remain small. Strong candidates now include:

1. **Project-memory experiment:** expose this history to Nexus through a minimal retrieval/context mechanism and measure whether its next idea selections improve.
2. **Single-call before/after measurement:** re-run a representative early experiment and compare quality, total tokens, runtime, and estimated cost against the historical multi-stage result.
3. **Local-router experiment:** benchmark a small current quantized model inside the B2ms 8-GB envelope as the always-available router/context-gatherer, while keeping execution sequential and measuring memory headroom, latency, context size, and how often delegation is actually necessary.
4. **Operational-autonomy review:** evaluate the draft self-healing operations against least-privilege, rollback, auditability, and measurable reduction in human intervention before adoption.

The next choice should be based on expected learning value, implementation effort, risk, and measurable benefit rather than on architectural completeness.

## Maintenance rule

Update this document when a development changes the project's understanding of itself — especially after:

- a major experiment,
- a significant failure or lesson,
- an architectural change,
- a new persistent capability,
- a change in autonomy or safety boundaries,
- or a meaningful change in the research direction.

Routine implementation details belong in commits, issues, and specialized documents rather than here.