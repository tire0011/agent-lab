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
## Coding-agent run costs

- Codex, Claude API and Azure fallbacks use the recorded token usage and the time-valid model price. Codex uses cached-input rates. Claude prices each model's input, cache reads, cache writes, and output separately. Missing model, rate, or billing evidence leaves the cost unknown. [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing).
- Audited Codex calls keep a native session rollout under the configured Codex home. Nexus reads its model and, when needed, cumulative token counters for the exact thread. These rollouts can contain prompts and output.
- Claude Code subscription billing is detected from effective authentication. Set `AGENT_CLAUDE_SUBSCRIPTION=1` or `0` only when the CLI's billing mode is known and needs an explicit override.
- Vibe records finite, nonnegative USD amounts from `export.json`. Set `AGENT_VIBE_SUBSCRIPTION=1` for a CLI configured to bill a Vibe subscription. Nexus requires export evidence of model calls before applying the subscription mark.
- Azure Cost Management snapshots remain useful for monitoring the whole subscription. The run store uses provider usage to estimate Azure invocation costs.
- `WEEKLY_COST_BY_PROVIDER_SQL` groups by UTC Monday, provider, and currency. A missing currency stays in its own bucket. A subscription counts toward coverage without contributing a money value.

Run the read-only report against an existing, migrated database:

```sh
python -m scripts.report_run_costs path/to/agent.db
```

The report gives the rolling seven-day share across all runs, weekly provider totals, and unresolved run IDs. Historical Azure `gpt-4o` rows with usage can be estimated from the price table. Historical Codex and Claude totals alone do not establish cache charges, the effective model, or the billing mode. Use `--recover-from evidence.json` to apply verified run-specific evidence before reporting. Each entry supplies `id`, `provider`, the exact stored `timestamp`, and a `source` reference. Include the resolved `model` and normalized `usage` or per-model `model_usage` for token estimates. Claude and Vibe also require `billing_mode` (`api` or `subscription`). Vibe API entries use `actual_usd` from the export. Apply only evidence already tied to the specific invocation. The blocked review reported 5 costed runs out of 64. Its hypothetical 53 out of 64 was a ceiling from token availability, not a verified recovery rate. The production snapshot and supporting logs were absent from the issue worktree.

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
