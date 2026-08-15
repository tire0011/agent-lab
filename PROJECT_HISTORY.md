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

- persistent Azure VM,
- baseline and MCP-oriented agent code,
- LLM/provider access,
- Codex execution,
- experiment orchestration,
- Git/worktree-based isolation and adoption,
- SQLite state,
- ideas and decision documents,
- Git-backed human/agent mailbox,
- API and browser UI,
- public and authenticated diagnostics,
- tests and policy boundaries.

The system is still intentionally small enough to understand.

## Important lessons so far

1. **Autonomy requires observability.** A result without enough evidence to understand the path to it is difficult to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether it should be adopted.
3. **Git is useful state, but not every kind of state belongs in the same repository.** High-frequency communication can conflict with source integration.
4. **Cheap reasoning matters.** Continuous autonomy will probably need a hierarchy of models, including small/local models for simple decisions and expensive models only when justified.
5. **Context itself is a resource.** More memory is not automatically better; project memory should be retrieved or summarized and measured.
6. **Failures are research data.** Selection failures, CLI incompatibilities, merge conflicts, timeouts, and rejected experiments are useful inputs to the next iteration.
7. **Keep the loop small.** Build, run, measure, understand, then choose the next improvement.

## Open research directions

Current directions worth investigating include:

- project memory and retrieval from this history,
- separating the communication repository from the source repository,
- using a small local LLM for frequent action selection and reflection,
- model routing by task difficulty and expected value,
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

The immediate work should remain small. The strongest candidates are:

1. **Project-memory experiment:** expose this history to Nexus through a minimal retrieval/context mechanism and measure whether its next idea selections improve.
2. **Communication-repository experiment:** move or prototype inbox/outbox in a separate Git repository to eliminate source-branch contention.
3. **Cheap local decision model:** test whether a small model running on existing hardware can handle frequent low-risk decisions cheaply enough to reduce API use.

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
