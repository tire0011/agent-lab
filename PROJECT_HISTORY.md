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

### 72. 2026-09-04: green verification exposed incorrect final-completion semantics

The successful version experiment also exposed that the mailbox task was reported `succeeded` while its Draft PR was still unmerged and the live Nexus checkout did not contain the feature. Issue #119 records the required distinction between **implementation produced**, **verification passed**, **integration**, **deployment/adoption**, and **final task completion**. Verification evidence must remain visible without implying that a feature is live.

**Lesson:** `verification=passed` is evidence, not the completion boundary for a development task.

### 73. 2026-09-04: tool discovery exposed a grant-contract ambiguity

A real `find_tool` call returned `status=not_found` with `related_tools`, after which the controller incorrectly treated a suggested operation as granted and hit `operation_not_granted`. Issue #117 defines the contract more strictly: only an explicit `status=found` selection may grant a tool; related tools are suggestions only. This remains open.

**Lesson:** discovery suggestions and execution authority must be separate machine-enforced concepts, especially for small models.

### 74. 2026-09-04: missing executables became a capability-learning direction

During the same E2E work Nexus attempted `rg`, which was absent on the VM, then recovered via another path. Issue #120 proposes treating `command_not_found` as a durable capability-gap signal: prefer an equivalent existing operation, install/provision a useful tool on the disposable VM when appropriate, create/reuse a deduplicated GitHub capability issue for recurring gaps, and eventually promote repeated needs into reusable OperationsRegistry tools.

**Lesson:** runtime tool failures can become structured inputs to capability growth rather than one-off shell errors.

### 75. 2026-09-05: verified tasks gained persisted autonomous finalization through live validation

Issue #121 / PR #122 closed the largest remaining development-loop gap. Verified development mailbox tasks no longer become terminal immediately after experiment success. Nexus now reuses `ExperimentAdoptionService` and persists post-verification phases through PR finalization/merge, deployment, restart request, restart recovery, and task-specific live validation before publishing final success.

Late-stage retries are bounded and remain attached to the same task/experiment. The restart is an explicit persisted boundary, so work can resume after the services intentionally restart. General live validation checks `/health`; version-related tasks additionally run `agent.py --version` and require the live health version to match. Focused tests cover verified-but-unmerged state, adoption start, restart/resume, live validation, and the final success boundary.

PR #122 is merged on `main`, but the original version-feature PR #116 is still open/draft. Therefore the capability is **implemented but not yet proven by the intended real end-to-end acceptance run**.

**Lesson:** autonomous development is complete only when verified code is integrated and the requested behavior is observed in the live target; lifecycle state must survive the deployment action that restarts the agent itself.

### 76. 2026-09-05 to 2026-09-06: self-restarting mailbox commands gained evidence-based recovery

A real Nexus refresh exposed a remaining restart-boundary bug outside the development-task finalizer: the refresh updated `/agent` and restarted services successfully, but the detached mailbox command runner was killed before recording its exit code, so a later poll falsely reported `Exit code: 255`. Issue #123 captured the broader requirement that restart-sensitive Inbox work should resume or reconstruct its outcome rather than treating process disappearance as failure.

PR #124 merged the concrete command-channel fix. Before a command starts, Nexus persists a local pre-execution Git HEAD plus an optional declared `recovery_services` completion contract. If the runner later disappears without an exit code, recovery compares the persisted baseline with the current clean/upstream Git state and, when declared, expected systemd service state. It returns **verified success**, **verified failure**, or **unknown** rather than guessing; Git convergence alone is deliberately insufficient for a multi-step command unless the message explicitly declares that as the complete expected state. The recovered verdict is cached so repeated polls/restarts reuse the same result and do not duplicate the reply.

Regression coverage reproduces a command that commits/pushes and then kills its own runner, plus failed-convergence, unknown-evidence, service-state, and idempotence cases. The mailbox suite passed **223 tests**. A full-repository run still had **27 failures already present on the base branch**, so they were treated as baseline debt rather than regressions from PR #124.

**Lesson:** process exit status is not authoritative across intentional self-restarts. Persist expected postconditions before restart-sensitive work and reconstruct outcomes from state evidence; when evidence is insufficient, report `unknown` instead of inventing success or failure.

### 77. 2026-09-06: real finalization exposed an unresolved adoption restart gap

The intended end-to-end continuation of the verified version experiment reached the adoption/finalization layer and exposed a separate restart bug not covered by the mailbox-command recovery. After a service restart, an adoption left in `queued`/`adopting` was converted to terminal `interrupted`, and the original task ended `needs_review` even though the mailbox and services were healthy.

Issue #125 defines startup recovery for the higher-level adoption lifecycle: reconstruct progress from durable GitHub/Git/service evidence, preserve the same task/experiment/adoption/Issue/PR identity, resume from the latest confirmed phase, avoid duplicate merge/deploy work, and make repeated startup recovery idempotent. A restart alone must not be treated as failure.

**Lesson:** restart resilience must exist at every persisted lifecycle layer. A durable outer coordinator cannot rely on a lower-level service that turns non-terminal state into `interrupted` on startup.

### 78. 2026-09-07: full-history log search and cross-ID order tracing merged

PR #126 fixed a major usability gap in the unified logs: search previously filtered only the newest bounded page, so older matching events looked nonexistent. Search is now pushed into the log readers and scans historical candidate files and journals under explicit budgets; responses report `truncated` when the scan cannot prove completeness. Matching includes correlation IDs plus command/cwd/stdout/stderr/exception detail.

The PR also added `GET /logs/trace/{reference}`, which expands correlations across changing identities such as inbox filename, mailbox task, experiment, and session/job IDs to reconstruct the reachable execution chain from any one reference. Rotation was enabled for experiment/task/chat logs so reaching the 5 MB ceiling no longer silently stops recording. Per decision D-012, log read endpoints are now unauthenticated while write endpoints remain authenticated; literal values of configured high-value credentials are scrubbed in addition to pattern-based redaction.

Verification added **40 focused tests**. The branch had the same **29 pre-existing full-suite failures** as clean `main`, with no new regression. A browser/server E2E showed **408 entries** in the default view, found one deliberately old search match that `main` missed, and reconstructed a **5-entry** mailbox-to-verification chain from only the inbox filename.

**Lesson:** observability is useful only if old evidence is actually discoverable and identity changes can be traversed. Bounded scans must expose incompleteness explicitly rather than returning a misleading empty result.

### 79. 2026-09-07: controller JSON escaping remains an open reliability edge case

A real chat implementation request still produced `TaskOrchestratorError: Failed to parse JSON` because model output contained a JSON-invalid backslash escape inside a command string. This shows that the existing schema/syntax-repair work does not yet make arbitrary model-generated command strings robust across all quoting cases.

**Lesson:** avoid fragile nested string encoding where possible; otherwise JSON repair/validation must specifically normalize or reject invalid escape sequences before orchestration state is lost. No merged fix is recorded yet.

### 80. 2026-09-07: adoption restart recovery was implemented and merged

Issue #125 / PR #127 changed restart handling from terminal interruption to durable reconstruction. If GitHub already shows the PR merged, Nexus records the real merge commit without merging again; otherwise it resumes the same adoption record from persisted Git state. The experiment commit SHA is persisted as soon as it exists so recovery can continue from a clean post-commit worktree. Recovery attempts are bounded, and genuinely unrecoverable or conflicting concurrent state becomes `needs_review` instead of being guessed through.

Regression coverage included restart before merge, after commit, after merge, repeated idempotent startup recovery, exhausted recovery budget, and inconsistent multiple stuck adoptions. The branch had the same **27 pre-existing failures** as clean `main`, with **1,291 passing versus 1,286** on the base.

**Lesson:** self-restart recovery should resume the same durable lifecycle identity and derive completion from external/persisted evidence; restart itself is not a failure signal.

### 81. 2026-09-07 to 2026-09-09: finalization state was rebound from transient approvals to the experiment lifecycle

After PR #127, a real retry reused the existing experiment and already-green exact-commit CI evidence, but autonomous finalization failed because `task_finalizations` was unique by experiment while lookup began from a new mailbox approval ID. Issue #128 / PR #133 fixed this by looking up the durable row by `experiment_id` and atomically rebinding it to a later approval when the previous owner is terminal.

The rebind preserves lifecycle progress such as phase, restart timestamp, merge commit, and live-validation evidence; only per-approval retry/error state is reset. Active-owner takeover and stale writes are rejected, so two approvals cannot drive the same experiment concurrently. Regression coverage includes repeated retries and continuation from `restart_requested` through live validation.

**Lesson:** persistence keys should match the lifetime of the state they own. A transient transport/request identity may own a retry budget, but not a lifecycle designed to survive retries and re-approvals.

### 82. 2026-09-07 to 2026-09-08: mailbox retention became state-aware and self-maintaining

Issue #129 / PR #130 fixed two accumulating queue problems: the outbox had grown to **233 responses** with no retention policy, and exhausted inbox messages were re-read and re-skipped forever. Retention now runs inside the mailbox processor with a configurable **5-day default** (zero or invalid configuration disables deletion), removes only terminal exhausted inbox items after logging their failure context, and protects any outbox file still needed by incomplete mailbox/operation-chain/approval publication state.

A copy of the real mailbox was reduced from **233 to 37 outbox files**, deleting **196** old responses in one clean commit while leaving the working tree clean. Full-suite comparison showed the identical **27 pre-existing failures**, with **1,310 passing versus 1,292** on clean `main`.

**Lesson:** retention of durable queues must be state-aware. Git age alone cannot tell whether a file is still required to finish a persisted commit/publish transition; cleanup belongs next to the state machine that knows those invariants.

### 83. 2026-09-08: log readability fixes merged after mobile diagnosis

Phone use of the unified log timeline exposed three stacked defects: the bounded feed rendered oldest-first so current events were buried, permanently skipped mailbox messages flooded INFO on every poll, and mailbox records were misclassified as `source: experiment`. Issue #131 / PR #132 fixed the main feed to newest-first while keeping causal trace views chronological, moved terminal-skip repetition to DEBUG while retaining skipped counts in poll summaries, and corrected both new and historical mailbox source classification without migration.

PR #132 merged after browser verification at phone viewport plus **12 focused tests**. Its full-suite comparison reported the same **27 pre-existing failures**, with **1,322 passing versus 1,310** on the base.

**Lesson:** observability can fail at presentation semantics even when the underlying events exist. Ordering, noise policy, and source classification are part of diagnostic correctness.

### 84. 2026-09-09: adoption began reusing exact-commit CI with integration-aware precedence

Issue #134 / PR #135 aligned adoption with the earlier exact-SHA verification rule. Green GitHub Actions evidence is reusable only for the persisted checkpoint SHA and only while that checkpoint's parent still matches the current target-branch base. If `main` moved, Nexus runs integration validation on the current-base worktree instead of treating stale green CI as sufficient.

Only explicit GitHub Actions `failure` is authoritative red evidence. Cancelled, timed-out, skipped, neutral, unavailable, or transient lookup failures fall back to local integration validation. The workflow run's actual `head_sha` is used as evidence identity.

**Lesson:** checkpoint correctness and integration correctness are different facts. Exact-commit CI can replace duplicate tests only while the integration base it was verified against is still valid.

### 85. 2026-09-09: stale adoption failures became evidence-revisable but bounded

A later retry exposed that an exhausted finalization retry budget could permanently freeze an adoption whose old local failure never reached a real test verdict. Issue #136 / PR #137 introduced persisted failure assessment: a non-zero test exit code remains terminal evidence, but a no-verdict failure such as timeout/git/worktree failure may be superseded when authoritative green CI now exists for the exact checkpoint.

Superseded failures are archived for audit, the same adoption identity is reused, and a separate stale-retry budget permits at most **two** such reopenings. The coordinator consults this evidence only when the ordinary retry budget is exhausted, preserving the fresh bounded retry semantics added for approval rebinding. Full suite: **1,287 tests with one pre-existing environment-sensitive failure**; 24 new focused tests covered the stale-failure path.

**Lesson:** durable failure state should distinguish an implementation verdict from failure to obtain a verdict. New authoritative evidence may invalidate the latter, but recovery must be bounded and auditable.

### 86. 2026-09-09: authoritative CI evidence became durable across restart and credential changes

The stale-failure work exposed another restart dependency: `wait_for_ci` observed authoritative CI but only logged it, so later adoption/recovery had to query GitHub again. Issue #138 / PR #139 added a SQLite `github_ci_verifications` store keyed by commit SHA. Successful and failed authoritative observations are persisted with run metadata; unavailable results are deliberately not cached.

Adoption now consults persisted exact-SHA evidence before any live lookup. A later authoritative observation for the same checkpoint supersedes the older one, while a new checkpoint naturally has no reusable row. This lets restart recovery continue even if GitHub credentials or live API access are temporarily unavailable, without reusing evidence from another commit or persisting secrets.

PR #139 added **15 focused tests**. The full suite reported **1,305 tests with one pre-existing environment-sensitive failure**. A real single test still does not cover the entire adoption -> merge -> deploy -> restart recovery -> live validation -> succeeded chain, so that remains an important E2E target.

**Lesson:** if a fact is authoritative enough to drive a durable lifecycle, it must itself be durable and keyed to the exact identity it proves.

### 87. 2026-09-10: pre-#139 authoritative CI evidence gained offline migration

The real long-running version-feature E2E exposed a migration gap: experiments verified before PR #139 already contained accepted GitHub Actions evidence in historical verification state, but the new durable `github_ci_verifications` table remained empty on resume. Without current GitHub credentials, stale-adoption assessment therefore incorrectly fell back to `no_ci_evidence`.

Issue #140 / PR #142 fixed this by backfilling only explicitly identified historical GitHub Actions verification records during database initialization. The migration binds evidence to the exact checkpoint and reconstructed checkpoint-parent integration base, requires matching structured run identity/conclusion/exit code, never upgrades generic/local test success to CI evidence, performs no live GitHub lookup, and uses idempotent insert-only semantics so newer durable evidence cannot be overwritten.

**Lesson:** schema evolution must migrate already-authoritative lifecycle facts, not only persist observations made after the new schema exists.

### 88. 2026-09-10: stale-adoption evidence decisions became first-class log events

Issue #141 / PR #143 made the previously hidden CI/adoption decision path visible in normal structured logs. Nexus now emits stable events for persisted CI lookup, reuse/rejection, live fallback/result, stale-failure assessment, requeue, and finalization blocking. Rejection reasons such as `target_base_moved`, `incomplete_base_evidence`, `checkpoint_mismatch`, and `red_verdict` are machine-readable; decision events are phase-tagged where the same evidence is evaluated in assessment versus adoption.

The change added 14 focused logging tests and a regression that reconstructs the #115/#116 failure from the emitted JSONL feed alone. The full suite reported **1,329 tests with one pre-existing environment-sensitive failure**.

**Lesson:** an observable outcome is not enough; the evidence-selection and rejection decisions that caused it must also be durable, structured, and machine-readable if Nexus is expected to diagnose itself.

### 89. 2026-09-10: reusable local-LLM failure analysis became a Nexus capability

Issue #145 / PR #146 added a subject-agnostic `FailureAnalysisService`. It accepts caller-supplied or log-derived bounded/redacted evidence, asks the configured local Nexus LLM for a structured diagnosis, and persists summary, category, confidence, evidence-linked hypotheses, missing information, recommended checks/actions, and self-recovery/escalation indicators in SQLite. Malformed model output or analyzer failure degrades to a persisted `analysis_unavailable` result and cannot replace or corrupt the original lifecycle failure.

Automatic best-effort integration currently covers failed/`needs_review` development tasks and finalization failures, while diagnostic API endpoints allow manual inspection. A fixture based on the real #115/#116 event sequence verifies that the analyzer can identify the moved-base/missing-integration-fallback blocker. **40 focused tests passed**; full-suite comparison showed the same 27 pre-existing environment failures on branch and base, with 31 net new passing tests.

**Lesson:** once failure evidence is structured, diagnosis can become a reusable agent capability rather than repeated human log interpretation. The analyzer must remain advisory and failure-isolated until its recommendations are proven reliable enough to drive actions.

### 90. 2026-09-10: moved-target-base stale adoption now enters integration fallback

The same E2E then exposed a semantic gap after CI persistence and logging were correct: Nexus recognized that historical green CI belonged to an old base, but `assess_failure()` still converted `target_base_moved` into terminal `no_ci_evidence` before the intended integration validation could run.

Issue #144 / PR #147 fixed this by returning a bounded `integration_fallback_required` stale assessment. Nexus reuses the same adoption row/id and existing stale-retry budget, rebuilds the change on current `main` through the existing adoption worktree/cherry-pick semantics, and runs integration fallback tests there. Real test verdicts and exhausted retry budgets remain terminal rather than being superseded.

**Lesson:** rejected evidence can still describe the next valid verification path. A moved base should invalidate reuse of old CI without being misclassified as absence of evidence.

### 91. 2026-09-11: failure analysis became a mailbox-visible durable result

Issue #148 / PR #152 extended `FailureAnalysisService` beyond SQLite/log diagnostics by publishing concise correlated `failure_analysis` messages through the existing Git-backed outbox lifecycle. Analysis rows now carry experiment/task correlation plus persistent publication state; a dedicated publisher uses the same write/commit/push and restart-idempotency semantics as other outbox producers. Publishing is gated to lifecycle-correlated analyses, and publish failure is isolated from both the original failure and the diagnosis itself.

The user-visible message contains the bounded summary, category/confidence, top cause, recommended checks and recovery/escalation flags; unavailable analyses can also produce a concise notification. **Lesson:** useful autonomous diagnosis should reach the operator through the normal work channel, while detailed evidence remains in logs/SQLite. Notification delivery needs the same persistent exactly-once semantics as other durable outputs.

### 92. 2026-09-11: the real moved-base retry exposed verified-source versus integration-candidate state

After PR #147, the real #115/#116 chain reached the moved-base integration fallback but still failed before adoption. An earlier adoption attempt had already committed the verified experiment worktree; the experiment record still described the pre-commit diff, so the normal source-integrity guard interpreted the now-clean committed worktree as post-verification tampering.

Issue #149 / PR #153 fixed this without weakening the ordinary guard. For `integration_fallback_required` only, Nexus proves that the persisted source commit is still HEAD, the worktree is clean, and the commit diff exactly equals the verified experiment diff. It then restores that trusted commit to the original pre-commit diff shape and lets the existing adoption guard validate it normally before constructing the current-base integration candidate. Distinct `verified_source_integrity` and `integration_candidate` events separate the two facts; deliberately mutated source remains terminal/auditable.

**Lesson:** a verified source state and a later integration candidate are different lifecycle artifacts. Recovery must normalize trusted historical representations before applying an invariant, rather than weakening the invariant because the representation changed.

### 93. 2026-09-11: missing failure-analysis state was traced to deployment/runtime freshness rather than a missing finalization call site

Issue #150 / PR #154 investigated why a real finalization reached `needs_review` while the live database had no `failure_analyses` table. Code-path review found that normal, exceptional, and resumed finalization terminal paths already converge on the analyzer hook. The schema is centrally created by `run_store.init_db()`; therefore a missing table on the live runtime most strongly indicates that the active process predates the migration/feature or otherwise has stale deployment state, not that current code omitted the analyzer call.

PR #154 improved error logs with task/experiment correlation and added a regression that reconstructs a finalization across a simulated process restart, exhausts adoption retries from persisted state, reaches `needs_review`, and confirms the analyzer fires. Focused suites passed **10**, **73**, and **318** tests for the relevant groups. The real live #115/#116 reproduction still needs a post-deploy/restart validation before this behavior is considered operationally proven.

**Lesson:** source correctness and deployed-runtime freshness are separate evidence. When a migrated table or capability is absent in production, first distinguish stale process/schema activation from a missing code path, and test recovery using newly constructed services rather than only in-memory mocks.

### 94. 2026-09-11 to 2026-09-12: structured failure-analysis observability merged

Issue #151 / PR #155 added explicit lifecycle events for failure-analysis request, service/schema initialization, model call, persistence, completion/unavailable/skipped outcomes, startup feature status, and resumed-finalization source. Logging intentionally excludes prompts, raw model output, credentials, and raw exception text while retaining safe correlation/diagnostic fields.

PR #155 merged on 2026-09-12. The unified `/logs` feed can now distinguish whether failure analysis was invoked, skipped, unavailable because initialization/model/persistence failed, or resumed through recovery, and startup reports whether the deployed feature/schema is active.

**Lesson:** autonomous diagnosis itself needs observable lifecycle state; otherwise a missing diagnosis creates another opaque failure layer.

### 95. 2026-09-12: autonomy audit found three independent blockers before true self-directed operation

Issue #156 and draft PR #157 traced why Nexus still had not been doing useful work on its own. The investigation found three separate causes rather than one architectural failure.

First, resumed adoption compared textual worktree layout (`git status --short` / `git diff --stat`) with the verified state. Adoption's own commit changed a dirty verified worktree into a clean committed representation, so the guard interpreted its own transition as tampering. The real version experiment had verified green on 2026-09-04 but then reached `needs_review` on **eight consecutive retries**. The proposed fix compares the content/tree being integrated against the verified checkpoint instead of comparing presentation of the worktree.

Second, no production component actually initiated work autonomously: `NexusControllerService` existed but was instantiated only in tests, while mailbox/chat paths remained reactive. The draft adds a persisted one-item initiative tick with priority **unintegrated verified work -> explicitly labelled open issues -> new ideas**, bounded by a daily budget and staleness-aware busy detection. Live data showed **seven succeeded experiments had never been integrated**, plus stale/zombie rows that a naive busy check would treat as permanently active.

Third, authoritative CI had a collection hole: the existing `unittest discover` runner silently skipped plain pytest-style tests. **120 tests in six files were collected zero times** while CI could still report green; after switching to pytest, **119 of the 120 newly visible tests passed unchanged**. Corrected collection then exposed non-hermetic tests whose behavior varies with host memory because prompt-budget escalation can reach a real external provider instead of the injected test double. The draft branch reported **20 failed, 1,461 passed** in the CI-like environment and therefore remains intentionally red/unmerged.

**Lessons:** autonomy needs both a reliable last-mile integration path and an explicit initiative source; lifecycle invariants should compare semantic content rather than incidental worktree representation; and authoritative CI is only authoritative if its collection is complete and hermetic. Before adding more lifecycle semantics, prove one real adoption -> merge -> deploy -> live-validation cycle end to end.

## What Nexus currently learns

Nexus accumulates durable information from:

- code and tests,
- project/idea/decision/history documents,
- SQLite experiment, controller/session, chat-job, local-LLM-job, GitHub-work mapping, operation, tool-loop, verification-run, finalization, CI-verification, adoption-failure-history, failure-analysis, and adaptive service state,
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
- exact-commit GitHub Actions evidence as authoritative verification with bounded local fallback, persisted exact-SHA CI observations reusable across restart, and offline backfill of pre-schema authoritative evidence,
- mandatory GitHub-backed implementation lifecycle using Issues, Issue-linked branches, WIP checkpoints, Draft PRs, verification, merge, and Issue completion,
- persisted post-verification finalization through PR merge, adoption/deployment, service restart recovery, task-specific live validation, and only then final `succeeded`; finalization state is experiment-owned and can be safely rebound to later terminal-owner approvals,
- restart-aware adoption with integration-aware CI precedence, auditable stale-failure supersession, bounded stale retries, moved-base integration fallback on the current target branch, and separate integrity handling for persisted verified-source commits versus integration candidates,
- a reusable local-first failure-analysis service with bounded/redacted evidence, structured persisted diagnoses, best-effort lifecycle integration, diagnostic API access, correlated restart-idempotent mailbox outbox publication, and structured lifecycle observability,
- restart-aware mailbox command recovery using persisted Git baselines, optional declared service postconditions, tri-state evidence (`succeeded`/`failed`/`unknown`), and cached idempotent replay,
- GitHub Actions verification for PRs and `nexus/**` pushes,
- SQLite state and durable project/idea/decision/history documents,
- separate Git-backed mailbox repository with crash-safe publication, rejected-chain recovery, fetch/rebase/push synchronization, deferred trusted command execution, synchronous internal-command compatibility, state-aware mailbox retention, and failure-analysis notifications,
- signed webhook wake-up with timer fallback and bounded recovery diagnostics,
- modularized mailbox, run-store, experiment-service, central-logging, and browser-client code,
- application-wide central structured logging from work submission onward, including stable CI-evidence/stale-adoption decision events, with unauthenticated read-only `/logs`, `/logs/public`, and `/logs/trace` surfaces plus authenticated write/control endpoints,
- one source-aware operational log timeline combining structured logs with bounded Nexus service journals, full-history bounded search, cross-ID execution tracing, log rotation, explicit truncation reporting, literal-secret redaction, newest-first feed ordering, and quieter terminal-skip handling,
- authenticated audited operations for local model, services, logs, processes, Git, and diagnostics,
- authenticated MCP HTTP surface with read-only inspection plus explicitly enabled bootstrap write tools,
- trusted-development mailbox task auto-execution,
- automatic API dependency synchronization,
- invariant protections for auth, secrets, redaction and state validity rather than broad static positive execution allowlists,
- tests and policy boundaries.

Conversation-style Inbox/Outbox rendering (Issue #75), source-context hygiene (Issue #74), strict discovery-grant semantics (Issue #117), explicit verification/integration/deployment status modeling (Issue #119), missing-capability recovery/issue creation (Issue #120), remaining Issue #123 restart paths, controller JSON escape hardening, measurement of the two-stage tool gateway, validation of LLM failure-analysis quality/actionability, and a successful real autonomous-finalization E2E remain open work/research. Issue #156 / draft PR #157 now also tracks the concrete prerequisites for self-directed operation: semantic adoption integrity, a production initiative tick, complete pytest collection, and hermetic provider-routing tests. The Grafana/OpenTelemetry experiment remains rejected. Draft PR #116 is successful CI evidence but remains unmerged.

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
48. **Verification is not completion.** A development task is complete only when required integration/deployment/live behavior is achieved.
49. **Suggestions are not grants.** Tool discovery hints must not implicitly authorize execution.
50. **Capability gaps should feed improvement.** Repeated missing tools can become provisioned dependencies, reusable operations, or deduplicated improvement issues.
51. **The agent must survive its own deployment boundary.** Persist the next unfinished lifecycle step before requesting a restart.
52. **Restart recovery needs postconditions, not process status.** A vanished worker is ambiguous; persist expected state before restart-sensitive work and distinguish proven success/failure from `unknown`.
53. **Restart recovery is layered.** Mailbox-command resilience does not imply adoption/finalization resilience; every durable coordinator must reconstruct its own unfinished state idempotently.
54. **Search completeness must be explicit.** Bounded historical scans should report truncation, and operational traces should bridge correlation-ID changes rather than forcing manual joins.
55. **Nested model-generated serialization is fragile.** Structured-output repair must handle invalid escaping or reduce the number of string-encoding layers.
56. **Persistent state ownership must match lifecycle identity.** Retry/approval IDs may change while an experiment/finalization lifecycle intentionally continues.
57. **Retention belongs with the state machine that knows liveness.** Durable queue files must not be aged out while persisted transitions still depend on them.
58. **Log presentation is diagnostic semantics.** Ordering, source classification, and repetitive-noise policy determine whether existing evidence is actually usable.
59. **Checkpoint verification and integration validation are separate facts.** Exact-SHA green CI is not sufficient after the target branch moves.
60. **No-verdict failures are not implementation verdicts.** New authoritative evidence can supersede an old timeout/infrastructure failure, but retries must remain bounded and auditable.
61. **Authoritative evidence must be durable.** A restart-safe lifecycle cannot depend on an observation that exists only in process memory or live credentials.
62. **Durable schemas need historical backfill.** New persistence is incomplete if already-authoritative facts remain stranded in older lifecycle state.
63. **Decision observability is part of agent reasoning infrastructure.** Log why evidence was accepted/rejected and which fallback was selected, not only the final status.
64. **Diagnosis should be reusable and failure-isolated.** An LLM analyzer may propose next actions, but its own failure must never hide or replace the original error.
65. **Invalidated evidence can still guide recovery.** `target_base_moved` means revalidate the combined tree, not `no evidence`.
66. **Human-facing diagnosis is a durable output.** A useful analysis should be surfaced through the normal mailbox channel with restart-safe exactly-once publication rather than remaining hidden in internal storage.
67. **Invariant checks must account for lifecycle representation changes.** A trusted verified diff may later exist as a clean persisted commit; recovery should prove and normalize that state before reusing the invariant, not weaken source-integrity checks.
68. **Deployment freshness is evidence.** Missing migrated schema/capabilities can indicate stale running code even when current source and tests are correct; recovery tests should reconstruct services from durable state.
69. **Autonomy needs an initiator as well as an executor.** Durable controller/experiment machinery remains reactive unless something periodically selects work and records that decision before dispatch.
70. **CI authority depends on collection and hermeticity.** A green workflow is not trustworthy if tests are silently undiscovered or can escape to real providers because shared host state changes routing.
71. **Compare semantic state, not representation.** Lifecycle guards should validate the content that will be integrated; dirty-vs-clean worktree layout can change legitimately during recovery.

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
- measure whether full-history log search and cross-ID tracing reduce diagnosis time and whether scan budgets/truncation behave well on larger histories,
- measure whether submission-time job/session/mailbox correlation makes failures diagnosable without external tracing,
- measure authoritative exact-SHA CI reuse, historical backfill, and persisted evidence across restarts: wait time, live lookup avoidance, local fallback frequency, false timeout elimination, stale-evidence rejection, and end-to-end latency,
- measure bounded verification repair: recovery rate, iterations, repeated-failure stops, extra model/CI cost, and reduction in human intervention,
- measure stale-adoption recovery including moved-base source normalization/integration fallback: how often no-verdict failures are superseded, fallback success/failure rate, whether the two-retry budget is sufficient, whether trusted committed-source normalization works across restart, and whether terminal implementation/tamper failures remain closed,
- measure local-LLM failure analysis: diagnosis accuracy, calibration/confidence usefulness, actionability of recommended checks, context size/cost, malformed-output rate, outbox usefulness, and reduction in human diagnosis time before allowing automatic diagnostic loops,
- validate failure-analysis activation and structured lifecycle observability on a freshly deployed/restarted live Nexus: can `/logs` alone explain skipped/init/model/persistence failures and recovery-path invocation?,
- evaluate whether self-healing operations and duplicate-loop prevention materially reduce human intervention,
- evaluate MCP as the standard control path and narrow temporary bootstrap capabilities over time,
- measure whether the mandatory GitHub lifecycle improves fire-and-forget development, traceability, recovery, and review,
- add concise GitHub Issue lifecycle progress without duplicating detailed Nexus logs,
- benchmark the two-stage `find_tool(goal) -> call_tool(tool, arguments)` gateway against direct catalog exposure: schema-token reduction, routing accuracy, resolver latency/calls, wrong-tool selection, invalid-goal recovery, and task success,
- enforce and test `find_tool` grant semantics so only `found` can authorize execution and suggestions cannot (Issue #117),
- model verification, integration, deployment, and final completion distinctly across outbox/UI/state (Issue #119),
- investigate missing-capability recovery, deduplicated issue creation, tool provisioning, and promotion of recurring gaps into OperationsRegistry capabilities (Issue #120),
- validate Issue #123 beyond the command-channel fix: restart/resume across remaining mailbox/task paths, explicit expected-state evidence, and exactly-once pending replies,
- harden controller structured-output parsing against JSON-invalid escape sequences and measure repair success without broad lossy normalization,
- finish Issue #156 / PR #157 by making the newly complete pytest suite hermetic, then prove one real stranded verified experiment can traverse adoption -> merge -> deploy -> live validation before enabling broader initiative work,
- evaluate the proposed initiative tick after the integration-only E2E succeeds: stale-work detection, one-item concurrency, daily budget, priority ordering, duplicate avoidance, and useful-work rate,
- test whether reversing current model roles improves reliability/cost: stronger external reasoning for routing/planning/review with local models focused on bounded tool/context work, rather than compensating for a 7B model owning the hardest orchestration decisions,
- investigate checked-out-branch pollution from runtime outbox commits during feature-branch work,
- investigate claim/heartbeat semantics before allowing more than one autonomously initiated item per tick,
- investigate missing-capability detection and structured issue creation before automatic tool generation,
- stronger automatic evaluation and improvement-quality-per-euro metrics,
- automatic discovery of recurring operational problems,
- agent-generated experiments for prompts/tools/context,
- quantization/fine-tuning only where evidence justifies it,
- multi-agent specialization only when benefit exceeds complexity.

## Current next-step candidates

1. **Unblock real autonomy E2E:** finish Issue #156 / draft PR #157 by isolating the corrected pytest suite from real provider calls, get authoritative CI green, then use the semantic adoption invariant to carry one of the stranded verified experiments through adoption -> merge -> deploy -> live validation. Do not add more lifecycle semantics before this succeeds once.
2. **Enable initiative conservatively:** after the E2E succeeds, enable the persisted initiative tick in integration-only mode first; verify stale/zombie rows do not block it, then expand from stranded verified work to labelled Issues and `new` ideas.
3. **Failure-analysis live activation:** reproduce a real finalization `needs_review` on freshly deployed/restarted `main` and confirm the analysis schema, invocation, persistence, outbox publication, and new PR #155 lifecycle events are all visible in `/logs`.
4. **Failure-analysis quality:** run the analyzer against several real lifecycle/tool failures and measure diagnosis accuracy, confidence calibration, useful next checks, malformed-output handling, outbox usefulness, and whether it reduces manual log/DB inspection before enabling any automatic diagnostic loop.
5. **Restart-recovery E2E:** exercise the real Inbox refresh path with PR #124, including declared service postconditions, repeated restart/idempotence, and an inconclusive case that must remain `unknown`; then decide whether broader Issue #123 acceptance criteria need more work.
6. **Controller JSON escaping:** reproduce the `Invalid \\escape` failure with a focused regression and harden structured-output repair or payload encoding without weakening semantic validation.
7. **Tool-grant contract:** implement Issue #117 so `related_tools` can never be interpreted as granted operations, then repeat the version/tool-gateway path.
8. **Missing-capability learning:** implement Issue #120 and test `rg`-missing recovery, fallback/provisioning, deduplicated issue creation, and later promotion to a reusable tool.
9. **Completion-state modeling:** complete Issue #119 so outbox/UI distinguish verified, integrated, deployed, and final succeeded states even outside the finalization coordinator internals.
10. **Real post-OOM rerun:** repeat the feature task that previously caused prompt growth/tool looping and measure whether PRs #95/#97/#99 now finish without repetition or local OOM.
11. **Tool-gateway measurement:** compare the two-stage discovery/invocation protocol with direct catalog exposure and quantify context reduction versus resolver cost, invalid-goal recovery, and wrong selection.
12. **Verification-repair measurement:** run representative repairable and non-repairable failures and measure recovery, repeat detection, latency, model calls, CI runs, and intervention.
13. **Authoritative-CI/recovery measurement:** run several experiments across service restarts and target-branch movement; verify persisted/backfilled exact-SHA evidence avoids duplicate live lookup while never covering another commit or stale integration base.
14. **Adaptive-routing measurement:** run representative simple/medium/large tasks and measure selection, fallback, success, latency, tokens, cost, and intervention.
15. **Persistent job/resource measurement:** quantify local-model/chat queue/runtime/token behavior plus RAM/swap/mmap pressure and recovery across restarts.
16. **Local-model/context benchmark:** compare local candidates under identical actual VM limits and test two-pass context gathering versus larger context.
17. **Project-memory experiment:** retrieve only relevant history and measure idea relevance, repetition, prompt size, and task success.
18. **Mandatory GitHub lifecycle measurement:** verify reuse/resume/conflict/provider-failure behavior across multiple experiments on one Issue/branch/PR.
19. **Source-context hygiene experiment:** run Issue #74 on a small module set and compare context reduction against readability and unchanged behavior.

## 2026-09-12 follow-up: PR #157 merged and the autonomy prerequisites moved from proposal to code

PR #157 merged into `main` after the corrected pytest workflow reached **1,508 passed, 71 skipped in 2m37s**. The adoption guard now compares the semantic Git tree that would be integrated with the verified checkpoint rather than comparing dirty/clean worktree presentation. The repository also now contains the persisted initiative tick and its 15-minute timer, but the timer intentionally ships **disabled** until a complete manual adoption -> merge -> deploy -> live-validation cycle succeeds once. The CI migration ultimately exposed **147 previously uncollected tests across eight files**, contradictory stale expectations, and an operation-chain outbox test whose default `/agent` dependency revealed that a swallowed publication exception can let a chain report completed even when its outbox result vanished; that production-risk follow-up is tracked in Issue #158.

This supersedes the earlier section 95 status that described PR #157 as a red draft. The remaining acceptance target is no longer “get #157 green”; it is to deploy current `main` and prove the first complete lifecycle E2E before enabling autonomous initiative.

**Lesson:** test discovery is part of the specification. An unexecuted test is not neutral; it can preserve false or contradictory expectations while CI still reports green.

## 2026-09-12 follow-up: Issue #158 captured structural simplification targets rather than new lifecycle rules

The autonomy diagnosis also produced Issue #158 with architectural work that deliberately did **not** block PR #157. A captured controller turn showed a 7,415-character prompt exceeding a host-memory-derived safe local budget of 4,784 characters and escalating to `codex_readonly`, strengthening the hypothesis that the current role split may assign the hardest orchestration work to the weakest model while frequently bypassing it in real sessions. The agreed direction is to measure local-versus-escalated controller usage before changing roles.

Issue #158 also records four broader debts: the long serial lifecycle should be simplified only after one green E2E proves which stages earn their existence; plan safety still relies partly on brittle prose substring checks and should move toward behavioral invariants; long-running work has no explicit heartbeat, so liveness is inferred with a coarse staleness window and zombie rows remain possible; and tests must be hermetic so no suite execution can reach a real paid provider. More than one autonomously initiated item per tick should wait for explicit claim/heartbeat semantics.

**Lesson:** after repeated recovery fixes, the next architectural improvement may be subtraction. Measure which stages, model roles, and policy layers contribute value before adding more lifecycle semantics.