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

MCP-style tools were introduced as an architectural direction so model reasoning and external capabilities can evolve independently rather than becoming one monolithic process. V1 remains an evolving direction rather than a finished architecture.

### 5. Web API and browser control surface

FastAPI and a small web UI evolved from simple health/status access into a remote operational surface for problems, ideas, chat, Codex tasks, experiment state, mailbox approvals, activity, diagnostics, and sanitized logs. A core design rule is to expose enough evidence to understand Nexus without publicly leaking raw prompts, source code, unrestricted logs, paths, or secrets.

### 6. Controlled coding through Codex

Codex CLI became the first controlled implementation path. Important lessons were that CLI flags and sandbox combinations must be tested, read-only analysis and workspace-write implementation need different boundaries, task IDs/status must survive connection failures, and timeout/working-directory constraints must be enforced server-side.

### 7. ExperimentService and measurable self-modification

`ExperimentService` turned improvement ideas into isolated experiments with research/selection, implementation, verification, and terminal states such as success, failure, rejection, timeout, or `needs_review`. Git worktrees isolate changes; verification uses allowlisted commands; risky actions are bounded.

The conceptual step was separating **proposing a change** from **adopting a change**. A self-improving system must be able to reject its own bad changes.

### 8. Better selection and diagnostics

Weak/placeholder experiment selections led to tighter validation requiring meaningful affected files and implementation scope. Selection/reformat failures became visible through diagnostics instead of silently disappearing. Observability is treated as part of intelligence: failures that cannot be explained cannot be systematically improved.

### 9. Human approval loop

Mailbox tasks gained a human approval gate before autonomous code modification:

`message -> pending approval -> approved -> submitted -> running -> result`

The boundary is intentionally evolvable; later experiments may identify low-risk categories suitable for auto-approval.

### 10. Git-backed Nexus mailbox

A Git-backed inbox/outbox created a simple, human-readable, versioned protocol between humans, ChatGPT, Nexus, and future agents. It also exposed an important systems lesson: communication commits have a different write pattern from source-code integration and can cause non-fast-forward/contention problems.

### 11. Public activity and diagnostics

Sanitized activity/diagnostic APIs expose lifecycle timing, changed filenames, test state, bounded messages, usage, estimated cost where available, and deployment revision. Nexus should increasingly be evaluated by evidence of what it did, what it cost, and whether the outcome improved measurable behavior.

### 12. Operation chains

The system began moving from one-shot operations toward sequential operation chains. Integration conflicts when `origin/main` moved during adoption correctly produced `needs_review` rather than forcing a merge, validating conservative adoption behavior.

### 13. 2026-08-15: code/mailbox repository split

The communication/source split was implemented. `/agent` is the source repository and `/var/lib/nexus/mailbox-repo` the mailbox repository; compatibility with the older layout remains. Cross-repository regression coverage included an end-to-end two-repository mailbox test.

**Lesson:** communication state and source-integration state should be isolated when their Git histories interfere.

### 14. 2026-08-15: crash-safe outbox publication

Task and operation-chain results now use a persistent `OutboxPublisher` lifecycle such as:

`pending_write -> written -> committing -> committed -> pushed`

Intent is persisted before the atomic file write; publication is serialized; recovery retries unfinished work; a recorded commit SHA is reused for push retry. Git diagnostics are redacted.

**Lesson:** asynchronous communication requires idempotent delivery semantics; writing an outbox file is not the same as reliably delivering it.

### 15. 2026-08-16: single-call experiment pipeline

The normal Codex experiment path was reduced from roughly **3-5 LLM calls to one implementation call**. The model now receives the raw goal and investigates/plans/implements/tests within one call; safety checks remain before execution and implementation quality/verification remain after it.

A regression showed that explicit tests-only goals need separate classification: legitimate test-only experiments may succeed, while ordinary implementation goals that modify only tests still go to `needs_review`.

The key research trade-off is now measurable: **how much cost/token reduction is gained, and does implementation quality remain acceptable?**

### 16. 2026-08-16: token-efficient verification with bounded failure evidence

Default `unittest` verification uses quiet mode (`-q`). Success keeps only useful summary output; failures retain a bounded/redacted tail (30 lines, 500 characters/line, 4,000 characters total). Twenty-two focused regression tests covered truncation, redaction, API/operation/UI behavior, omission on success, and safe rendering.

**Lesson:** tool output should be shaped for its consumer—compact on success, minimal actionable evidence on failure.

### 17. 2026-08-17 to 2026-08-18: local-first routing direction

Repeated external-provider quota pressure strengthened the local-first design: keep one small local model available continuously for routing, context gathering, planning, and small/medium tasks; escalate only when a stronger external provider is justified.

Execution is deliberately **sequential**: one model/provider active at a time, with roles such as router, context/research, planner, developer, and reviewer/tester. This bounds RAM, concurrency, token use, and causal complexity.

PR #21 prototypes this architecture but remains open/draft and is **not adopted on `main`**. Review caused several important design corrections:

- role and provider are separate concepts; a role can be executed by different providers,
- the local server/model configuration is generic rather than tied to a model family,
- context gathering is a first-class local step before expensive delegation,
- Codex delegation must execute for real rather than silently falling back to local,
- provider accounting records the provider actually used plus runtime/token data where available,
- verification uses real tests and `git diff`; an LLM reviewer interprets evidence but cannot override a failed real test,
- direct orchestrator tests cover delegation, fallback, sequential execution, verification, and provider logging (55 tests reported after the architectural refactor).

The preferred current default candidate in the PR is **Qwen2.5-Coder-7B-Instruct Q4_K_M**, served through generic `llama.cpp`/`llama-server` infrastructure rather than `llama-cpp-python`. The architecture remains model-agnostic so this default can change after benchmarking.

### 18. 2026-08-18: B2ms resource envelope for persistent local inference

The Azure VM was resized to **Standard_B2ms: 2 vCPUs and 8 GiB RAM**. PR #21's conservative deployment defaults now target roughly 8k context, one parallel slot, two CPU threads, local-only binding (`127.0.0.1`), and the `agent` service user. These remain configurable.

This turns local-model selection into a concrete resource-constrained benchmark: quality and routing/coding usefulness must be weighed against memory headroom, usable context, latency, and coexistence with Nexus services.

Deployment on the real VM also exposed a missing build dependency: the OpenBLAS `llama.cpp` build failed because `pkg-config` was absent. At the latest inspected PR head, `scripts/install-llama-cpp.sh` still lacked `pkg-config`, so PR #21 remains unfinished. This is a useful reminder that installation scripts must be validated on the actual target environment, not only through unit tests or static review.

### 19. 2026-08-18: self-healing operations adopted on main

PR #22 was reviewed, hardened, approved, and merged into `main`. Nexus now has authenticated `/operations/*` capabilities for bounded Git operations, service status/restart, log inspection, diagnostics/self-healing, local-model status/testing, and restricted configuration operations. Invocations are centrally dispatched and persisted in an operation log.

Review deliberately tightened the safety boundary before adoption:

- lock cleanup only treats locks as stale when age/liveness evidence supports it; active/unknown locks are preserved,
- Git paths are resolved against the repository root and traversal/symlink escape is rejected,
- service actions use explicit allowlists,
- configuration values are not echoed back,
- audit parameters/results are recursively redacted through nested dict/list/tuple structures before persistence,
- direct safety tests cover these implementation boundaries rather than mocking only HTTP endpoints.

**Lesson:** increasing autonomy should come with narrower, testable capabilities and auditability—not unrestricted shell access. Safety review can expand practical autonomy when it removes concrete failure modes instead of blocking the capability wholesale.

A separate measurement decision remains open: re-run an early simple webpage-change experiment with the newer pipeline and compare quality, total tokens, runtime, and estimated cost against the historical baseline. No measured result is recorded yet.

## What Nexus currently learns

Nexus already accumulates durable information from:

- code and tests,
- project/idea/decision documents,
- SQLite experiment and operation state,
- mailbox `context` knowledge,
- experiment outcomes, diagnostics, activity, usage, and cost data where available.

It does **not automatically inherit the full reasoning history from ChatGPT conversations**. `PROJECT_HISTORY.md` exists to preserve durable decisions, rejected alternatives, cost concerns, failures, and long-running research directions that would otherwise be lost.

## Recommended use as Nexus project memory

Do not inject this entire file into every model call. Compare at least:

1. no history context,
2. a short generated history summary,
3. retrieval of only relevant history sections.

Measure relevance of proposed ideas, duplicate work, consistency with previous decisions, token use, latency, and cost. Project memory should itself be an experiment.

## Current architectural picture

Current `main` includes:

- persistent Standard_B2ms Azure VM,
- baseline/MCP-oriented agent code and provider access,
- single-call Codex experiment execution,
- experiment orchestration, worktree isolation, quality gating, and verification,
- SQLite state and durable project/idea/decision documents,
- separate Git-backed mailbox repository with crash-safe outbox publication,
- API/web UI, public/authenticated diagnostics, and compact failure evidence,
- authenticated, audited, allowlisted self-healing/operations capabilities,
- tests and policy boundaries.

PR #21 is still exploring the persistent local routing/orchestration layer; it is not yet a `main` capability.

## Important lessons so far

1. **Autonomy requires observability.** A result without evidence is hard to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether to adopt it.
3. **Different state types may need different repositories.** Mailbox traffic and source integration had incompatible Git write patterns.
4. **Communication needs delivery semantics.** Persistence, locking, retry, recovery, and idempotence matter.
5. **Cheap reasoning matters.** Local/small models should handle work that does not justify an expensive provider.
6. **Context is a resource.** Retrieve or compress relevant memory instead of blindly increasing context.
7. **Failures are research data.** CLI mistakes, merge conflicts, stale assumptions, deployment failures, and rejected experiments should influence the next iteration.
8. **Optimize orchestration before buying more capacity.** Fewer LLM calls and quieter tools can reduce cost before changing models.
9. **Real tools are the source of truth for verification.** LLM reviewers may interpret evidence but should not invent pass/fail state.
10. **Separate capability architecture from model identity.** The routing layer should survive model replacement.
11. **Target-environment validation matters.** Deployment scripts can fail despite clean architectural review and unit tests.
12. **Safety can enable more autonomy.** Narrow allowlists, path boundaries, recursive redaction, liveness checks, and audit logs made self-healing operations acceptable to merge.
13. **Keep the loop small.** Build, run, measure, understand, then choose the next improvement.

## Open research directions

- project-memory retrieval from this history,
- event-driven mailbox wake-up with timer fallback,
- measured token/cost/quality impact of the single-call pipeline and compact tool output,
- finish and deploy the generic local-first router after fixing the remaining VM build issue,
- benchmark Qwen2.5-Coder-7B-Instruct Q4_K_M and alternative quantized models inside the B2ms envelope,
- route by task difficulty, expected value, provider limits, historical quality, and resource cost,
- measure how much local context preparation reduces external-provider tokens,
- evaluate whether merged self-healing operations materially reduce human intervention without increasing incidents,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- context compression/retrieval strategies,
- quantization/fine-tuning where evidence justifies it,
- multi-agent specialization only when measurable benefit exceeds complexity,
- controlled exploration of useful products, services, public open-source work, and eventually partial cost recovery.

## Current next-step candidates

1. **Finish PR #21 deployment validation:** add the missing `pkg-config` dependency, build on the real B2ms VM, run the local server, and record memory/latency/context observations before merge.
2. **Local-router benchmark:** test Qwen2.5-Coder-7B Q4_K_M against at least one plausible alternative under the same B2ms limits; measure routing accuracy/usefulness, latency, RAM, context, and delegation rate.
3. **Single-call before/after measurement:** repeat a representative early experiment and compare quality, tokens, runtime, and cost with the historical multi-stage path.
4. **Project-memory experiment:** expose only relevant history to Nexus and measure whether idea selection becomes more relevant and less repetitive.
5. **Operational-autonomy measurement:** use the merged `/operations/*` layer in normal Nexus operation and measure successful self-recovery, human interventions avoided, and any safety/false-positive incidents.

Choose the next step by expected learning value, effort, risk, and measurable benefit—not architectural completeness.

## Maintenance rule

Update this document only when development changes the project's understanding of itself: major experiments, significant failures/lessons, architectural changes, persistent capabilities, autonomy/safety boundary changes, infrastructure changes, or meaningful research-direction changes. Routine implementation details belong in commits, issues, and specialized documentation.
