# ARCHITECTURE.md

## Current architecture (implemented)

### 1) Agent runtime

- File: `agent.py` (thin CLI orchestration only)
- Supporting modules under `modules/`:
  - `modules/agent_clients.py` — Azure OpenAI client + MCP filesystem server construction
  - `modules/agent_result.py` — usage/tool-trace extraction from agent run results
  - `modules/serialization.py` — JSON-safe serialization helpers
  - `modules/env.py` — required environment variable lookup
  - `modules/run_log.py` — writes JSON run artifacts to `state/runs/`
  - `modules/cli_output.py` — CLI print helpers for cost/run summaries
  - `modules/cost_monitor.py` — `AzureCostMonitor`/`AzureCostSnapshot` (Azure Cost Management)
  - `modules/run_store.py` — SQLite-backed run/tool-call/price persistence
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

- File: `api.py`, models in `models.py`, read-only data adapters in `data_sources.py`, rule-based chat in `chat.py`
- Framework: FastAPI
- Endpoints:
  - `GET /health` (public)
  - `GET /secure-health` (requires `x-api-key` matching `AGENT_API_KEY`)
  - `GET /status`, `GET /problems`, `GET /ideas`, `POST /chat` (read-only; consistent data via `data_sources.py`)
  - `POST /codex/tasks`, `GET /codex/tasks`, `GET /codex/tasks/{id}`,
    `POST /codex/tasks/{id}/run` (all require `x-api-key`) — thin routes over
    the existing `modules.codex_task_service.CodexTaskService`; no worker
    logic duplicated in `api.py`. `working_dir` (`/agent`) and `sandbox`
    (`read-only`) are fixed server-side; `timeout_seconds` is capped at
    120s. Synchronous only — no queue, no parallel execution, no chat/Web UI
    wiring yet. `mark_interrupted_tasks_failed()` runs once via FastAPI's
    lifespan startup hook to mark orphaned `running` tasks as `failed`
    without restarting them.
- Deployment context: FastAPI behind nginx on VM with HTTPS

### 4) Client

- Files: `Client/client.py` (CLI), `Client/Invoke-Client.ps1` (PowerShell wrapper)
- Subcommands: `health`, `status`, `problems`, `ideas`, `chat`

### 5) Infrastructure

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
