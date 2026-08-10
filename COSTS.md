# COSTS.md

## Cost policy

- Operate within limited Azure Visual Studio Enterprise monthly credit.
- Favor cheapest option that still meets quality/safety requirements.
- Do not provision paid cloud DB until justified by measured need.
- Prefer small experiments with explicit token/runtime/cost reporting.

## Current cost-related facts

- Azure OpenAI deployment in use: `gpt-4o` (`2024-05-13`, GlobalStandard).
- Compute baseline: `Standard_B1s` VM (1 vCPU, 1 GB RAM).
- V0 currently logs token usage and runtime per run.

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

## Tracking format (template)

| Date (UTC) | Experiment | Model/Provider | Tokens (in/out/total) | Runtime (s) | Est. Cost | Notes |
|---|---|---|---:|---:|---:|---|
| TBD | TBD | TBD | TBD | TBD | TBD | Initialize once benchmark harness exists |

