# PROJECT.md

## Scope

This repository explores distributed self-improving LLM agents with strict cost control.

Current implementation is intentionally small and measurable.

## Goals

1. Build a reliable baseline agent (V0 done).
2. Add MCP-enabled capabilities (V1 in progress).
3. Measure quality, tokens, cost, runtime, and resource usage.
4. Introduce safe evaluation gates before self-modification.
5. Run controlled self-improvement experiments only after instrumentation and benchmarking are in place.

## Evolving purpose

Self-improvement is a means, not Nexus's only purpose. As its capabilities
mature, Nexus may investigate useful research questions, build small tools
or services, and prepare contributions to public open-source projects. It
should choose and explain possible directions using expected usefulness,
novelty, learning value, cost, and risk, and should publish measurable
evidence and results where appropriate.

Nexus may also propose new goals, interests, or changes to this purpose.
Such proposals must be recorded with their motivation, expected benefits,
costs, risks, and a way to evaluate them. The purpose is intentionally
allowed to evolve through experience, but proposed changes are initially
adopted only after joint human discussion and approval.

External publication, pull requests to third-party projects, spending, and
commitments made on behalf of people or the project initially require
explicit human approval. These approval boundaries may themselves evolve
later through measured, reversible autonomy experiments.

## Non-goals (for now)

- Large-scale architecture before benchmark evidence.
- Early paid managed databases.
- Vendor lock without adapter abstraction.

## Current facts

- V0 in `agent.py` runs locally and on VM via environment variables.
- V0 writes JSON logs per run into `state/runs/`.
- V1 is being developed around MCP filesystem access.
- API service exists in `api.py` and has authenticated endpoints except `/health`.
- FastAPI + nginx + TLS are configured on VM.

## Development principles

- Always check existing repo functionality first.
- Prefer maintained OSS libraries and reusable GitHub projects before custom builds.
- Prefer transparent components that can be inspected/forked/modified.
- Keep experiments small and reversible.

## Unknowns

- V1 completeness and robustness under repeated runs.
- Best provider/model mix at acceptable quality/cost.
- How to reliably expose agent capabilities to external systems without brittle coupling.

## Exit criteria for current phase

Current phase is complete when all are true:

1. V1 passes defined tests.
2. Structured persistence exists (SQLite or equivalent local DB).
3. V0 vs V1 benchmarks are reproducible.
4. Evaluation gate exists and blocks unsafe/unverified self-modifying actions.

