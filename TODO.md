# TODO.md

## Priority roadmap

1. [ ] Finish and test MCP-based V1.
2. [x] Establish persistent structured logging/database (SQLite first).
3. [ ] Benchmark V0 vs V1 with reproducible tasks and metrics.
4. [ ] Add model/provider abstraction.
5. [ ] Evaluate cheaper Azure models and additional providers.
6. [ ] Add evaluation gate before self-modification.
7. [ ] Begin controlled self-improvement experiments.

## Current direction — 2026-08-11

### Completed infrastructure and communication baseline

- [x] Resolve the `agent-api`/Uvicorn startup issue and confirm the VM baseline is healthy.
- [x] Increase the main VM to a low-cost 2 vCPU / 4 GB class (`Standard_B2s`).
- [x] Install GitHub Copilot CLI and Codex CLI on the VM.
- [x] Install system `bubblewrap` for the Codex sandbox.
- [x] Expose the API through Nginx and HTTPS.
- [x] Add read-only API endpoints for health, status, problems, and todos.
- [x] Add the initial chat endpoint.
- [x] Build and publish a small mobile-friendly Web UI for status, problems, todos, and chat.
- [x] Add a separate CLI client for the API.
- [ ] Update README and AGENTS.md so the documented VM size, API endpoints, clients, and Web UI match reality.

### Immediate next milestone — Codex worker

- [x] Inspect the current agent/module boundaries and define the smallest `CodexWorker` interface.
- [x] Add a replaceable coding-tool adapter that invokes `codex exec` through an argument list, never through a shell command string.
- [x] Start with read-only jobs in an explicitly configured repository working directory.
- [x] Capture exit code, stdout, stderr, duration, Codex session/run identifier when available, and timeout state.
- [x] Add configurable timeout and output-size limits.
- [x] Reject missing, relative, or non-allowlisted working directories.
- [x] Do not accept arbitrary shell commands; the worker accepts a natural-language coding task only.
- [x] Persist a minimal audit record using the existing run/logging infrastructure.
- [x] Add unit tests with a fake process runner so tests do not consume Codex quota.
- [x] Run one real read-only smoke test: inspect `README.md` and summarize the project without modifying files.
- [x] Verify after the smoke test that the Git working tree is unchanged.
- [x] Measure runtime and record whether token/usage information is available.
- [x] Keep the worker internal initially; do not expose execution through the public chat/API yet. (superseded below — now exposed via an authenticated task API)
- [x] After the internal worker is stable, design an authenticated task endpoint with approval, status, cancellation, and result retrieval. (approval/cancellation intentionally deferred; see below)

### Codex task API (implemented, small first step)

- [x] Add authenticated `POST/GET /codex/tasks`, `GET /codex/tasks/{id}`, `POST /codex/tasks/{id}/run` in `api.py`, reusing `CodexTaskService`/`CodexWorker` with no duplicated worker logic.
- [x] Enforce `working_dir=/agent`, `sandbox=read-only`, and a fixed max `timeout_seconds` (120s) server-side; client cannot override.
- [x] Run `mark_interrupted_tasks_failed()` once at API startup via FastAPI lifespan.
- [x] Add `codex create|list|get|run` subcommands to `Client/client.py`.
- [x] Fix a thread-safety bug surfaced by wiring `CodexTaskService`'s SQLite connection into FastAPI's sync threadpool (`check_same_thread=False` + a lock).
- [ ] Add approval workflow (currently: any `queued` task can be run immediately by an authenticated caller).
- [ ] Add task cancellation (currently: a running task cannot be stopped once started).
- [ ] Move execution off the request thread (currently strictly synchronous; a long Codex call blocks the HTTP response) once background execution is deliberately in scope.

### Baseline measurement

- [ ] Run one reproducible end-to-end baseline task and persist quality, tokens, runtime, estimated cost, and resource usage.
- [ ] Keep the VM as the primary persistent home of the agent, repository, memory, orchestration, tests, and CLI tooling.

### Web research worker

- [ ] Evaluate a self-hosted SearXNG instance as the primary low-cost search backend.
- [ ] Build a small `WebResearchWorker` that searches through SearXNG, fetches a bounded number of result pages, and returns answers with source URLs.
- [ ] Add strict request, page-count, download-size, timeout, and concurrency limits.
- [ ] Respect robots.txt, provider terms, rate limits, and website access restrictions; do not scrape Google AI Overviews as a production dependency.
- [ ] Log search count, fetched pages, tokens, runtime, failures, and estimated cost per research task.
- [ ] Compare SearXNG against Gemini Grounding with Google Search on a small fixed question set for answer quality, source quality, freshness, runtime, and total cost.
- [ ] When testing Gemini 3.x, account separately for the monthly 5,000 included Google Search requests and the model input/output token charges; one prompt may trigger multiple search requests.

### Memory and self model

- [ ] Add a persistent self model (`SELF.json` or SQLite-backed equivalent) containing version, capabilities, known weaknesses, resources, goals, and current constraints.
- [ ] Separate memory into current task context, project memory, experience/experiment memory, and self model.
- [ ] Load only relevant memory into LLM context instead of continuously growing one large prompt.
- [ ] Reuse the existing SQLite store for structured memories and experiment history before adding embeddings/RAG.

### Communication interface

- [x] Use the Web API as the primary, provider-independent communication interface for humans, UIs, tools, and later other agents.
- [x] Provide structured status, problem, todo, and chat endpoints without coupling the agent core to a specific client.
- [x] Build a small mobile-friendly Web UI for chat, agent status, problems, and todos.
- [x] Add authentication before exposing task execution, detailed operational data, or write operations. (`x-api-key` now enforced on the new `/codex/tasks*` endpoints, same as `/secure-health`)
- [ ] Add experiment, cost, and approval views after the corresponding backend contracts exist.
- [ ] Keep communication adapters separate so messenger integrations such as WhatsApp can be added later without changing the agent core.
- [ ] Explore using the same communication contract later for agent-to-agent communication.

### First self-improvement loop

- [ ] Agent reviews previous runs and identifies one measurable weakness.
- [ ] Agent proposes one bounded improvement before being allowed to modify itself automatically.
- [ ] Evaluate candidate against a fixed baseline.
- [ ] Accept only improvements that pass quality/cost/runtime gates; otherwise rollback.
- [ ] Persist experiment hypothesis, change, metrics, result, decision, and cost.

### Test isolation / second VM

- [ ] Investigate a second Azure VM as an ephemeral test worker.
- [ ] Compare cost of local tests on the main VM vs starting a disposable test VM only for risky changes.
- [ ] Preferred flow: create/start test VM -> fresh checkout -> tests/benchmarks -> return result -> deallocate/delete.
- [ ] Main agent should eventually choose test isolation level based on risk and expected cost.
- [ ] Do not keep a second VM running 24/7 unless measurements justify it.

### Coding CLI as an agent tool

- [x] Treat coding CLIs as replaceable tools rather than building all coding-agent capabilities ourselves.
- [ ] Let the orchestrator request a code change from a CLI, capture diff/test results, then independently evaluate the result.
- [ ] Later compare multiple coding CLIs/models by task quality, cost, tokens, and runtime and route tasks accordingly.

### Autonomy stages

- [ ] Level 0: human assigns every task.
- [ ] Level 1: agent proposes its own next experiment.
- [ ] Level 2: agent executes cheap, reversible, bounded experiments automatically.
- [ ] Level 3: continuous improvement loop with automatic evaluation and rollback.
- [ ] Level 4: architecture/model/resource changes within explicit budgets and safety limits.

## Task details

### 1) V1 MCP completion

- [ ] Define V1 scope (capabilities and limits).
- [ ] Add tests for MCP path success/failure cases.
- [ ] Validate behavior on local machine and VM.

### 2) Persistent structured storage

- [x] Define SQLite schema for runs, tool calls, model prices, and simple knowledge entries.
- [x] Add bootstrap logic for `/agent/state/agent.db`.
- [x] Keep JSON run artifacts initially for audit/debug parity.

### 3) Benchmarking

- [ ] Create fixed benchmark task set.
- [ ] Capture quality rubric and scorer approach.
- [ ] Track tokens, runtime, estimated cost, and resource usage.
- [ ] Produce side-by-side V0/V1 benchmark report.

### 4) Provider abstraction

- [ ] Define provider/model interface.
- [ ] Implement Azure adapter first.
- [ ] Add at least one non-Azure adapter.

### 5) Low-cost model evaluation

- [ ] Test cheap Azure model options.
- [ ] Test at least one additional provider.
- [ ] Compare quality/cost/runtime tradeoff.

### 6) Evaluation gate

- [ ] Define pass/fail criteria before self-modification.
- [ ] Block changes that fail evaluation.
- [ ] Log evaluation outcomes in structured store.

### 7) Controlled self-improvement

- [ ] Start with low-risk, bounded modifications.
- [ ] Require rollback strategy for each experiment.
- [ ] Record experiment design, result, and cost.

## Later milestone

- [ ] **V2.1: Azure actual-cost reconciliation**

## Unknowns

- Which evaluation strategy is robust enough for automated gating.
- Which low-cost model mix offers acceptable quality.
- How much autonomy is safe on current infrastructure limits.
- Which VM size gives the best cost/performance for coding CLI workloads.
- Whether an ephemeral second VM is cheaper and safer than testing on the main VM.
- Which memory retrieval strategy remains simple enough while still scaling with accumulated experience.
- Which stable Codex CLI output or event format exposes usage and session metadata reliably.
