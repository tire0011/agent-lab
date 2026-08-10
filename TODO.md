# TODO.md

## Priority roadmap

1. [ ] Finish and test MCP-based V1.
2. [x] Establish persistent structured logging/database (SQLite first).
3. [ ] Benchmark V0 vs V1 with reproducible tasks and metrics.
4. [ ] Add model/provider abstraction.
5. [ ] Evaluate cheaper Azure models and additional providers.
6. [ ] Add evaluation gate before self-modification.
7. [ ] Begin controlled self-improvement experiments.

## Current direction — 2026-08-10

### Immediate next steps

- [ ] Resolve current `agent-api`/Uvicorn startup issue and confirm the VM baseline is healthy.
- [ ] Run one reproducible end-to-end baseline task and persist quality, tokens, runtime, estimated cost, and resource usage.
- [ ] Increase the main VM RAM enough to run coding/LLM CLI tools directly on Linux; target a low-cost 2 vCPU / 4 GB class first.
- [ ] Install and evaluate one coding/LLM CLI directly on the VM so Windows is not required for the normal development loop.
- [ ] Keep the VM as the primary persistent home of the agent, repository, memory, orchestration, tests, and CLI tooling.

### Memory and self model

- [ ] Add a persistent self model (`SELF.json` or SQLite-backed equivalent) containing version, capabilities, known weaknesses, resources, goals, and current constraints.
- [ ] Separate memory into current task context, project memory, experience/experiment memory, and self model.
- [ ] Load only relevant memory into LLM context instead of continuously growing one large prompt.
- [ ] Reuse the existing SQLite store for structured memories and experiment history before adding embeddings/RAG.

### Communication interface

- [ ] Use the Web API as the primary, provider-independent communication interface for humans, UIs, tools, and later other agents.
- [ ] Define a simple message endpoint (for example `POST /messages`) plus status/experiment endpoints without coupling the agent core to a specific client.
- [ ] Build a small mobile-friendly Web UI for chat, agent status, current goal, experiments, costs, and approvals.
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

- [ ] Treat coding CLIs as replaceable tools rather than building all coding-agent capabilities ourselves.
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

## Next milestone

- [ ] **V2.1: Azure actual-cost reconciliation**

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

## Unknowns

- Which evaluation strategy is robust enough for automated gating.
- Which low-cost model mix offers acceptable quality.
- How much autonomy is safe on current infrastructure limits.
- Which VM size gives the best cost/performance for coding CLI workloads.
- Whether an ephemeral second VM is cheaper and safer than testing on the main VM.
- Which memory retrieval strategy remains simple enough while still scaling with accumulated experience.
