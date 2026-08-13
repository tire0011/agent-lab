# agent-lab

Experimental project for distributed self-improving LLM agents under strict budget constraints.

## Current status

- V0 (`agent.py`) works locally and on Azure VM.
- V0 supports Azure OpenAI calls and logs run artifacts under `state/runs/`.
- V1 (MCP-based) is in progress.
- API service (`api.py`) provides:
	- public `GET /health`
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
- Run tests: `AGENT_API_KEY=test-key .venv/bin/python -m unittest discover -s tests`

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
