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

Task and operation-chain results use a persistent `OutboxPublisher` lifecycle such as:

`pending_write -> written -> committing -> committed -> pushed`

Intent is persisted before the atomic file write; publication is serialized; recovery retries unfinished work; a recorded commit SHA is reused for push retry. Git diagnostics are redacted.

**Lesson:** asynchronous communication requires idempotent delivery semantics; writing an outbox file is not the same as reliably delivering it.

### 15. 2026-08-15: event-driven mailbox wake-up

A signed GitHub webhook adapter was merged for mailbox changes, sharing the same single-poll processor as the timer-based path. The timer remains a fallback rather than the only wake-up mechanism.

**Lesson:** event-driven wake-up can reduce latency without removing the simpler recovery path.

### 16. 2026-08-16: single-call experiment pipeline

The normal Codex experiment path was reduced from roughly **3-5 LLM calls to one implementation call**. The model now receives the raw goal and investigates/plans/implements/tests within one call; safety checks remain before execution and implementation quality/verification remain after it.

A regression showed that explicit tests-only goals need separate classification: legitimate test-only experiments may succeed, while ordinary implementation goals that modify only tests still go to `needs_review`.

The key research trade-off is now measurable: **how much cost/token reduction is gained, and does implementation quality remain acceptable?**

### 17. 2026-08-16: token-efficient verification with bounded failure evidence

Default `unittest` verification uses quiet mode (`-q`). Success keeps only useful summary output; failures retain a bounded/redacted tail (30 lines, 500 characters/line, 4,000 characters total). Twenty-two focused regression tests covered truncation, redaction, API/operation/UI behavior, omission on success, and safe rendering.

**Lesson:** tool output should be shaped for its consumer—compact on success, minimal actionable evidence on failure.

### 18. 2026-08-17 to 2026-08-19: local-first routing developed and validated

Repeated external-provider quota pressure strengthened the local-first design: keep one small local model available continuously for routing, context gathering, planning, and small/medium tasks; escalate only when a stronger external provider is justified.

Execution is deliberately **sequential**: one model/provider active at a time, with roles such as router, context/research, planner, developer, and reviewer/tester. This bounds RAM, concurrency, token use, and causal complexity.

PR #21 evolved through real-VM testing and review. Important corrections included:

- role and provider are separate concepts,
- local server/model configuration is model-agnostic,
- deterministic repo context is gathered before expensive delegation,
- a clean worktree must not be mistaken for an empty repository,
- read-only/no-change tasks verify worktree cleanliness instead of running the full test suite,
- Codex delegation executes for real rather than silently falling back,
- provider accounting records the provider actually used plus runtime/token data where available,
- real tests and `git diff` are authoritative; an LLM reviewer cannot override failed objective verification.

The preferred default is **Qwen2.5-Coder-7B-Instruct Q4_K_M** via native `llama.cpp`/`llama-server`, while the orchestration layer remains model-agnostic.

### 19. 2026-08-18: B2ms resource envelope for persistent local inference

The Azure VM was resized to **Standard_B2ms: 2 vCPUs and 8 GiB RAM**. Conservative local-server defaults target roughly 8k context, one parallel slot, two CPU threads, local-only binding (`127.0.0.1`), and the `agent` service user.

Real deployment exposed a missing OpenBLAS build dependency (`pkg-config`). The install script was subsequently fixed before the local-router branch was merged.

**Lesson:** installation scripts must be validated on the actual target environment, not only through unit tests or static review.

### 20. 2026-08-18: self-healing operations adopted on main

PR #22 was reviewed, hardened, approved, and merged into `main`. Nexus gained authenticated `/operations/*` capabilities for bounded Git operations, service status/restart, log inspection, diagnostics/self-healing, local-model status/testing, and restricted configuration operations. Invocations are centrally dispatched and persisted in an operation log.

Review tightened the safety boundary: stale-lock cleanup uses age/liveness evidence; Git path traversal/symlink escape is rejected; service actions are allowlisted; configuration values are not echoed; audit parameters/results are recursively redacted; and direct safety tests cover implementation boundaries.

**Lesson:** increasing autonomy should come with narrower, testable capabilities and auditability—not unrestricted shell access.

### 21. 2026-08-19: local-first multi-provider orchestration adopted on main

PR #21 was merged into `main` after target-VM fixes and read-only/context-gathering regressions were addressed. Nexus now has a model-agnostic sequential pipeline:

`Router -> Context Gatherer -> Planner -> Developer -> real-tool Verification -> Reviewer`

The persistent local model is the default low-cost decision layer, while Azure OpenAI and Codex are escalation providers when configured. A temporary disabled-by-default maintenance bridge supports bounded remote status/tests/router/service/log operations behind an additional token and optional expiry.

No quality/latency/cost benchmark has yet established that Qwen2.5-Coder-7B Q4_K_M is the best B2ms model; the merge adopts the capability architecture, not a permanent model choice.

### 22. 2026-08-20: read-only MCP HTTP adapter adopted on main

PR #27 added a dedicated Streamable HTTP MCP surface at `/mcp/`. It reuses existing FastAPI/service logic rather than introducing a parallel implementation and applies the existing API-key authentication model.

The MCP surface is intentionally **read-only**. It exposes compact structured inspection tools for status, problems, ideas/chat, Codex task reads, mailbox reads, experiment state/logs/adoption, activity/diagnostics, operation catalogs/chains, and maintenance status/audit. It deliberately excludes write endpoints, shell-backed operations, webhooks, and new filesystem capabilities.

Limit validation was centralized/shared with API constants and covered for MCP-side error cases before merge.

**Lesson:** MCP is most useful here as a thin capability adapter over tested domain services; remote interoperability does not require duplicating business logic or widening permissions.

## What Nexus currently learns

Nexus accumulates durable information from:

- code and tests,
- project/idea/decision/history documents,
- SQLite experiment and operation state,
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
- persistent local `llama.cpp` model capability plus model-agnostic sequential routing/orchestration,
- Azure OpenAI/Codex escalation paths with provider accounting,
- single-call Codex experiment execution,
- experiment orchestration, worktree isolation, quality gating, and real-tool verification,
- SQLite state and durable project/idea/decision/history documents,
- separate Git-backed mailbox repository with crash-safe outbox publication,
- signed GitHub webhook wake-up with timer fallback,
- API/web UI, public/authenticated diagnostics, and compact failure evidence,
- authenticated, audited, allowlisted self-healing/operations capabilities,
- authenticated read-only MCP HTTP inspection surface,
- tests and policy boundaries.

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
13. **Adapters should reuse domain logic.** MCP/API surfaces should expose existing tested capabilities rather than fork behavior.
14. **Keep the loop small.** Build, run, measure, understand, then choose the next improvement.

## Open research directions

- project-memory retrieval from this history,
- measured token/cost/quality impact of the single-call pipeline and compact tool output,
- benchmark Qwen2.5-Coder-7B-Instruct Q4_K_M and alternative quantized models inside the B2ms envelope,
- test what the merged local model/router can accomplish on genuinely read-only small and medium tasks before granting broader autonomy,
- route by task difficulty, expected value, provider limits, historical quality, and resource cost,
- measure how much local context preparation reduces external-provider tokens,
- evaluate whether merged self-healing operations materially reduce human intervention without increasing incidents,
- evaluate MCP as the standard inspection/integration path for external agents before adding any write-capable MCP tools,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- context compression/retrieval strategies,
- quantization/fine-tuning where evidence justifies it,
- adaptive two-pass context research / richer repo-map approaches when small context is insufficient,
- multi-agent specialization only when measurable benefit exceeds complexity,
- controlled exploration of useful products, services, public open-source work, and eventually partial cost recovery.

## Current next-step candidates

1. **Read-only local-router evaluation:** give Nexus representative inspection/reasoning tasks and record success, latency, context use, routing/delegation behavior, and failures without allowing code changes.
2. **Local-model benchmark:** compare Qwen2.5-Coder-7B Q4_K_M with at least one plausible alternative under identical B2ms limits.
3. **Single-call before/after measurement:** repeat a representative early experiment and compare quality, tokens, runtime, and cost with the historical multi-stage path.
4. **Project-memory experiment:** expose only relevant history to Nexus and measure whether idea selection becomes more relevant and less repetitive.
5. **Operational-autonomy measurement:** measure successful self-recovery, human interventions avoided, and safety/false-positive incidents using the merged `/operations/*` layer.
6. **MCP integration experiment:** connect an external read-only client/agent to `/mcp/` and compare usability/token overhead with direct REST inspection.

Choose the next step by expected learning value, effort, risk, and measurable benefit—not architectural completeness.

## Maintenance rule

Update this document only when development changes the project's understanding of itself: major experiments, significant failures/lessons, architectural changes, persistent capabilities, autonomy/safety boundary changes, infrastructure changes, or meaningful research-direction changes. Routine implementation details belong in commits, issues, and specialized documentation.