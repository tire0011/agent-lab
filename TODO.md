# TODO.md

## Priority roadmap

1. [ ] Finish and test MCP-based V1.
2. [x] Establish persistent structured logging/database (SQLite first).
3. [ ] Benchmark V0 vs V1 with reproducible tasks and metrics.
4. [ ] Add model/provider abstraction.
5. [ ] Evaluate cheaper Azure models and additional providers.
6. [ ] Add evaluation gate before self-modification.
7. [ ] Begin controlled self-improvement experiments.

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

