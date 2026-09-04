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

The project began with the idea of an independent agent that can improve tools, context, memory, model selection, evaluation, orchestration, and eventually its own useful capabilities. Early investigation covered Azure credits, local/cloud models, MCP/RAG, quantization, and multi-agent approaches. A recurring conclusion was that continuous autonomy should not depend on expensive external API calls.

### 2. Azure VM as the persistent laboratory

A small Ubuntu VM became Nexus's always-on home: predictable Linux tooling, continuous operation independent of a workstation, access to Azure credits, and a controlled place for APIs, schedules, experiments, logs, and self-modification.

### 3. Baseline agent V0

The first deliberately small baseline runs locally and on the Azure VM, can call an Azure-hosted LLM, and writes structured run artifacts. It exists as a measurable reference point; self-improvement is meaningful only relative to a previous state.

### 4. MCP and replaceable capability architecture

MCP-style tools were introduced so model reasoning and external capabilities can evolve independently rather than becoming one monolithic process. The architecture remains intentionally replaceable.

### 5. Web API and browser control surface

FastAPI and a small web UI evolved into a remote operational surface for problems, ideas, chat, Codex tasks, experiment state, mailbox state, activity, diagnostics, and sanitized logs. A core rule is to expose enough evidence to understand Nexus without publicly leaking raw prompts, source code, unrestricted logs, paths, or secrets.

### 6. Controlled coding through Codex

Codex CLI became the first controlled implementation path. Read-only analysis and workspace-write implementation need different boundaries; task IDs/status must survive connection failures; timeout/working-directory constraints belong server-side.

### 7. ExperimentService and measurable self-modification

`ExperimentService` turned improvement ideas into isolated experiments with implementation, verification, and terminal states such as success, failure, rejection, timeout, or `needs_review`. Git worktrees isolate changes. The key step was separating **proposing a change** from **adopting a change**.

### 8. Better selection and diagnostics

Weak experiment selections led to tighter validation requiring meaningful affected files and implementation scope. Selection/reformat failures became visible instead of silently disappearing. **Lesson:** failures that cannot be explained cannot be systematically improved.

### 9. Human approval loop

Mailbox tasks initially required human approval before autonomous modification. This was scaffolding and was later removed after persistence, observability, verification, and recovery improved.

### 10. Git-backed Nexus mailbox

A Git-backed inbox/outbox created a human-readable, versioned protocol between humans, ChatGPT, Nexus, and future agents. It also exposed that communication commits and source-code integration have incompatible Git write patterns.

### 11. Public activity and diagnostics

Sanitized APIs expose lifecycle timing, changed filenames, test state, bounded messages, usage, estimated cost where available, and deployment revision. Nexus should increasingly be judged by evidence of what it did, what it cost, and whether behavior improved.

### 12. Operation chains

The system moved from one-shot operations toward sequential chains. Integration conflicts when `origin/main` moved correctly produced `needs_review` rather than forcing a merge.

### 13. 2026-08-15: code/mailbox repository split

`/agent` became the source repository and `/var/lib/nexus/mailbox-repo` the mailbox repository. Cross-repository regression coverage included an end-to-end two-repository mailbox test. **Lesson:** isolate state types whose Git histories interfere.

### 14. 2026-08-15: crash-safe outbox publication

`OutboxPublisher` gained a persistent lifecycle (`pending_write -> written -> committing -> committed -> pushed`), atomic write, serialized publication, recovery, and commit-SHA reuse. **Lesson:** writing a result is not the same as reliably delivering it.

### 15. 2026-08-15: event-driven mailbox wake-up

A signed GitHub webhook adapter was merged while timer polling remained as fallback. **Lesson:** event-driven wake-up can reduce latency without removing a simpler recovery path.

### 16. 2026-08-16: single-call experiment pipeline

The normal Codex path was reduced from roughly **3-5 LLM calls to one implementation call**; real verification remains after model execution. A regression showed explicit tests-only goals need separate classification from accidental tests-only changes.

### 17. 2026-08-16: token-efficient verification

Default `unittest` verification uses quiet mode. Success keeps only useful summary output; failures retain a bounded/redacted tail. **Lesson:** keep success quiet and failure evidence actionable.

### 18. 2026-08-17 to 2026-08-19: local-first routing developed and validated

Quota pressure strengthened the local-first design. PR #21 adopted model-agnostic orchestration, deterministic context gathering, real verification, and provider accounting. Qwen2.5-Coder-7B-Instruct Q4_K_M via `llama.cpp` became the default local model, not a permanent architectural dependency.

### 19. 2026-08-18: B2ms resource envelope

The VM was then Standard_B2ms (**2 vCPU, 8 GiB RAM**) with conservative local-server defaults around 8k context, one slot, two CPU threads, and local-only binding. Deployment exposed a missing OpenBLAS dependency. **Lesson:** validate installation on the real target environment.

### 20. 2026-08-18: self-healing operations adopted

PR #22 added authenticated Git/service/log/diagnostic/local-model operations. Review tightened stale-lock cleanup, path boundaries, redaction, and tests. **Lesson:** autonomy should be paired with testable boundaries and auditability.

### 21. 2026-08-19: local-first multi-provider orchestration adopted

PR #21 merged the sequential `Router -> Context Gatherer -> Planner -> Developer -> Verification -> Reviewer` pipeline with local default reasoning and Azure/Codex escalation.

### 22. 2026-08-20: read-only MCP HTTP adapter

PR #27 added a Streamable HTTP MCP surface at `/mcp/`, reusing existing authenticated service logic.

### 23. 2026-08-20: bootstrap MCP write capabilities

PR #29 added explicitly enabled Git/service/test/experiment/router/bootstrap shell tools behind the MCP surface.

### 24. 2026-08-20: dependency synchronization became automatic

PR #32 made runtime dependency synchronization part of source update/startup/deployment. **Lesson:** source and runtime dependency state are one operational concern.

### 25. 2026-08-21: safety-policy false positive fixed

Issue #33/PR #35 fixed `output tokens` being misread as credential access while retaining credential protections. **Lesson:** safety quality includes false-positive measurement.

### 26. 2026-08-21: trusted mailbox became a development command interface

PRs #34/#36 added trusted command messages and removed the development-mode content gate while keeping timeout/output/redaction/environment boundaries.

### 27. 2026-08-21 to 2026-08-22: mailbox Git synchronization became self-healing

PR #38 replaced push-first/`pull --ff-only` deadlock behavior with bounded **fetch -> rebase -> push** retries, aborting real conflicts safely without force-push/reset/discard.

### 28. 2026-08-22: verification timeout became a first-class outcome

Issue #39 separated `test_timeout` from ordinary failure. **Lesson:** distinct failure classes enable distinct adaptation.

### 29. 2026-08-22: persisted adaptive verification timeout

Issue #40/PR #45 starts verification at 300 s and multiplies by 1.5 after timeout, capped at 1,800 s and persisted in SQLite. Full suite: **905 tests OK**. This became the first small persisted self-adaptation loop.

### 30. 2026-08-22: GitHub and MCP control-plane directions

Issues #41-#44 defined GitHub Issues as durable work state and proposed a global MCP gateway where a cheap local LLM routes specialized tools while reducing tool-schema context. The gateway remains experimental.

### 31. 2026-08-22 to 2026-08-24: persistent local-LLM controller adopted

Issue #46/PR #48 moved Nexus from one-shot routing to a bounded ReAct-style loop. Durable sessions preserve discussion/history; tools include operations, bounded memory search, delegation, and experiment start/resume. Delegation returns observations rather than transferring ownership. **Lesson:** routing is not control; control should return to the local agent after observations.

### 32. 2026-08-23 to 2026-08-24: long local inference became persistent work

Real local controller turns took roughly **7.5-14 minutes**. Nexus therefore persists serialized local inference as durable jobs. **Lesson:** slow inference is normal work, not necessarily a hung request.

### 33. 2026-08-24: mailbox observability and repository separation tightened

PR #51 added bounded mailbox poll diagnostics and PR #52 removed the obsolete in-repo mailbox after the separate repository became authoritative.

### 34. 2026-08-25: experiment adoption and verification share one runner

Experiment and adoption verification now share timeout classification, environment isolation, duration/output handling, and adaptive timeout logic. **Lesson:** duplicated verification semantics drift.

### 35. 2026-08-25: manual mailbox task approval removed

Mailbox tasks now enter the worker/controller immediately; redundant manual approval UI was removed while execution/recovery boundaries remained.

### 36. 2026-08-25: GitHub Actions became a reusable verification surface

A Python 3.12 `unittest` workflow runs for PRs, manual dispatch, and `nexus/**` pushes. The long-term goal is to reuse exact matching CI success instead of rerunning tests locally.

### 37. 2026-08-26: command execution became iterative controller work

PR #53 made command execution a tool observation rather than a terminal action, enabling `command -> observation -> command -> answer` loops. **Lesson:** operational actions should normally return control to the agent.

### 38. 2026-08-26: GitHub became the outer experiment lifecycle

PR #54 established `Issue -> branch -> experiment -> verified adoption -> PR -> merge -> Issue completion` around internal isolated worktrees. Runtime experiment identity and human work identity are complementary.

### 39. 2026-08-26: adaptive provider routing with local development fallback

PR #56 made local execution a normal route for simple work, Azure preferred for harder reasoning, and Codex for large implementation, with provider fallback and actual-provider accounting. **Lesson:** local-first means cheapest capable route.

### 40. 2026-08-26 to 2026-08-28: CI/smoke-test hardening exposed environment drift

Actions testing exposed concurrency, output-noise, writable-runtime-state, authentication, and secret-validation assumptions hidden by the long-lived VM. Post-hardening CI succeeded. **Lesson:** CI is also an environment-parity test.

### 41. 2026-08-27: webhook-triggered mailbox sync gained immediate recovery

PR #62 added one immediate retry for transient webhook `sync_failed` results while retaining timer fallback.

### 42. 2026-08-27: persistent Nexus chat added discussion-to-execution flow

Issue #61/PR #63 introduced persistent chat backed by controller/session state. Read-only discussion remains distinct from explicit execution; short approval follow-ups can reconstruct an implementation goal from persisted discussion.

### 43. 2026-08-27 to 2026-08-28: chat became asynchronous, multi-client, and observable

PR #64 made chat durable asynchronous jobs to avoid reverse-proxy timeouts; PR #65 added a PowerShell client; PR #66 fixed command normalization and added chat-job logging after a real `unknown final action 'command'` failure.

### 44. 2026-08-28: chat context was compacted independently

PR #67 introduced a chat-specific context builder: at most six recent non-tool turns and roughly 2,500 history characters plus a compact capability catalog. **Lesson:** context policy should match the interaction surface.

### 45. 2026-08-28: static positive execution allowlists were removed

PR #68 removed remaining application-level positive allowlists while keeping authentication, credential protection, redaction, timeouts, protocol/state validity, and concrete adapter dispatch. Verification: **933 tests OK, 55 skipped** and successful Actions.

### 46. 2026-08-29: rejected operation-chain publication became recoverable

PR #69 persisted rejected chains before publication and safely recovered machine-managed untracked outbox entries. Verification: **51 focused tests** and **936 full-suite tests OK, 55 skipped**.

### 47. 2026-08-29: residual operations allowlists were removed and diagnostics broadened

PRs #70/#72 removed secondary operation/service positive gates, added normal local-model status and bounded memory-sorted process diagnostics. Full suite: **944 tests OK, 55 skipped**. **Lesson:** runtime probes find hidden policy drift.

### 48. 2026-08-29: controller structured-output failures gained bounded repair

PR #73 added schema-constrained controller generation plus one syntax-repair completion. A later valid-but-incomplete action exposed the need for semantic validation too; Nexus added one-shot semantic correction. **Lesson:** grammar/schema and semantic validation are separate layers.

### 49. 2026-08-29: direct controller self-repair exposed a lifecycle question

Nexus directly committed a semantic-response reliability fix with regression coverage. The later mandatory GitHub lifecycle work resolved this ambiguity: normal implementation/self-repair should remain Issue/branch/PR-visible rather than silently writing `main`.

### 50. 2026-08-29: mailbox commands became deferred asynchronous work

Trusted mailbox commands gained persistent process state and deferred collection so long-running/service-disrupting shell work can survive the poll that launched it. **Lesson:** long shell work needs the same durable-job semantics as slow inference.

### 51. 2026-08-29: comment/docstring context cost became a measured experiment

Issue #74 proposes measuring whether redundant production comments/docstrings consume meaningful source context without improving comprehension. Architectural rationale/invariants should be preserved. This remains open research.

### 52. 2026-08-29: internal orchestrator command execution was restored

The async mailbox migration broke state-less internal command calls. A dedicated synchronous compatibility path plus regression test restored them. **Lesson:** persistence semantics depend on the caller; shared adapters may need persistent external and synchronous internal modes.

### 53. 2026-08-29: mailbox request/response correlation became a UI target

Issue #75 records that inbox/outbox correlation already exists in durable state, so conversation-style rendering should reuse it rather than inventing another protocol ID.

### 54. 2026-08-29 to 2026-08-31: runtime observability experiment was not adopted

Issue #76/PR #77 explored heartbeat/state/trace APIs and optional OpenTelemetry export. Although its tests passed, a real Grafana Cloud trial was too cumbersome/noisy; PR #77 was closed without merge. **Lesson:** observability must reduce operator effort, not merely produce telemetry.

### 55. 2026-08-31: a real `--version` smoke test exposed optional GitHub lifecycle

A small version feature passed focused verification but remained only in the VM worktree because GitHub tracking was silently unconfigured. Issue #78 converted GitHub readiness into a lifecycle invariant. **Lesson:** small end-to-end feature tasks expose architecture gaps unit tests miss.

### 56. 2026-08-31: GitHub lifecycle became mandatory and WIP-visible

PR #81 requires Issue/branch identity before implementation, WIP checkpoints, a Draft PR before verification completes, preserved failed/reviewable work, and no silent direct-`main` fallback. **Lesson:** visible intermediate work is part of durability.

### 57. 2026-08-31: large modules were split and compatibility regressions became test data

Browser, mailbox, run-store, and experiment-service code were modularized. PR #82 repaired stale imports/patch targets/redaction/frontend polling and restored **949 tests OK, 55 skipped**; PR #87 split the ~2,570-line experiment service behind a compatibility façade. **Lesson:** behavior-preserving refactors still change integration surfaces.

### 58. 2026-08-31: logging UX shifted toward manual, unified, failure-focused inspection

Issues #84/#86 defined a manual-refresh unified structured feed, submission-time correlation, collapsed/lazy details, and failure-focused verification output. **Lesson:** logging quality is signal-to-noise, not volume.

### 59. 2026-09-01: unified log aggregation merged but exposed a cross-service gap

PR #88 added authenticated `GET /logs` aggregation across persisted experiment/mailbox logs. Real chat testing showed failures before experiment creation were still invisible, proving a unified reader is not a unified logging system.

### 60. 2026-09-02: application-wide structured logging completed

PR #89 merged centralized structured logging across API, agent, mailbox, CLI, and workers, with `INFO+` default, optional deeper levels, chat `job_id`/`session_id` correlation, transient IDs for early mailbox rejection, and explicit log-storage errors. This closes the major pre-experiment visibility gap identified by the real chat failure.

PR #94 then renamed the misleading `experiment_logger` module to `central_logging` and added a strictly allowlisted unauthenticated `GET /logs/public` view backed by the same multi-source feed rather than experiment-only diagnostics.

**Lesson:** logging should be one emission path from submission through terminal state, with public/private views differing by exposure policy rather than by data source.

### 61. 2026-09-02: duplicate tool-call loops gained persistent non-progress detection

A real controller run repeatedly retried the same ineffective tools/commands until context and memory pressure became dangerous. Issue #92 / PR #95 introduced canonical tool signatures, persistent SQLite reservations/results, exact-duplicate and A→B→A cycle detection, configurable step/cycle limits, state-sensitive retries, and controlled clarification instead of unbounded looping. A retry after relevant state change requires an explicit reason. Full suite: **1050 tests OK, 55 skipped**.

**Lesson:** an agent loop needs explicit progress semantics. Persistence alone can make a bad loop more durable; duplicate/cycle detection must survive service restarts and distinguish justified retries from repetition.

### 62. 2026-09-02: VM memory resilience improved after local-model pressure

The local-model workload exposed that the small Azure VM had no useful swap safety margin and that forcing non-mmap model loading made memory pressure less recoverable. PR #96 added an **8 GiB swap** setup on the Azure resource disk with per-boot recreation and low swappiness. PR #98 removed `--no-mmap`, restoring llama.cpp's default memory-mapped model loading so Linux can reclaim file-backed model pages under pressure.

The historical B2ms measurement remains useful, but the current VM size should no longer be inferred from that old benchmark.

**Lesson:** local-model reliability depends on host virtual-memory behavior as well as model/context size; avoid turning reclaimable model pages into unnecessarily pinned anonymous memory.

### 63. 2026-09-02: runtime execution stopped depending on LLM interpreter guesses

A smoke task failed because the controller guessed `python`, which was absent from PATH, then drifted into memory-search loops. PR #97 added deterministic Python interpreter resolution (`configured -> active venv -> project venv -> sys.executable -> python3`), a typed `python_run` tool, validated script/module/cwd arguments, and at most one deterministic command-not-found correction without another LLM call. Unknown executables now produce a compact `command_not_found` observation suitable for future tool discovery.

**Lesson:** host/runtime facts that can be resolved deterministically should not consume model reasoning. Tool interfaces should encode stable runtime semantics instead of making the LLM guess machine-specific executable names.

### 64. 2026-09-02 to 2026-09-03: persistent-controller prompt growth gained an adopted budget layer

A real run showed that the controller resent accumulated task/history and tool observations on every local-model turn, with some observations duplicated through persisted history. Prompt size grew roughly quadratically and contributed to `llama-server` being OOM-killed.

PR #99 was merged on 2026-09-03. It now budgets local turns against both model context and host available memory, excludes current-run observations from persisted-history replay, compacts old/oversized observations to durable event references, deduplicates nested memory-search content, logs budget decisions, and escalates an over-budget turn to an external provider. If no external provider is usable, Nexus returns controlled `missing_capability` rather than risking another local OOM.

**Lesson:** model context, persisted memory, in-memory observations, and host RAM are separate budgets; context must be bounded across turns rather than only per tool result.

### 65. 2026-09-03: the global tool-gateway idea became an implemented prototype

Issue #44 / PR #100 merged an OperationsRegistry-backed `find_tool(goal)` gateway. The controller no longer needs the full operation catalog embedded in each prompt: it asks for a capability, a cheap local resolver considers at most five lexical candidates, and only one resolved read-only operation schema is returned. The prototype records legacy-catalog versus selected-schema size, resolver prompt/output size, LLM calls, latency, and wrong-tool-selection status.

This is adopted as a prototype, not yet evidence that model-mediated discovery is better than direct tool exposure. **Lesson:** tool discovery can itself be a narrow capability with measurable token/latency/selection trade-offs.

### 66. 2026-09-03: exact-commit GitHub CI became authoritative verification evidence

A real version-task experiment exposed a verification bug: GitHub Actions passed **1,129 tests in about 66 seconds** for the checkpoint commit, but Nexus reran the full suite locally, hit the 450-second timeout, and incorrectly marked the experiment `timed_out`.

Issue #103 / PR #104 fixed this by polling the `tests.yml` workflow for the exact checkpoint SHA. Completed CI success or failure is now authoritative; local isolated verification runs only when matching CI never starts, is unavailable, or does not finish within a bounded wait. The selected verification source and fallback reason are logged. PR verification reported **15 focused tests**, **1,057 full-suite tests passed, 55 skipped**, and successful Actions run #90.

**Lesson:** duplicate verification can make a successful experiment fail; remote evidence is reusable only when it is tied to the exact commit.

### 67. 2026-09-03 to 2026-09-04: the logs UI became one source-aware Nexus timeline

Operator testing showed that centralized instrumentation was still hard to use while the website retained the old experiment selector and separated journal/service output. Issue #105 / PR #106 replaced that model with one chronological timeline combining structured logs with bounded journal entries from `agent-api`, `nexus-mailbox`, and `llama-server`. The merged view supports level/source/phase/text filters, correlated expandable details, cursor-based older-page loading, and per-source errors so one unreadable source does not blank the timeline.

PR #106 merged on 2026-09-04. **Lesson:** instrumentation and operator presentation are both part of observability; UI boundaries should match runtime reality.

### 68. 2026-09-04: a version smoke test exposed an ambiguous controller tool protocol

After `find_tool(goal)` was introduced, a real end-to-end version task produced `unknown final action 'find_tool'`: the small local controller emitted a known tool name through the final-action envelope rather than the tool envelope. A compatibility normalization fixed that immediate regression, but a follow-up run then reached `find_tool` without the required goal.

Issue #111 / PR #112 therefore simplified the outer controller protocol instead of adding more normalization rules: tool work now uses one explicit action field with `find_tool(goal)` for discovery and `call_tool(tool, arguments)` for invocation; normal final outcomes remain separate. Persisted grants, loop protection, context budgeting, memory, delegation, command/Python capabilities, and legacy persisted-session read compatibility were retained. Focused controller verification: **47 tests passed**.

**Lesson:** small-model reliability benefits more from an unambiguous protocol than from accumulating normalization heuristics around an ambiguous one.

### 69. 2026-09-04: actionable verification failures became bounded repair feedback

A version-feature experiment produced an otherwise useful implementation but exact-commit CI failed with **1 error out of 1,167 tests, 55 skipped**. Nexus immediately marked the experiment failed even though the failure was deterministic and directly related to the new code. Issue #110 changed this behavior: ordinary actionable test failures can now trigger up to **two focused repair iterations** in the same experiment/worktree, each followed by verification.

Verification attempts are persisted with iteration, commit/evidence, command, bounded output/error, and a failure fingerprint. Repeated identical failures stop the loop, and repair timeout/error/exhaustion still ends cleanly. **Lesson:** the first red verification is often feedback, not a terminal state; autonomous repair must remain bounded, observable, and progress-sensitive.

### 70. 2026-09-04: tool discovery gained semantic input validation and recovery

The simplified tool protocol exposed another real small-model failure: an executable prompt example used the placeholder `plain-language capability needed`, which the controller copied literally into `find_tool`. The resolver treated it as a genuine unmatched request, the controller repeated it, and the existing non-progress guard eventually stopped the session.

Issue #113 / PR #114 replaced placeholder-style executable examples with concrete capability requests and added an `invalid_goal` result distinct from valid `not_found`. Invalid/empty/generic discovery goals now return concise corrective guidance and permit a changed reformulation, while repeated invalid requests remain subject to loop protection. Focused validation: **40 tests passed**.

**Lesson:** prompt examples are part of runtime behavior for small models, and malformed requests must be distinguished from genuinely missing capabilities.

### 71. 2026-09-04: post-fix smoke testing reached successful exact-commit CI

After the tool-protocol and `invalid_goal` fixes were deployed, Nexus reran the version-feature task through the normal GitHub lifecycle. Draft PR #116 shows the experiment reached an exact-commit GitHub Actions result of **success** instead of failing in controller dispatch or verification orchestration. The draft currently implements a canonical application version shared by `agent.py --version`, `/health`, and chat/API version reporting, with runtime-only imports deferred so the informational CLI flag works in lightweight environments.

PR #116 remains **open/draft and is not adopted on `main` yet**. The durable result is the end-to-end evidence that the repaired controller/tool/verification path can now carry a feature task through implementation and successful CI.

## What Nexus currently learns

Nexus accumulates durable information from:

- code and tests,
- project/idea/decision/history documents,
- SQLite experiment, controller/session, chat-job, local-LLM-job, GitHub-work mapping, operation, tool-loop, verification-run, and adaptive service state,
- mailbox `context` knowledge,
- experiment outcomes, central structured logs, diagnostics, activity, usage, CI evidence where integrated, and cost data where available.

It does **not automatically inherit the full reasoning history from ChatGPT conversations**. `PROJECT_HISTORY.md` preserves durable decisions, rejected alternatives, failures, and research directions that would otherwise be lost.

## Recommended use as Nexus project memory

Do not inject this entire file into every model call. Compare at least:

1. no history context,
2. a short generated history summary,
3. retrieval of only relevant history sections.

Measure relevance of proposed ideas, duplicate work, consistency with previous decisions, token use, latency, memory pressure, and cost. Project memory should itself be an experiment.

## Current architectural picture

Current `main` includes:

- a persistent small Azure VM with explicit swap support; the old Standard_B2ms measurement is historical rather than a statement of current SKU,
- persistent local `llama.cpp` capability using mmap plus a persistent local-LLM controller and durable local-inference jobs,
- prompt budgeting/compaction across persistent controller turns with host-memory-aware external fallback,
- persistent authenticated Nexus chat with explicit discussion/execution semantics, asynchronous jobs, and shared web/PowerShell clients,
- compact chat-specific context separate from task orchestration,
- schema-constrained controller decisions with one bounded syntax/semantic repair attempt,
- adaptive local/Azure OpenAI/Codex routing with provider fallback and actual-provider accounting,
- iterative command/operation/memory/delegation loops with persistent duplicate/cycle/non-progress detection,
- deterministic typed Python execution and one bounded command-resolution retry,
- a two-stage OperationsRegistry tool gateway using unambiguous `find_tool(goal) -> call_tool(tool, arguments)`, persisted grants, selected-schema exposure, and `invalid_goal` versus valid `not_found` semantics,
- single-call Codex experiment execution,
- worktree-isolated experiments with shared real-tool verification/adoption, distinct timeout state, persisted adaptive timeout, and bounded focused repair after actionable verification failures,
- persisted verification-run evidence with repeated-failure detection,
- exact-commit GitHub Actions evidence as authoritative verification with bounded local fallback,
- mandatory GitHub-backed implementation lifecycle using Issues, Issue-linked branches, WIP checkpoints, Draft PRs, verification, merge, and Issue completion,
- GitHub Actions verification for PRs and `nexus/**` pushes,
- SQLite state and durable project/idea/decision/history documents,
- separate Git-backed mailbox repository with crash-safe publication, rejected-chain recovery, fetch/rebase/push synchronization, deferred trusted command execution, and synchronous internal-command compatibility,
- signed webhook wake-up with timer fallback and bounded recovery diagnostics,
- modularized mailbox, run-store, experiment-service, central-logging, and browser-client code,
- application-wide central structured logging from work submission onward, with authenticated full `/logs` and allowlisted public `/logs/public` views,
- one source-aware operational log timeline combining structured logs with bounded Nexus service journals while retaining redaction and cursor pagination,
- authenticated audited operations for local model, services, logs, processes, Git, and diagnostics,
- authenticated MCP HTTP surface with read-only inspection plus explicitly enabled bootstrap write tools,
- trusted-development mailbox task auto-execution,
- automatic API dependency synchronization,
- invariant protections for auth, secrets, redaction and state validity rather than broad static positive execution allowlists,
- tests and policy boundaries.

Conversation-style Inbox/Outbox rendering (Issue #75), source-context hygiene (Issue #74), measurement of the two-stage tool gateway, and broader validation of bounded verification repair remain open work/research. The Grafana/OpenTelemetry experiment remains rejected. Draft PR #116 is successful CI evidence but is not adopted architecture until merged.

## Important lessons so far

1. **Autonomy requires observability.** A result without evidence is hard to improve.
2. **Self-modification needs evaluation and rollback.** Generating code is easier than deciding whether to adopt it.
3. **Different state types may need different repositories.** Mailbox traffic and source integration had incompatible Git write patterns.
4. **Communication needs delivery semantics.** Persistence, locking, retry, recovery, idempotence, and safe reconciliation matter.
5. **Cheap reasoning matters.** Local/small models should handle work that does not justify an expensive provider.
6. **Context is a resource.** Retrieve, deduplicate, compact, and budget relevant memory instead of blindly increasing context.
7. **Failures are research data.** CLI mistakes, loops, OOMs, merge conflicts, deployment failures, rejected experiments, and timeouts should influence the next iteration.
8. **Optimize orchestration before buying capacity.** Fewer LLM calls, deterministic runtime resolution, quieter tools, and bounded context can reduce cost before changing models/VMs.
9. **Real tools are the source of truth for verification.** LLMs may interpret evidence but should not invent pass/fail state.
10. **Separate capability architecture from model identity.** Routing should survive model replacement.
11. **Target-environment validation matters.** Deployment scripts can fail despite clean unit tests.
12. **Safety can enable more autonomy.** Authentication, secret isolation, redaction, liveness checks, bounds, and audit logs support broader execution.
13. **Adapters should reuse domain logic.** MCP/API/GitHub/public views should expose existing tested logic rather than fork behavior.
14. **Deployment includes dependencies and host memory behavior.** Source, packages, swap, mmap, and service configuration jointly determine runtime reliability.
15. **Safety policies need precision, not only strictness.** False positives can block useful experiments.
16. **Failure taxonomy enables adaptation.** Timeout, command-not-found, missing capability, invalid tool goal, duplicate loop, and ordinary failure should not collapse into one outcome.
17. **Simple persisted adaptation counts.** Automatic timeout adjustment is already a self-improvement loop.
18. **Routing is not control.** The controller should regain control after tool/delegate observations.
19. **Long inference and shell work should be durable.** Work lifetime should not equal transport lifetime.
20. **Verification semantics should be shared.** Local, adoption, and remote evidence should converge on one meaning of verified.
21. **Approval gates are scaffolding, not architecture.** Stronger persistence/evidence/recovery can replace redundant manual steps.
22. **GitHub work identity and runtime experiment identity are complementary.** Human-readable work tracking need not replace isolated internal worktrees.
23. **The cheapest capable route should win.** Escalation should be justified by evidence.
24. **Actions should usually produce observations, not terminate reasoning.** Iterative execution is more agentic than one-shot dispatch.
25. **CI is an environment-parity test.** Runtime state/auth/event assumptions must remain valid outside the long-lived VM.
26. **Conversation should survive the decision to execute.** Persisted discussion can become a self-contained implementation goal.
27. **Context should be surface-specific and turn-budgeted.** Chat compaction alone does not prevent persistent-controller history/observation duplication.
28. **Prefer invariant safety properties over brittle positive lists.** Broader autonomy can coexist with strong boundaries.
29. **Persist intent before publication on every terminal path.** Rejected/error results need crash recovery too.
30. **Structured output has grammar and semantic layers.** Both need bounded repair.
31. **Runtime probes find policy drift.** Tests/code search alone may miss secondary gates.
32. **Durability semantics are call-site dependent.** External persistent work and internal synchronous orchestration can legitimately differ.
33. **Operational simplicity is an architecture criterion.** Reject observability stacks that add more burden than insight.
34. **Lifecycle invariants must fail closed before implementation.** Mandatory GitHub state cannot be best-effort.
35. **Visible intermediate work is part of durability.** WIP commits and Draft PRs preserve inspectable evidence.
36. **Behavior-preserving refactors still change integration surfaces.** Compatibility coverage matters.
37. **Logs should optimize for diagnosis.** Keep success quiet and detailed evidence available on demand.
38. **Aggregation is not instrumentation.** Structured logging must start at submission/process boundaries.
39. **Agent loops need progress invariants.** Durable state without duplicate/cycle detection can preserve failure indefinitely.
40. **Deterministic host facts should be tools, not guesses.** Interpreter/runtime resolution is cheaper and more reliable outside the LLM.
41. **Model context and host RAM are independent limits.** Both must be budgeted; duplicated persistent/in-memory context can create superlinear growth and OOM failure.
42. **Exact-commit CI evidence should be reused, not duplicated.** Redundant local verification can turn a known-good checkpoint into a false timeout.
43. **Tool catalogs can be retrieved on demand.** A narrow discovery interface may save context, but its routing quality and latency must be measured.
44. **Operational UI boundaries must match runtime reality.** Central logs become useful when the frontend presents all relevant runtime sources as one diagnosable flow.
45. **A failed verification is feedback before it is a terminal state.** Automatic repair needs bounded iterations, persistent evidence, and repeated-failure detection.
46. **Simplify protocols before adding normalization.** Small local models benefit from explicit discovery/execution actions more than overlapping envelopes plus compatibility heuristics.
47. **Prompt examples are executable behavior for small models.** Use concrete examples and distinguish malformed input from genuinely missing capabilities.

## Open research directions

- project-memory retrieval from this history rather than full-file injection,
- measured token/cost/quality impact of single-call execution and compact tool output,
- benchmark current local models inside the actual VM memory envelope, including usable context, latency, mmap/swap behavior, and OOM margin,
- test a two-pass local workflow where a first pass gathers/reduces repository context and a second implements,
- measure controller/chat success, command/tool loops, duplicate/cycle prevention, clarification/resume, missing-capability behavior, structured-output repair rate, and human intervention,
- measure local-LLM/chat job queue/runtime/token rate/restart recovery/resource use,
- quantify adaptive routing by task class, actual provider, fallback, success, latency, tokens, and cost,
- validate merged PR #99 against the original OOM reproduction; measure prompt growth per turn, RAM, cache reuse, latency, external-fallback rate, and task quality,
- measure whether old observation compaction to durable event references still provides enough evidence for correct later decisions,
- measure source-comment/docstring context reduction without losing architectural rationale (Issue #74),
- validate conversation-style mailbox rendering against linked/pending/multi-response cases (Issue #75),
- measure whether the merged unified Nexus log timeline makes cross-service failures diagnosable quickly, including partial-source failures, pagination, and error visibility,
- measure whether submission-time job/session/mailbox correlation makes failures diagnosable without external tracing,
- measure authoritative exact-SHA CI reuse: wait time, local fallback frequency, false timeout elimination, and end-to-end experiment latency,
- measure bounded verification repair: recovery rate, iterations, repeated-failure stops, extra model/CI cost, and reduction in human intervention,
- evaluate whether self-healing operations and duplicate-loop prevention materially reduce human intervention,
- evaluate MCP as the standard control path and narrow temporary bootstrap capabilities over time,
- measure whether the mandatory GitHub lifecycle improves fire-and-forget development, traceability, recovery, and review,
- add concise GitHub Issue lifecycle progress without duplicating detailed Nexus logs,
- benchmark the two-stage `find_tool(goal) -> call_tool(tool, arguments)` gateway against direct catalog exposure: schema-token reduction, routing accuracy, resolver latency/calls, wrong-tool selection, invalid-goal recovery, and task success,
- investigate missing-capability detection and structured issue creation before automatic tool generation,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- quantization/fine-tuning only where evidence justifies it,
- multi-agent specialization only when benefit exceeds complexity.

## Current next-step candidates

1. **Post-fix version E2E:** review/complete draft PR #116 and verify the deployed `find_tool -> call_tool -> implementation -> exact-CI` path, including bounded repair when a real implementation failure is introduced.
2. **Real post-OOM rerun:** repeat the feature task that previously caused prompt growth/tool looping and measure whether PRs #95/#97/#99 now finish without repetition or local OOM.
3. **Tool-gateway measurement:** compare the two-stage discovery/invocation protocol with direct catalog exposure and quantify context reduction versus resolver cost, invalid-goal recovery, and wrong selection.
4. **Verification-repair measurement:** run representative repairable and non-repairable failures and measure recovery, repeat detection, latency, model calls, CI runs, and intervention.
5. **Authoritative-CI measurement:** run several experiments and verify exact-SHA CI reuse removes duplicate local suites without accepting stale evidence.
6. **Adaptive-routing measurement:** run representative simple/medium/large tasks and measure selection, fallback, success, latency, tokens, cost, and intervention.
7. **Persistent job/resource measurement:** quantify local-model/chat queue/runtime/token behavior plus RAM/swap/mmap pressure and recovery across restarts.
8. **Local-model/context benchmark:** compare local candidates under identical actual VM limits and test two-pass context gathering versus larger context.
9. **Single-call before/after measurement:** repeat a representative early experiment and compare quality, tokens, runtime, and cost.
10. **Project-memory experiment:** retrieve only relevant history and measure idea relevance, repetition, prompt size, and task success.
11. **Mandatory GitHub lifecycle measurement:** verify reuse/resume/conflict/provider-failure behavior across multiple experiments on one Issue/branch/PR.
12. **Source-context hygiene experiment:** run Issue #74 on a small module set and compare context reduction against readability and unchanged behavior.
