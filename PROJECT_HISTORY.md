# Project History

This document is durable project memory for the **distributed self-improving LLM agents** project and agents such as Nexus. It complements `PROJECT.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `IDEAS.md`, and `COSTS.md`. It preserves why the system evolved, what was learned, and which questions remain open.

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

The project began with the idea of an independent agent that can improve tools, context, memory, model selection, evaluation, orchestration, and eventually its own useful capabilities. Early investigation covered Azure credits, local/cloud models, cheap providers, MCP/RAG, quantization, and multi-agent approaches. A recurring conclusion was that continuous autonomy should not depend on expensive external API calls.

### 2. Azure VM as the persistent laboratory

A small Ubuntu VM became Nexus's always-on home: predictable Linux tooling, continuous operation independent of a workstation, access to Azure credits, and a controlled place for APIs, schedules, experiments, logs, and self-modification.

### 3. Baseline agent V0

The first deliberately small baseline runs locally and on the Azure VM, can call an Azure-hosted LLM, and writes structured run artifacts. It exists as a measurable reference point; self-improvement is meaningful only relative to a previous state.

### 4. MCP and replaceable capability architecture

MCP-style tools were introduced so model reasoning and external capabilities can evolve independently rather than becoming one monolithic process. The architecture remains intentionally replaceable.

### 5. Web API and browser control surface

FastAPI and a small web UI evolved into a remote operational surface for problems, ideas, chat, Codex tasks, experiment state, mailbox state, activity, diagnostics, and sanitized logs. A core rule is to expose enough evidence to understand Nexus without publicly leaking raw prompts, source code, unrestricted logs, paths, or secrets.

### 6. Controlled coding through Codex

Codex CLI became the first controlled implementation path. Important lessons were that read-only analysis and workspace-write implementation need different boundaries, task IDs/status must survive connection failures, and timeout/working-directory constraints must be enforced server-side.

### 7. ExperimentService and measurable self-modification

`ExperimentService` turned improvement ideas into isolated experiments with implementation, verification, and terminal states such as success, failure, rejection, timeout, or `needs_review`. Git worktrees isolate changes; verification uses bounded commands. The key conceptual step was separating **proposing a change** from **adopting a change**.

### 8. Better selection and diagnostics

Weak experiment selections led to tighter validation requiring meaningful affected files and implementation scope. Selection/reformat failures became visible through diagnostics instead of silently disappearing. **Lesson:** failures that cannot be explained cannot be systematically improved.

### 9. Human approval loop

Mailbox tasks initially required a human approval gate before autonomous code modification. This was intentionally scaffolding, not permanent architecture; it was removed after the trusted development path gained stronger persistence, observability, verification, and recovery.

### 10. Git-backed Nexus mailbox

A Git-backed inbox/outbox created a simple, human-readable, versioned protocol between humans, ChatGPT, Nexus, and future agents. It also exposed that communication commits have a different write pattern from source-code integration and can create Git contention.

### 11. Public activity and diagnostics

Sanitized activity/diagnostic APIs expose lifecycle timing, changed filenames, test state, bounded messages, usage, estimated cost where available, and deployment revision. Nexus should increasingly be evaluated by evidence of what it did, what it cost, and whether the outcome improved measurable behavior.

### 12. Operation chains

The system moved from one-shot operations toward sequential operation chains. Integration conflicts when `origin/main` moved during adoption correctly produced `needs_review` rather than forcing a merge, validating conservative adoption behavior.

### 13. 2026-08-15: code/mailbox repository split

`/agent` became the source repository and `/var/lib/nexus/mailbox-repo` the mailbox repository. Cross-repository regression coverage included an end-to-end two-repository mailbox test. **Lesson:** communication state and source-integration state should be isolated when their Git histories interfere.

### 14. 2026-08-15: crash-safe outbox publication

Task and operation-chain results gained a persistent `OutboxPublisher` lifecycle (`pending_write -> written -> committing -> committed -> pushed`). Intent is persisted before atomic file write; publication is serialized; recovery retries unfinished work; recorded commit SHAs are reused for push retry. **Lesson:** writing an outbox file is not the same as reliably delivering it.

### 15. 2026-08-15: event-driven mailbox wake-up

A signed GitHub webhook adapter was merged for mailbox changes, sharing the same poll processor as the timer-based path. The timer remains a fallback. **Lesson:** event-driven wake-up can reduce latency without removing a simpler recovery path.

### 16. 2026-08-16: single-call experiment pipeline

The normal Codex experiment path was reduced from roughly **3-5 LLM calls to one implementation call**. The model investigates/plans/implements/tests within one call; safety checks remain before execution and verification remains after it. A regression showed that explicit tests-only goals need separate classification from ordinary implementation goals that accidentally change only tests.

### 17. 2026-08-16: token-efficient verification

Default `unittest` verification uses quiet mode (`-q`). Success keeps only useful summary output; failures retain a bounded/redacted tail. **Lesson:** tool output should be compact on success and preserve only actionable evidence on failure.

### 18. 2026-08-17 to 2026-08-19: local-first routing developed and validated

External-provider quota pressure strengthened the local-first design: keep one small local model continuously available for routing, context gathering, planning, and small/medium work; escalate only when justified. Execution is sequential so one model/provider is active at a time. PR #21 evolved through real-VM testing and adopted model-agnostic orchestration, deterministic context gathering, real verification, and provider accounting. The default became Qwen2.5-Coder-7B-Instruct Q4_K_M via `llama.cpp`, while the architecture remains model-agnostic.

### 19. 2026-08-18: B2ms resource envelope

The Azure VM was resized to **Standard_B2ms: 2 vCPUs and 8 GiB RAM**. Conservative local-server defaults target roughly 8k context, one parallel slot, two CPU threads, and local-only binding. Real deployment exposed a missing OpenBLAS dependency (`pkg-config`), fixed before merge. **Lesson:** installation scripts must be validated on the actual target environment.

### 20. 2026-08-18: self-healing operations adopted

PR #22 added authenticated `/operations/*` capabilities for bounded Git operations, service status/restart, log inspection, diagnostics/self-healing, local-model status/testing, and restricted configuration operations. Review tightened stale-lock cleanup, path/symlink boundaries, service allowlists, recursive redaction, and safety tests. **Lesson:** increasing autonomy should come with narrower, testable capabilities and auditability—not unrestricted shell access.

### 21. 2026-08-19: local-first multi-provider orchestration adopted

PR #21 merged a model-agnostic sequential pipeline: `Router -> Context Gatherer -> Planner -> Developer -> real-tool Verification -> Reviewer`. The local model is the default low-cost decision layer; Azure OpenAI and Codex are escalation providers when configured. The capability architecture was adopted, not a permanent model choice.

### 22. 2026-08-20: read-only MCP HTTP adapter

PR #27 added a Streamable HTTP MCP surface at `/mcp/`, reusing existing FastAPI/service logic and authentication. It exposes compact read-only inspection tools and deliberately avoids duplicating business logic.

### 23. 2026-08-20: bootstrap MCP write capabilities

PR #29 added write-capable MCP bootstrap tools behind `MCP_WRITE_ENABLED=true`, including Git pull, service status/restart, bounded tests, experiment start, local-router execution, and a temporary bounded/redacted/audited shell tool.

### 24. 2026-08-20: dependency synchronization became automatic

A deployment failure exposed stale Python dependencies after source updates. PR #32 added a reusable dependency-sync helper for `requirements-api.txt` and wired it into startup, refresh/deploy, and operations-driven Git pull paths. **Lesson:** source deployment and runtime dependency state are one operational concern.

### 25. 2026-08-21: safety-policy false positive fixed

An experiment was rejected because `output tokens` was interpreted as a credential action. Issue #33/PR #35 introduced a narrow context-specific exception for LLM token metrics while credential-qualified access/auth/API/bearer token actions remain rejected. **Lesson:** safety-policy quality includes false-positive measurement.

### 26. 2026-08-21: trusted mailbox became a development command interface

PR #34 added a mailbox `command` message type and PR #36 removed the development-mode execution gate for command messages in the trusted setup. Timeout/output/redaction/environment protections remain. **Lesson:** trusted-channel policy and command-content policy are separate choices.

### 27. 2026-08-21 to 2026-08-22: mailbox Git synchronization became self-healing

PR #38 replaced a push-first/`pull --ff-only` deadlock with a bounded **fetch -> rebase onto upstream -> push** loop. The tree must be clean; push races retry; real conflicts abort safely; force-push/reset/discard are not used. A real-Git regression test verifies simultaneous local outbox and remote inbox commits survive and converge.

### 28. 2026-08-22: verification timeout became a first-class outcome

Issue #39 separated subprocess timeout from ordinary test failure. Nexus records a distinct `test_timeout` event and timed-out state instead of folding it into `needs_review`. **Lesson:** failure classes must remain distinguishable if the system should learn different responses.

### 29. 2026-08-22: persisted adaptive verification timeout

Issue #40 / PR #45 introduced a small evidence-driven adaptation: verification starts at 300 seconds and increases by 1.5 after `test_timeout`, capped at 1,800 seconds, persisted in SQLite and reused after restarts. Successful or ordinary failing tests do not increase it. The full suite reported **905 tests, OK**. **Lesson:** bounded parameter adaptation is already a real self-improvement loop.

### 30. 2026-08-22: GitHub and MCP control-plane directions

Issues #41-#44 defined the next coordination layer: GitHub Issues as durable work state, experiment branches/PRs for review, concise issue progress, and a possible global MCP gateway where a cheap local LLM routes specialized tools while reducing tool-schema context. The global gateway remains a research direction and should first be tested with only 2-3 capabilities.

### 31. 2026-08-22 to 2026-08-24: persistent local-LLM controller adopted

A resume request exposed that one-shot intent classification was not enough. Issue #46 / PR #48 moved Nexus to a bounded ReAct-style loop: the local model chooses a tool or final action, receives the real observation, and decides again. Durable SQLite sessions preserve source identity and discussion history. Tools include operations, bounded memory search, delegation, and experiment start/resume. Delegation returns observations rather than transferring task ownership. PR #48 was merged after focused real-VM validation; an end-to-end Laguna run used memory search and Git status and completed without creating an experiment.

**Lesson:** routing is not control. If the local model is meant to be the agent, control should return to it after tool/delegate observations.

### 32. 2026-08-23 to 2026-08-24: long local inference became persistent work

Real Laguna controller turns on B2ms took roughly **7.5 to 14 minutes**. A synchronous controller request exceeded transport timeouts even while `llama-server` remained healthy. Nexus therefore persists serialized local inference as SQLite jobs (`queued`, `running`, `succeeded`, `failed`, `cancelled`) and lets the owning task wait/resume from stored results. **Lesson:** slow local inference is normal work, not necessarily a hung request.

### 33. 2026-08-24: mailbox observability and repository separation tightened

PR #51 added focused, content-free diagnostics around webhook/timer mailbox polls. PR #52 removed the obsolete in-repo `communication/` mailbox after the separate mailbox repository became authoritative. **Lesson:** completed migrations should remove compatibility state that can mislead operators and agents.

### 34. 2026-08-25: experiment adoption and normal verification share one runner

Experiment verification and adoption/integration verification now use the same isolated runner, including persisted adaptive timeout, timeout classification, environment isolation, duration/output handling, and future-timeout adjustment. **Lesson:** duplicated verification implementations drift.

### 35. 2026-08-25: manual mailbox task approval removed

Mailbox task messages are now immediately handed to the existing worker/controller; the UI approval affordance was removed. Existing timeout, output, redaction, environment, persistence, experiment isolation, and self-disruption protections remain. **Lesson:** approval gates can be useful scaffolding, but stronger evidence and recovery mechanisms can replace redundant manual clicks in a trusted channel.

### 36. 2026-08-25: GitHub Actions became a reusable verification surface

A repository test workflow was added for Python 3.12 using `python -m unittest discover -s tests -q`, with dependency caching, a 20-minute job timeout, read-only repository permissions, and concurrency cancellation. It runs for pull requests targeting `main`, manual dispatch, and pushes to `nexus/**` branches.

This creates the basis for remote verification of Nexus-generated branches without requiring every test run to consume VM time. The desired next step is to let Nexus recognize successful GitHub verification and run the same tests locally only when remote evidence is absent or unusable.

**Lesson:** verification can be distributed, but the evidence model should stay unified; CI success should be reusable proof rather than trigger redundant local work.

### 37. 2026-08-26: command execution became iterative controller work

PR #53 promoted trusted `command` execution from a terminal controller action into a first-class tool in the Nexus loop. Command output is bounded/redacted, returned as an observation, and the local controller must decide again. Focused tests cover `command -> observation -> command -> observation -> answer`.

This is a meaningful autonomy improvement: Nexus can now inspect repository/system state, react to what actually happened, issue a second command, and only then conclude instead of treating shell execution as the end of reasoning.

**Lesson:** operational actions should usually return control to the agent; terminal actions are appropriate only when ownership genuinely transfers to another durable workflow.

### 38. 2026-08-26: GitHub became the outer experiment lifecycle

PR #54 implemented issue/branch/PR identity as the durable human-readable lifecycle around Nexus's existing isolated worktrees:

`Issue -> nexus/<issue>-<slug> branch -> Nexus experiment -> verified adoption -> PR -> merge to main -> Issue completed`

Nexus reuses an explicit/exact Issue when possible, otherwise creates one; creates/reuses the Issue branch before implementation; preserves source-to-work and experiment-to-work mappings so multiple experiments can belong to one work item; creates/reuses a PR; and marks the Issue complete after successful merge. Internal `experiment/<uuid>` worktrees remain the isolated execution environment.

When GitHub tracking is configured, adoption publishes to the Issue branch and only reaches `main` through the PR path. GitHub failures or merge conflicts become `needs_review` and leave `main` unchanged. The previous direct-main path remains only as compatibility fallback when GitHub tracking is not configured.

**Lesson:** runtime experiment identity and human work identity solve different problems. Keeping both allows strong isolation internally while making long-running autonomous work understandable and auditable through standard GitHub objects.

### 39. 2026-08-26: adaptive provider routing with local development fallback

PR #56 extended the existing agentic controller instead of adding a second routing architecture. The policy now treats the local model as a real development route for simple analysis and small changes through the iterative command loop; Azure OpenAI is preferred for harder reasoning/research and medium programming guidance; Codex experiments are reserved for large or broad repository implementation.

Delegation gained automatic provider fallback. For example, Azure failure/quota can fall back to local and then Codex when available. Provider selection, fallback, completion, and escalation reasons are logged, and observations persist both requested and actually used provider information.

**Lesson:** local-first should mean **cheapest capable route**, not merely “local as emergency fallback.” Routing quality must be measured by actual task success, provider use, cost, latency, and fallback behavior rather than model preference alone.

### 40. 2026-08-26 to 2026-08-27: first CI/smoke-test hardening exposed environment drift

Using GitHub Actions and Nexus smoke testing as real verification surfaces exposed assumptions that local/VM runs had hidden. CI concurrency was separated by event so a push run cannot cancel the corresponding PR run. CI output moved to a dedicated runner that keeps successful output quiet and surfaces only actionable unittest failures. The workflow also gained an explicitly writable temporary SQLite database path instead of relying on repository-local runtime state.

The debugging cycle additionally caught test-driven regressions in security boundaries: mailbox secret validation and the authenticated experiment-logs route were restored rather than weakening production authentication to satisfy tests. The last observed workflow run before these final fixes still failed, so a clean post-fix CI run remains required before treating this hardening cycle as complete.

**Lesson:** remote CI is not only a second place to run tests; it is an environment-parity check. Test isolation must model writable runtime state explicitly, and fixing tests must not silently relax production authentication or secret validation.

## What Nexus currently learns

Nexus accumulates durable information from:

- code and tests,
- project/idea/decision/history documents,
- SQLite experiment, controller/session, local-LLM-job, GitHub-work mapping, operation, and small adaptive service state,
- mailbox `context` knowledge,
- experiment outcomes, diagnostics, activity, usage, CI evidence where integrated, and cost data where available.

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
- persistent local `llama.cpp` capability plus a persistent local-LLM controller and durable local-inference jobs,
- adaptive local/Azure OpenAI/Codex routing with provider fallback and actual-provider accounting,
- iterative command/operation/memory/delegation tool loops that return evidence to the local controller,
- single-call Codex experiment execution,
- worktree-isolated experiments with quality gating, shared real-tool verification/adoption runner, distinct timeout state, and persisted adaptive verification timeout,
- GitHub-backed outer work lifecycle using reusable Issues, `nexus/**` branches, PRs, merge, and Issue completion,
- GitHub Actions verification for PRs and `nexus/**` branch pushes, with event-separated concurrency and compact failure-focused output,
- SQLite state and durable project/idea/decision/history documents,
- separate Git-backed mailbox repository with crash-safe outbox publication and bounded fetch/rebase/push synchronization,
- signed GitHub webhook wake-up with timer fallback and bounded content-free diagnostics,
- API/web UI and compact public/authenticated diagnostics,
- authenticated, audited self-healing/operations capabilities,
- authenticated MCP HTTP surface with read-only inspection plus explicitly enabled bootstrap write tools,
- trusted-development mailbox task auto-execution,
- automatic API dependency synchronization,
- tests and policy boundaries.

The persistent local-controller and GitHub experiment-lifecycle designs are adopted on `main`. The global MCP gateway remains a research direction.

## Important lessons so far

1. **Autonomy requires observability.** A result without evidence is hard to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether to adopt it.
3. **Different state types may need different repositories.** Mailbox traffic and source integration had incompatible Git write patterns.
4. **Communication needs delivery semantics.** Persistence, locking, retry, recovery, idempotence, and safe reconciliation matter.
5. **Cheap reasoning matters.** Local/small models should handle work that does not justify an expensive provider.
6. **Context is a resource.** Retrieve or compress relevant memory instead of blindly increasing context.
7. **Failures are research data.** CLI mistakes, merge conflicts, deployment failures, rejected experiments, and timeouts should influence the next iteration.
8. **Optimize orchestration before buying capacity.** Fewer LLM calls and quieter tools can reduce cost before changing models.
9. **Real tools are the source of truth for verification.** LLMs may interpret evidence but should not invent pass/fail state.
10. **Separate capability architecture from model identity.** The routing layer should survive model replacement.
11. **Target-environment validation matters.** Deployment scripts can fail despite clean unit tests or static review.
12. **Safety can enable more autonomy.** Narrow boundaries, redaction, liveness checks, and audit logs can support stronger capabilities.
13. **Adapters should reuse domain logic.** MCP/API/GitHub surfaces should expose existing tested capabilities rather than fork behavior.
14. **Deployment includes dependencies.** Pulling source without synchronizing runtime state is incomplete.
15. **Safety policies need precision, not only strictness.** False positives can block useful experiments.
16. **Capability trust is layered.** A trusted development channel can allow broad commands while runtime/output/secrets remain bounded.
17. **Failure taxonomy enables adaptation.** Different failures should drive different bounded responses.
18. **Simple persisted adaptation counts.** Automatic timeout adjustment is already a self-improvement loop.
19. **Routing is not control.** The controller should regain control after tool/delegate observations.
20. **Long inference should be durable work.** Persist model jobs instead of coupling slow inference to one transport timeout.
21. **Verification semantics should be shared.** Experiment, adoption, local and remote evidence should converge on one meaning of “verified.”
22. **Approval gates are scaffolding, not architecture.** Stronger persistence, evidence, and recovery can replace redundant manual steps.
23. **GitHub work identity and runtime experiment identity are complementary.** Human-readable Issues/branches/PRs need not replace isolated internal worktrees.
24. **The cheapest capable route should win.** Local development is a normal path; escalation should be justified by task difficulty and evidence.
25. **Actions should usually produce observations, not terminate reasoning.** Iterative command execution is more agentic than one-shot command dispatch.
26. **CI is an environment-parity test.** Runtime paths, auth, secrets, and event semantics must remain valid outside the long-lived VM; test fixes must not weaken production boundaries.

## Open research directions

- project-memory retrieval from this history,
- measured token/cost/quality impact of the single-call pipeline and compact tool output,
- benchmark Laguna/current local models and alternatives inside the B2ms envelope, including usable context and latency,
- test a two-pass local workflow where a small-context first pass gathers/reduces repository context and a second pass implements,
- measure controller task success, command/tool loops, memory use, clarification/resume, missing-capability behavior, and human intervention,
- measure persistent local-LLM jobs: queue/generation/runtime, token rate, restart recovery, cancellation/watchdog behavior, and resource use,
- quantify adaptive routing by task class, provider chosen/requested/actually used, fallback rate, success, latency, tokens, and cost,
- measure how much local context preparation reduces external-provider tokens,
- stabilize GitHub Actions under a clean runner, then make the verification controller reuse matching successful CI evidence and run local tests only as fallback when remote evidence is missing, stale, failed, or unavailable,
- evaluate whether self-healing operations materially reduce human intervention without increasing incidents,
- evaluate MCP as the standard integration/control path and narrow temporary bootstrap capabilities over time,
- measure whether GitHub Issue/branch/PR lifecycle improves fire-and-forget development, traceability, recovery, and human review,
- add concise GitHub Issue lifecycle progress without duplicating detailed Nexus logs,
- prototype the global MCP gateway with only 2-3 tools and measure routing accuracy, schema-token reduction, latency, total cost, and failures,
- investigate missing-capability detection and structured issue creation before any automatic tool generation,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- context compression/retrieval strategies,
- quantization/fine-tuning where evidence justifies it,
- multi-agent specialization only when measurable benefit exceeds complexity,
- controlled exploration of useful products, services, public open-source work, and eventually partial cost recovery.

## Current next-step candidates

1. **Nexus smoke-test/autonomy sample:** give the adopted controller a small real task and measure whether it uses iterative commands/tools correctly, completes it, and records provider/evidence cleanly.
2. **CI stabilization and evidence reuse:** obtain a clean Actions run with production-equivalent auth/runtime assumptions, then teach verification to accept a matching successful run for the exact branch/commit and use local tests only when that evidence is absent or unusable.
3. **Adaptive-routing measurement:** run representative simple/medium/large tasks and measure local/Azure/Codex selection, fallbacks, task success, latency, tokens, cost, and human intervention.
4. **Persistent local-LLM measurement:** quantify Laguna/local-model queue/runtime/token behavior and verify recovery across API/controller restarts.
5. **Local-model/context benchmark:** compare current local candidates under identical B2ms limits and test two-pass context gathering versus simply increasing context.
6. **Single-call before/after measurement:** repeat a representative early experiment and compare quality, tokens, runtime, and cost with the historical multi-stage path.
7. **Project-memory experiment:** expose only relevant history to Nexus and measure whether idea selection becomes more relevant and less repetitive.
8. **GitHub lifecycle measurement:** verify multiple experiments can safely reuse one Issue/branch identity and that PR/merge/Issue completion remains recoverable under conflicts/provider failures.
9. **Global MCP gateway experiment:** route 2-3 known tool types through the local model and compare directly against main-model tool selection.

Choose the next step by expected learning value, effort, risk, and measurable benefit—not architectural completeness.

## Maintenance rule

Update this document only when development changes the project's understanding of itself: major experiments, significant failures/lessons, architectural changes, persistent capabilities, autonomy/safety boundary changes, infrastructure changes, or meaningful research-direction changes. Routine implementation details belong in commits, issues, and specialized documentation.