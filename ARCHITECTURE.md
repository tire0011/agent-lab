# ARCHITECTURE.md

## Current architecture (implemented)

### 1) Agent runtime

- File: `agent.py`
- Execution model: CLI task runner
- Model backend: Azure OpenAI via OpenAI SDK client configured with Azure base URL
- Agent framework: OpenAI Agents SDK (`Agent`, `Runner`)
- Tooling: MCP filesystem server over stdio, restricted to read-only functions (`list_directory`, `read_file`)

### 2) Logging and persistence

- SQLite database at `/agent/state/agent.db` is the primary persistent run store.
- `runs` table stores execution metadata: timestamp, provider/model, input description, tokens, runtime, estimated cost, actual cost (nullable), cost status, success, and error.
- `model_prices` table stores provider/model token prices with `valid_from` history and source attribution; estimated cost uses this table only.
- `tool_calls` table stores per-run MCP tool call records.
- `knowledge` table stores simple structured observations from runs for later analysis.
- JSON artifacts under `state/runs/` are kept for lightweight audit/debug parity.

### 3) API surface

- File: `api.py`
- Framework: FastAPI
- Endpoints:
  - `GET /health` (public)
  - `GET /secure-health` (requires `x-api-key` matching `AGENT_API_KEY`)
- Deployment context: FastAPI behind nginx on VM with HTTPS

### 4) Infrastructure

- Single low-cost VM (`Standard_B1s`) in `germanywestcentral`
- Azure OpenAI deployment: `gpt-4o` (GlobalStandard)

## Near-term target architecture (planned, not complete)

1. **V1 MCP agent path**
   - Expand practical MCP usage while keeping capability boundaries explicit.
2. **Persistent storage**
   - Keep SQLite-backed structured run store and evolve reporting/migrations as needed.
3. **Benchmark harness**
   - Reproducible V0 vs V1 comparison pipeline.
4. **Provider abstraction layer**
   - Decouple agent runtime from single provider/model.
5. **Evaluation gate**
   - Automated checks before any self-modifying workflow.

## Design constraints

- Budget-first decisions.
- Favor existing maintained OSS over custom orchestration.
- Keep components inspectable and modifiable.
- Security baseline: no secrets in repo.

## Known gaps

- No persistent relational metrics store yet.
- No formal benchmark suite checked into repo yet.
- No finalized policy/eval framework for autonomous self-modification yet.
- External ChatGPT-to-endpoint integration is not yet dependable.
