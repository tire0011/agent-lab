# AGENTS.md

Purpose: enable any future AI agent to continue this project without prior chat history.

## Identity

The persistent agent system is named **Nexus**. Read `NEXUS.md` before acting.
Codex, Copilot, and other LLMs are workers acting on Nexus's behalf, not human
users and not independent persistent identities.

## Mission

Build and evaluate a distributed self-improving LLM agent system under strict budget limits.

Primary objective: learn how far the system can improve itself safely and measurably.

## Working Principles

1. Prefer small, measurable experiments over big architecture rewrites.
2. Before building new functionality, check:
   - existing repo functionality,
   - maintained open-source libraries,
   - reusable GitHub projects.
3. Prefer solutions that can be inspected, forked, and modified.
4. Never commit secrets (API keys, credentials, SSH private keys, tokens).
5. Treat unknowns as first-class: document them explicitly.
6. Do not mark work complete without evidence (logs, tests, benchmarks, artifacts).

## Current State (as of 2026-08-10)

- `agent.py` V0 runs locally and on Azure VM with env vars.
- V0 logs model calls, token usage, runtime, and MCP tool calls to `state/runs/*.json`.
- V1 is in development with MCP filesystem access.
- FastAPI app exists in `api.py` with:
  - public `GET /health`
  - authenticated `GET /secure-health` via `AGENT_API_KEY`
- FastAPI + nginx are running on VM with HTTPS and Let's Encrypt.
- Azure OpenAI API connectivity has been tested successfully.

## Hard Constraints

- Azure subscription credit is limited (Visual Studio Enterprise benefit).
- Do not provision paid cloud DB until truly needed.
- SQLite is the preferred first persistent database.

## Known Infrastructure

- Resource group: `agent-lab-rg`
- Region: `germanywestcentral`
- VM: `agent-lab-vm` (`Standard_B1s`, 1 vCPU / 1 GB RAM, Ubuntu 22.04)
- VM identity: system-assigned managed identity (no broad permissions currently)
- Repo: `tire0011/agent-lab`
- Azure OpenAI resource: `openAI-tire0011`
- Deployment: `gpt-4o` (`2024-05-13`, GlobalStandard)
- Public hostname: `agent-lab-tire0011.germanywestcentral.cloudapp.azure.com`

## Execution Order (do not skip)

1. Finish and test MCP-based V1.
2. Establish persistent structured logging/database.
3. Benchmark V0 vs V1.
4. Add model/provider abstraction.
5. Test cheaper Azure models and other providers.
6. Add evaluation gate before self-modification.
7. Start controlled self-improvement experiments.

## What to Read First

1. `README.md` (quick start + repo overview)
2. `PROJECT.md` (scope, goals, non-goals)
3. `ARCHITECTURE.md` (current architecture and planned evolution)
4. `IDEAS.md` (open idea repository, not a fixed task list)
5. `DECISIONS.md` (decision log)
6. `COSTS.md` (budget guardrails)
7. `OPERATIONS.md` (runbook)

## Mailbox polling

Nexus polls `communication/inbox/` every minute via a systemd timer (`deploy/nexus-mailbox.timer`). Drop a Markdown file with YAML front matter (`type`, `created_by`) to communicate with Nexus. Responses appear in `communication/outbox/`. See `README.md` "Mailbox" section and `OPERATIONS.md` "Mailbox systemd service" for full details.

## Unknowns to Resolve Early

- Exact V1 MCP feature completeness and test coverage.
- Whether OpenAI Agents SDK can replace custom orchestration cleanly.
- Reliable way for external ChatGPT-to-endpoint integration (currently not dependable).
- Practical cost/quality tradeoff across cheaper models/providers.
