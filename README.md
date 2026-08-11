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
- Current `api.py` exposes only `/health` and `/secure-health`. If no chat endpoint exists,
  the `chat` command returns a clear error.

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
