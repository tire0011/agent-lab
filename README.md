# agent-lab

Experimental project for distributed self-improving LLM agents under strict budget constraints.

## Current status

- V0 (`agent.py`) works locally and on Azure VM.
- V0 supports Azure OpenAI calls and logs run artifacts under `state/runs/`.
- V1 (MCP-based) is in progress.
- API service (`api.py`) provides:
	- public `GET /health`
	- authenticated `GET /secure-health`
	- `GET /status`, `GET /problems`, `GET /todos`, `POST /chat` (read-only over
	  existing data; see `data_sources.py` and `chat.py`)
	- authenticated Codex task endpoints (`POST /codex/tasks`,
	  `GET /codex/tasks`, `GET /codex/tasks/{id}`,
	  `POST /codex/tasks/{id}/run`) — synchronous, `read-only` sandbox,
	  `working_dir` fixed to `/agent`; see "Codex task API" below

## Project priorities

1. Finish/test MCP-based V1.
2. Add persistent structured logging/database (SQLite first).
3. Benchmark V0 vs V1.
4. Add model/provider abstraction.
5. Evaluate cheaper Azure models and additional providers.
6. Add evaluation gates before self-modification.
7. Start controlled self-improvement experiments.

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
- `TODO.md` - prioritized implementation backlog
- `DECISIONS.md` - decision log
- `COSTS.md` - budget and cost tracking policy
- `OPERATIONS.md` - operational runbook

## API CLI client

A small standalone API client is available as `client.py`.

Examples:

- `python -m client health`
- `python -m client status`
- `python -m client status --api-key <your-key>`
- `python -m client chat "Was ist dein aktueller Status?"`
- `python -m client chat --interactive`

Base URL configuration priority:

1. `--base-url`
2. `AGENT_API_BASE_URL`
3. default `http://127.0.0.1:8000`

Notes:

- Secrets are never stored in source; pass keys via environment variables or CLI.
- `api.py` exposes `/health`, `/secure-health`, `/status`, `/problems`, `/todos`, and `/chat`.

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
