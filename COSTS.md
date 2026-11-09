# COSTS.md

## Cost policy

- Operate within limited Azure Visual Studio Enterprise monthly credit.
- Favor cheapest option that still meets quality/safety requirements.
- Do not provision paid cloud DB until justified by measured need.
- Prefer small experiments with explicit token/runtime/cost reporting.

## Current cost-related facts

- Azure OpenAI deployment in use: `gpt-4o` (`2024-05-13`, GlobalStandard).
- Compute baseline: `Standard_D2ds_v5` VM.
- V2 stores run metrics in SQLite at `/agent/state/agent.db`.
- Estimated LLM cost is computed from token usage and `model_prices` rows valid at run time.
- If no matching model price exists, run execution continues and estimated cost is stored as unknown/null.
- Actual Azure cost reconciliation is deferred to **V2.1**.

## Unknowns

- Remaining monthly credit at this moment.
- Effective per-task cost distribution across planned workloads.
- Cost-performance of cheaper Azure model options vs quality targets.

## Measurement requirements for every experiment

Capture at minimum:

1. Prompt tokens
2. Completion tokens
3. Total tokens
4. Runtime
5. Estimated request cost
6. Resource usage (CPU/RAM where feasible)

## Estimated cost formula

For each run:

- `input_tokens / 1_000_000 * input_price_per_million_tokens`
- `output_tokens / 1_000_000 * output_price_per_million_tokens`

Total estimated cost is the sum of both values.

## Tracking format (template)

| Date (UTC) | Experiment | Model/Provider | Tokens (in/out/total) | Runtime (s) | Est. Cost | Notes |
|---|---|---|---:|---:|---:|---|
| TBD | TBD | TBD | TBD | TBD | TBD | Per-task numbers live in `benchmark_runs`; see `benchmarks/README.md` |
