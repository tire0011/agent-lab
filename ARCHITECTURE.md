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
  - `modules/run_store/` — SQLite-backed persistence package: schema and additive migrations, plus run/tool-call/price/activity/settings helpers
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
- `activity_log` table stores a persistent, append-only log of entity lifecycle
  events (currently experiment creation and status transitions), written by
  `ExperimentService` inside the same locked connection/transaction as the
  state change it records, so activity rows and experiment state never
  diverge. Exposed read-only via `GET /activity`.
- JSON artifacts under `state/runs/` are kept for lightweight audit/debug parity.

### 3) API surface

- File: `api.py`, models in `models.py`, read-only data adapters in `data_sources.py`, rule-based chat in `chat.py`
- Framework: FastAPI
- Endpoints:
  - `GET /health` (public)
  - `GET /secure-health` (requires `x-api-key` matching `AGENT_API_KEY`)
  - `GET /status`, `GET /problems`, `GET /ideas`, `POST /chat` (read-only; consistent data via `data_sources.py`)
  - `POST /mcp/` (requires `x-api-key`) — mounted Streamable HTTP MCP adapter
    that reuses the existing API/service logic for read-only Nexus inspection
    tools; no write tools, shell execution, or new business logic
  - `POST /ideas` (requires `x-api-key`) — appends a sanitized, state-`new`
    idea to a fixed `### Inbox` subsection at the end of `IDEAS.md`'s
    `## Ideas` section; `data_sources.add_idea()` guards the read-modify-write
    with an exclusive `fcntl.flock` so concurrent requests cannot corrupt
    the file or clobber each other's entries.
  - `POST /codex/tasks`, `GET /codex/tasks`, `GET /codex/tasks/{id}`,
    `POST /codex/tasks/{id}/run` (all require `x-api-key`) — thin routes over
    the existing `modules.codex_task_service.CodexTaskService`; no worker
    logic duplicated in `api.py`. `working_dir` (`/agent`) and `sandbox`
    (`read-only`) are fixed server-side; `timeout_seconds` is capped at
    120s. Synchronous only — no queue, no parallel execution, no chat/Web UI
    wiring yet. `mark_interrupted_tasks_failed()` runs once via FastAPI's
    lifespan startup hook to mark orphaned `running` tasks as `failed`
    without restarting them.
  - `GET /experiments`, `GET /experiments/{id}` (require `x-api-key`) — thin,
    read-only routes over `modules.experiment_service.ExperimentService`;
    no experiment orchestration is triggered from these routes.
  - `GET /activity` (requires `x-api-key`) — persistent experiment activity
    log (see `activity_log` table above), optionally filtered by
    `experiment_id`, newest first.
  - `ExperimentService` is now a process-wide singleton
    (`api.get_experiment_service()`) shared between the mailbox task
    approval bridge and the `/experiments`/`/activity` endpoints, so all
    routes observe the same in-process state.
- Deployment context: FastAPI behind nginx on VM with HTTPS

### 4) Client

- Files: `Client/client.py` (CLI), `Client/Invoke-Client.ps1` (PowerShell wrapper)
- Subcommands: `health`, `status`, `problems`, `ideas`, `chat`

### 5) Infrastructure

- Single low-cost VM (`Standard_B1s`) in `germanywestcentral`
- Azure OpenAI deployment: `gpt-4o` (GlobalStandard)

### 6) Mailbox

- Files: `modules/mailbox/core.py` (parsing, validation, persistence, locks, and Git helpers), `modules/mailbox/processing.py` (inbox workflow), `modules/mailbox/__init__.py` (stable `modules.mailbox` public API), and `mailbox_processor.py` (CLI entry point)
- Inbox: `communication/inbox/` — Markdown files with YAML front matter
- Outbox: `communication/outbox/` — response files written per processed message
- Message types: `idea` (appended to IDEAS.md), `task` (body stored, `pending_approval` record created in `mailbox_task_approvals`), `question` (acknowledged), `context` (stored in `knowledge` table)
- Safety: max 64 KB, no binary/symlinks/path-traversal, secret pattern detection, max 3 retries
- Git integration: commits outbox + IDEAS.md (idea type only), pushes to tracked branch
- Lock: `state/mailbox.lock` via `fcntl.flock` prevents concurrent runs on the Ubuntu deployment; the implementation includes a Windows fallback for portable local checks
- Deployment: systemd oneshot service + 1-minute timer (see `deploy/nexus-mailbox.{service,timer}`)
- Command channel restart recovery (`modules/mailbox/development.py` +
  `modules/mailbox/command_recovery.py`, Issue #123): the trusted `command`
  message type runs the shell command as a detached background job so
  Nexus-restarting commands (e.g. `refresh.sh`) survive across mailbox
  polls. Before spawning, a local `git rev-parse HEAD` of the working
  directory is persisted next to the job state, together with an optional
  `recovery_services` front-matter field the message can use to declare
  what "complete" means for it. If a later poll finds the runner's pid gone
  with no exit code recorded, it evaluates that baseline against the
  current repo state instead of reporting a bare `Exit code: 255`: a HEAD
  that moved but never converges to a clean commit matching its upstream is
  always a verified failure; git convergence by itself is reported as
  unknown (proving only that a git update step ran, not that the rest of a
  multi-step command such as a post-pull restart ever completed) unless the
  message declared `recovery_services: none` (git convergence is its whole
  expected state) or `recovery_services: svc-a, svc-b` (converged git *and*
  every listed systemd unit active is success; converged git with any
  listed unit inactive is a verified failure, not a false success). The
  verdict is persisted to the job's exit-code/stderr files the first time it
  is computed, so a further restart replays the same cached reply instead
  of re-deriving or duplicating it.

### 7) Mailbox task approval bridge

- Module: `modules/mailbox/task_service.py`
- Table: `mailbox_task_approvals` in SQLite
- State machine: `pending_approval → approved → submitted → running → succeeded / failed / needs_review`
- Human operators approve/reject via authenticated API (`POST /mailbox/tasks/{id}/approve`) or Web UI
- On approval, `ExperimentService.create_experiment` + `run_experiment` run in a daemon thread
- `MailboxTaskWorker` daemon polls every 10 s (`submit_approved` + `poll_running`)
- Terminal states write outbox files; `failed`/`needs_review` also write a problem JSON to `state/runs/`

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
