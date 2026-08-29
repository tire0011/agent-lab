# agent-lab

Experimental project for distributed self-improving LLM agents under strict budget constraints.

## Mailbox

Nexus uses the separate `tire0011/agent-lab-mailbox` repository for Git-backed mailbox communication. GitHub push events affecting `inbox/` trigger immediate processing through `POST /mailbox/webhook/github`; the systemd timer remains as a one-minute fallback.

**Inbox** (`agent-lab-mailbox/inbox/`): Drop a Markdown file with YAML front matter here to send a message to Nexus.

Supported message types: `idea`, `task`, `question`, `context`.

Example: `agent-lab-mailbox/examples/idea-template.md`.

**Outbox** (`agent-lab-mailbox/outbox/`): Nexus writes responses here. Processed mailbox state is kept in the dedicated mailbox repository rather than under `agent-lab/communication/`.

**Processing rules:**

- `idea` — appended to `IDEAS.md` under `### Mailbox submissions` with state `new`; response committed and pushed.
- `task` — body stored in SQLite and submitted through the mailbox task/experiment workflow.
- `question` — acknowledged; response placed in outbox.
- `context` — stored in the `knowledge` table under category `mailbox_context`.

**Safety:**

- Files over 64 KB, binary files, symlinks, path traversals, unknown types, and files containing secrets (e.g. `sk-`, `API_KEY=`) are rejected.
- Maximum 3 retries per message.
- Single-process lock prevents concurrent runs.

**Run manually:**

```bash
python mailbox_processor.py [--dry-run] [--db-path PATH] [--code-repo-path PATH] [--mailbox-repo-path PATH]
```

The VM normally sets `AGENT_CODE_REPO_PATH=/agent` and `AGENT_MAILBOX_REPO_PATH` to the separate mailbox checkout.

## Current status

- V0 (`agent.py`) works locally and on Azure VM.
- V0 supports Azure OpenAI calls and logs run artifacts under `state/runs/`.
- V1 (MCP-based) is in progress.
- API service (`api.py`) provides:
	- public `GET /health`
	- public `GET /activity/public` and `GET /experiments/{id}/public` with
	  strict allowlisted observability fields (no prompts, diffs, raw logs, or paths)
	- authenticated `GET /secure-health`
	- `GET /status`, `GET /problems`, `GET /ideas`, `POST /chat` (read-only over
	  existing data; see `data_sources.py` and `chat.py`)
	- authenticated `POST /ideas` to append a new idea (state `new`) to
	  `IDEAS.md`; see "Adding ideas" below
	- authenticated Codex task endpoints (`POST /codex/tasks`,
	  `GET /codex/tasks`, `GET /codex/tasks/{id}`,
	  `POST /codex/tasks/{id}/run`) — synchronous, `read-only` sandbox,
	  `working_dir` fixed to `/agent`; see "Codex task API" below

## Current direction

The project is not run from a fixed roadmap. Open, unprioritized ideas
for future work live in `IDEAS.md` and may be added to by humans or
agents at any time; see that file for current candidate ideas (V1 MCP
completion, benchmarking, provider abstraction, cheaper models,
evaluation gating, controlled self-improvement, and more) and their
lightweight states (`new`, `exploring`, `selected`, `tested`, `adopted`,
`rejected`).

## Local quick start

Requirements:

- Python 3.10+
- Azure OpenAI deployment (default expected name: `gpt-4o`)

Environment variables:

- Required:
	- `AZURE_OPENAI_ENDPOINT` (example: `https://<resource>.openai.azure.com`)
	- `AZURE_OPENAI_API_KEY`
- Optional:
	- `AZURE_OPENAI_DEPLOYMENT` (default: `gpt-4o`)
	- `AGENT_FS_ROOT` (default: `/agent`)
	- `MCP_FILESYSTEM_SERVER_COMMAND` (default: `npx`)
	- `MCP_FILESYSTEM_SERVER_ARGS` (default uses `@modelcontextprotocol/server-filesystem`)

Run V0:

- `python agent.py "Summarize the benefits of unit tests in 3 bullets."`

Run output:

- Response prints to stdout.
- Structured run log is written to `state/runs/*.json`.

## API service quick start

Dependencies for `api.py` and its tests are pinned in `requirements-api.txt`
(separate from `requirements-agent.txt`, which is for the Azure OpenAI/MCP
CLI runner):

- `python3 -m venv .venv && .venv/bin/pip install -r requirements-api.txt`
- `AGENT_API_KEY=<value> .venv/bin/uvicorn api:app --reload`
- Optional for GitHub mailbox webhook: `AGENT_GITHUB_WEBHOOK_SECRET=<value>`
- Run tests: `AGENT_API_KEY=test-key .venv/bin/python -m unittest discover -s tests -q`

## MCP endpoint quick start

The FastAPI service also mounts a read-only MCP HTTP endpoint for Nexus at
`/mcp/`.

- Start it with the normal API server:
  - `python3 -m venv .venv && .venv/bin/pip install -r requirements-api.txt`
  - `AGENT_API_KEY=<value> .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000`
- MCP URL: `http://127.0.0.1:8000/mcp/`
- Authentication: send the same `x-api-key: <AGENT_API_KEY>` header used by
  `/secure-health`, `/experiments`, `/codex/tasks*`, and related authenticated
  API routes.

Available MCP tools mirror the current **read-only** Nexus API surface:

- `health`, `secure_health`, `status`, `problems`, `ideas`, `chat`
- `codex_tasks_list`, `codex_task_get`
- `mailbox_tasks_list`, `mailbox_task_get`, `mailbox_messages_list`
- `experiments_list`, `experiment_get`, `experiment_logs`,
  `experiment_adoption_get`, `experiment_public_get`
- `activity_public_list`, `activity_list`, `diagnostics_public_get`
- `operations_catalog_get`, `operation_chains_list`, `operation_chain_get`
- `maintenance_status_get`, `maintenance_audit_get`

Intentionally **not** exposed over MCP in this task:

- all write endpoints (`POST /ideas`, task approval/rejection, experiment
  adoption, operation-chain creation, etc.)
- shell-backed `/operations/*` execution endpoints
- webhook endpoints

Example remote MCP client configuration:

```json
{
  "mcpServers": {
    "nexus": {
      "type": "streamable-http",
      "url": "https://agent-lab.example.com/mcp/",
      "headers": {
        "x-api-key": "${AGENT_API_KEY}"
      }
    }
  }
}
```

Notes:

- The endpoint returns compact structured tool results to reduce token usage.
- Experiment logs stay bounded through the existing `limit` parameter exposed by
  the `experiment_logs` MCP tool.
- The trailing slash in `/mcp/` is part of the endpoint URL and should be kept
  in client configuration.

## Adding ideas

`POST /ideas` (authenticated with `x-api-key`, same as `/secure-health` and
`/codex/tasks*`) appends a new idea to `IDEAS.md`:

- Request body: `{"text": "..."}` — non-empty after trimming, at most 500
  characters.
- The idea is sanitized (collapsed to one line, backticks stripped, no
  leading `#`/`-`/`>`) so it cannot forge a Markdown heading or a
  `` `state` `` marker, then appended with state `new` under a fixed
  `### Inbox` subsection at the end of the `## Ideas` section.
- Writes are guarded by an exclusive file lock (`fcntl.flock`) so
  concurrent requests append one at a time instead of racing.
- The Web UI's "Idee hinzufügen" field/button on `/` calls this endpoint
  and refreshes the ideas list on success.

## Codex task API

Small, authenticated FastAPI wrapper around `modules.codex_task_service.CodexTaskService`,
which itself reuses `modules.codex_worker.CodexWorker` (`codex exec`, never
`shell=True`). Currently synchronous only, no chat/Web UI integration, no
background queue, no parallel execution.

Server-enforced boundaries (not client-selectable):

- `working_dir` is always `/agent`.
- `sandbox` is always `read-only`.
- `timeout_seconds` defaults to 120s and cannot exceed 120s.

All four endpoints require the same `x-api-key` header as `/secure-health`.

- `POST /codex/tasks` — create a task (`{"prompt": "..."}`), returns it `queued` (`201`).
- `GET /codex/tasks?limit=20` — list the most recent tasks (newest first, summary shape).
- `GET /codex/tasks/{id}` — fetch one task's full state (`404` if unknown).
- `POST /codex/tasks/{id}/run` — run a `queued` task synchronously and return
  its final persisted state (`409` if not `queued`, `404` if unknown). Worker
  failures/timeouts are returned as a stored task status, not an HTTP 500.

Example flow:

```
curl -X POST -H "x-api-key: $AGENT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Inspect README.md and summarize the project."}' \
  http://127.0.0.1:8000/codex/tasks
# -> {"id": "...", "status": "queued", ...}

curl -X POST -H "x-api-key: $AGENT_API_KEY" \
  http://127.0.0.1:8000/codex/tasks/<id>/run
# -> {"id": "...", "status": "succeeded", "final_message": "...", ...}

curl -H "x-api-key: $AGENT_API_KEY" http://127.0.0.1:8000/codex/tasks/<id>
```

Orphaned `running` tasks (left behind by a crash/restart) are marked `failed`
exactly once at API startup via `mark_interrupted_tasks_failed()`; startup
never restarts execution or starts a Codex process.

## Mailbox task approval API

Task messages from the mailbox create `pending_approval` records that must be
reviewed and approved by a human operator before any code runs.

All endpoints require the `x-api-key` header.

- `GET /mailbox/tasks?limit=20` — list recent approvals (all statuses, newest first).
- `GET /mailbox/tasks/{id}` — fetch one approval.
- `POST /mailbox/tasks/{id}/approve` — approve; triggers `ExperimentService` in background.
- `POST /mailbox/tasks/{id}/reject` — reject (`{"reason": "..."}`).
- `POST /mailbox/webhook/github` — GitHub webhook trigger (`X-Hub-Signature-256`, HMAC-SHA256).

State machine: `pending_approval → approved → submitted → running → succeeded / failed / needs_review`.
A background `MailboxTaskWorker` daemon (10 s poll interval) calls
`submit_approved()` + `poll_running()` on each tick. Experiments run
serially (one VM, one budget). The Web UI Task Approvals card shows all records
and provides Approve/Reject buttons when an API key is entered.

## Nexus activity API

`GET /activity/public` returns the latest 100 sanitized lifecycle events without
authentication. `GET /experiments/{id}/public` returns the corresponding public
experiment summary. The website displays this feed and refreshes it every 10 seconds.
Public experiment lookups accept either the full ID or a unique lowercase hexadecimal
prefix of at least eight characters.

Authenticated `GET /activity` accepts `experiment_id` and `mailbox_task_id` filters
and includes at most the final 100 sanitized test-output lines per event.
Authenticated `GET /experiments/{id}` retains the detailed experiment diagnostics.

## Nexus diagnostics API

`GET /diagnostics/public` (optional `?limit=1..100`, default 10) is a single,
unauthenticated endpoint that consolidates everything needed to remotely
diagnose Nexus in one call. It is built exclusively from an explicit field
allowlist — never from a generic serialization of internal records — and
redaction is applied *before* persistence (in `modules/redaction.py`), so
activity events stored in the database are already safe to return.

**Public fields:**

- `service`: `status`, `git_revision` (short commit hash), `deployment_time`.
- `mailbox`: `status`, `pending_count`, `latest_tasks` (each: `id`, `status`,
  `received_at`, `experiment_id` only — never `task_body`, `sender`, or
  `file_path`).
- `experiments` (newest first, up to `limit`), each with: `id` (full
  experiment ID), `mailbox_task_id`, `phase`, `status`, `created_at`,
  `ended_at`, `lifecycle` (per-phase `phase`/`started_at`/`ended_at`/
  `duration_ms` for research, selecting, implementing), `rejection_reason`
  (sanitized, bounded), `policy_rule` (matched safety-policy rule name),
  `test_command` (a redacted command name, e.g. `python -m unittest`, never
  raw argv with filesystem paths), `test_exit_code`, `failure_summary`,
  `changed_files` (basenames only), `diagnostic_messages` (bounded,
  sanitized activity-log messages), `usage` (aggregated token counts), and
  `estimated_cost` (best-effort, present only when pricing data is
  configured).
- `problems`: the same sanitized summaries as `GET /problems`.

**Never returned:** API keys, tokens, credentials, or headers; prompts or
user-provided task bodies; environment variables; source code, diffs, or
file contents; stack traces with internal paths; or unrestricted/raw
command output. The website's Diagnostics card and
`./scripts/nexus-logs.ps1` both display this same model.

## Nexus runtime observability API

`GET /api/runtime/status` (authenticated) returns a small, cheap,
explicit-state snapshot of what Nexus is doing right now (idle, or the
current task/phase/operation, task/operation durations, and heartbeat
health). `GET /api/runtime/trace` (authenticated) returns a bounded recent
history of hierarchical operations (spans) for the current/last trace.
Existing per-experiment JSONL logs are tagged with matching
`trace_id`/`span_id`/`parent_span_id` fields, and traces can optionally be
exported via OpenTelemetry OTLP to an external backend (e.g. Grafana
Cloud) — see `OBSERVABILITY.md` for setup and manual verification steps.

## Security

- Never commit credentials, API keys, private keys, or tokens.
- Keep `.env` local and out of source control.

## Infrastructure snapshot

- Azure resource group: `agent-lab-rg`
- Region: `germanywestcentral`
- VM: `agent-lab-vm` (`Standard_B1s`, Ubuntu 22.04)
- Azure OpenAI resource: `openAI-tire0011`
- Deployment: `gpt-4o` (`2024-05-13`, GlobalStandard)
- Public HTTPS host: `agent-lab-tire0011.germanywestcentral.cloudapp.azure.com`

## Additional documentation

- `AGENTS.md` - handoff rules for future AI agents
- `PROJECT.md` - scope, goals, unknowns
- `ARCHITECTURE.md` - current architecture and target evolution
- `IDEAS.md` - open, continuously extensible idea repository (not a roadmap)
- `DECISIONS.md` - decision log
- `COSTS.md` - budget and cost tracking policy
- `OPERATIONS.md` - operational runbook
- `OBSERVABILITY.md` - runtime observability (tracing, heartbeat, status API, Grafana Cloud setup)

## API CLI client

A small standalone API client is available as `Client/client.py`
(run as `python -m Client.client ...` from the repo root).

Examples:

- `python -m Client.client health`
- `python -m Client.client status`
- `python -m Client.client status --api-key <your-key>`
- `python -m Client.client chat "Was ist dein aktueller Status?"`
- `python -m Client.client chat --interactive`

Base URL configuration priority:

1. `--base-url`
2. `AGENT_API_BASE_URL`
3. default `http://127.0.0.1:8000`

Notes:

- Secrets are never stored in source; pass keys via environment variables or CLI.
- `api.py` exposes `/health`, `/secure-health`, `/status`, `/problems`, `/ideas`, `/chat`, and `/codex/tasks*`.

### Codex task commands

Thin HTTP wrappers around the Codex task API (see "Codex task API" above).
The client never invokes `CodexWorker`/`CodexTaskService` directly and cannot
choose `working_dir` or `sandbox` — those remain server-enforced. All five
commands require `x-api-key` (same auth as the other endpoints).

- `codex create --prompt "..." [--timeout-seconds N]` — create a task, print
  its id/status/timeout/sandbox/working_dir. Returns `201`.
- `codex get TASK_ID` — fetch and print a task's full state (`404` if unknown).
- `codex list [--limit N]` — compact, newest-first list (id, status, created
  time, runtime, short prompt preview). Uses the server's default/maximum
  limit; invalid values are rejected by the API.
- `codex run TASK_ID` — run an already-created `queued` task synchronously
  and print its final state. Not retried automatically on `409`/timeout/
  connection failure — inspect the task afterwards with `codex get`.
- `codex execute --prompt "..." [--timeout-seconds N]` — convenience command:
  create one task, print its id, run exactly that task, print the final
  state. Never creates a second task as a retry.

Create + run example:

```
python -m Client.client codex create --prompt "Inspect README.md and summarize the project." --api-key $AGENT_API_KEY
# -> Task created (HTTP 201) ... id: <TASK_ID> ...

python -m Client.client codex run <TASK_ID> --api-key $AGENT_API_KEY
# -> HTTP 200 ... status: succeeded ...
```

Execute example (create + run in one step):

```
python -m Client.client codex execute --prompt "Inspect README.md and summarize the project." --timeout-seconds 60 --api-key $AGENT_API_KEY
```

`codex run`/`codex execute` are synchronous: the HTTP request stays open for
up to the task's timeout (server max 120s). The client's own HTTP timeout is
sized to the task timeout plus a small margin, not unlimited. If the
connection is lost or the client-side timeout is hit, the task's real
outcome is unknown from the CLI's point of view — the command does **not**
retry; use the printed task id with `codex get TASK_ID` to check whether it
actually completed. Exit codes: `0` success (task not `failed`/`timed_out`),
`1` HTTP/transport failure, `2` usage error (e.g. missing API key), `3` HTTP
call succeeded but the task's own status is `failed` or `timed_out`.

## Integration tests (live API)

Integration tests are intentionally separated from unit tests and only run when
`AGENT_API_URL` is explicitly set.

Example:

- `AGENT_API_URL=https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com python -m unittest discover -s tests/integration -v`

Covered checks:

- HTTPS connectivity
- `GET /health`
- all currently existing OpenAPI endpoints whose path includes `health`, `status`, `problem`, or `chat`
- one full `client chat` invocation against the configured API
- understandable behavior for a non-existing endpoint (`HTTP 404`)

These tests avoid administrative actions and do not require secrets.
