# agent-lab

Experimental project for distributed self-improving LLM agents under strict budget constraints.

Repository development and verification conventions are documented in
[`CODING_GUIDELINES.md`](CODING_GUIDELINES.md).

## Mailbox

Nexus uses the separate `tire0011/agent-lab-mailbox` repository for Git-backed mailbox communication. GitHub push events affecting `inbox/` trigger immediate processing through `POST /mailbox/webhook/github`; the systemd timer remains as a one-minute fallback.

**Inbox** (`<AGENT_MAILBOX_REPO_PATH>/inbox/`): Drop a Markdown file with YAML front matter here to send a message to Nexus. This root-level directory is the only supported inbox; the code repository's former `communication/inbox/` path is not scanned.

Supported message types: `idea`, `task`, `question`, `context`, `refresh`.

Example: `agent-lab-mailbox/examples/idea-template.md`.

**Outbox** (`<AGENT_MAILBOX_REPO_PATH>/outbox/`): Nexus writes responses here. Processed mailbox state is kept in the dedicated mailbox repository rather than under `agent-lab/communication/`.

**Retention:** each poll deletes outbox responses older than
`NEXUS_MAILBOX_RETENTION_DAYS` days (default `5`; set `0` to disable), and drops
inbox messages that exhausted their retries — those are terminal and would
otherwise be re-read and re-skipped on every poll forever. A message's reason
for removal is logged before it goes, so `GET /logs/trace/{reference}` still
explains it afterwards. Files that a publisher or the commit-retry path still
needs are never deleted. The files leave the working tree; git history keeps
them.

**Processing rules:**

- `idea` — appended to `IDEAS.md` under `### Mailbox submissions` with state `new`; response committed and pushed.
- `task` — body stored in SQLite and submitted through the mailbox task/experiment workflow.
- `question` — acknowledged; response placed in outbox.
- `context` — stored in the `knowledge` table under category `mailbox_context`.
- `refresh` — moves the deployment checkout to the branch named in the front
  matter and restarts the allowlisted services. Carries no shell: the body is
  ignored, and `branch` is the only thing the message can influence — not the
  repository, the remote, or which services restart. An invalid branch name is
  rejected with a reason and nothing is touched. See issue #228.

  ```markdown
  ---
  type: refresh
  created_by: rene
  branch: main
  ---
  ```

**Safety:**

- Files over 64 KB, binary files, symlinks, path traversals, unknown types, and files containing secrets (e.g. `sk-`, `API_KEY=`) are rejected.
- Maximum 3 retries per message.
- Single-process lock prevents concurrent runs. A run that finds the lock held
  logs `Mailbox poll skipped: another processor is already running` and exits
  successfully — the processor that holds the lock re-scans the inbox after
  every batch, so a message that arrives mid-run is handled by that same worker
  instead of waiting for the next poll. The drain is bounded by
  `AGENT_MAILBOX_DRAIN_MAX_PASSES` (default `5`) and
  `AGENT_MAILBOX_DRAIN_MAX_SECONDS` (default `60`); anything left over stays in
  the inbox for the next poll. A failed sync with the remote is still a real
  fault and exits non-zero, even if earlier batches succeeded. See
  `OPERATIONS.md`.

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
	- authenticated coding task endpoints (`POST /codex/tasks`,
	  `GET /codex/tasks`, `GET /codex/tasks/{id}`,
	  `POST /codex/tasks/{id}/run`) — synchronous, `read-only` sandbox,
	  `working_dir` fixed to `/agent`; see "Coding task API" below

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
- Required for Nexus implementation tasks: `GITHUB_REPOSITORY=owner/repository`
  and `GITHUB_TOKEN=<fine-grained token with Issues, Contents and Pull requests write access plus Actions read access>`.
  Without them the API remains available for read-only work, but implementation
  fails before an experiment starts.
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

- all write endpoints (`POST /ideas`, experiment
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

The tool router itself (`find_tool`, `call_tool`) is a separate stdio server:
`python -m modules.tool_router_mcp`. See `docs/issue-118-tool-router-mcp.md`,
which includes the Copilot CLI setup.

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

## Coding task API

Small, authenticated FastAPI wrapper around `modules.codex_task_service.CodexTaskService`,
which itself reuses the coding-agent workers — `modules.codex_worker.CodexWorker`
(`codex exec`) and `modules.claude_worker.ClaudeWorker` (`claude --print`),
never `shell=True`. Currently synchronous only, no chat/Web UI integration, no
background queue, no parallel execution.

Which agent runs a task is selected by a persistent round-robin when the task is created and
reported back as `provider` (see ARCHITECTURE.md section 13 and DECISIONS.md
D-022). The `/codex/tasks` paths are kept for compatibility; they are not
Codex-only. `modules.codex_task_service`'s own CLI takes `--provider` to name
one agent explicitly.

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
# -> {"id": "...", "status": "queued", "provider": "claude", ...}

curl -X POST -H "x-api-key: $AGENT_API_KEY" \
  http://127.0.0.1:8000/codex/tasks/<id>/run
# -> {"id": "...", "status": "succeeded", "final_message": "...", ...}

curl -H "x-api-key: $AGENT_API_KEY" http://127.0.0.1:8000/codex/tasks/<id>
```

Orphaned `running` tasks (left behind by a crash/restart) are marked `failed`
exactly once at API startup via `mark_interrupted_tasks_failed()`; startup
never restarts execution or starts a Codex process.

## Mailbox task API

Task messages from the mailbox go straight to Nexus: each creates a `queued`
record in `mailbox_task_approvals`, and the task worker hands it to the local
controller. There is no approval step (#264).

All endpoints require the `x-api-key` header.

- `GET /mailbox/tasks?limit=20` — list recent tasks (all statuses, newest first).
- `GET /mailbox/tasks/{id}` — fetch one task.
- `POST /mailbox/webhook/github` — GitHub webhook trigger (`X-Hub-Signature-256`, HMAC-SHA256).

State machine: `queued → submitted → running → succeeded / failed / needs_review`.
A background `MailboxTaskWorker` daemon (10 s poll interval) calls
`submit_queued()` + `poll_running()` on each tick. Experiments run
serially (one VM, one budget). Results reach the outbox as
`task-approval-result-<id>.md` (file name kept for compatibility). The Web UI
"Mailbox-Aufträge" card lists the records.

Records from before #264 stay readable: `approved` rows became `queued` on
migration; `pending_approval` and `rejected` rows keep their status and are
never run.

## Nexus logs API

Reading logs needs **no API key** (see `DECISIONS.md` D-012 — this is a
single-operator experiment VM and analysis is the point of the log surface).
Write endpoints are unaffected and still require `x-api-key`.

`GET /logs` is one merged, chronological timeline: the structured JSONL entries
(mailbox intake, controller decisions, implementation, verification, chat jobs)
plus the `agent-api`/`nexus-mailbox`/`llama-server` journals.

```bash
# Full-text search across the whole retained history - message, event,
# command, stdout/stderr, exception text and the correlation ids alike.
curl -s "$BASE/logs?search=disk+quota+exceeded"

# Narrow further; page back with the returned next_cursor.
curl -s "$BASE/logs?level=ERROR&source=controller&limit=50"
```

`truncated: true` in the response means the scan stopped at its byte/file budget,
so older matches may exist — it is never silently presented as an exhaustive result.

`GET /logs/trace/{reference}` answers the other question: *Nexus was given this —
what did it actually do?* The reference can be an inbox message filename, an
experiment id, a mailbox task id, a controller session/chat job id, or just a
fragment you remember. Nexus resolves whichever entries that identifies, reads the
correlation ids off them, and returns the **whole** chain plus a summary:

```bash
curl -s "$BASE/logs/trace/inbox/20260906-1142-refresh-nexus-claude.md"
curl -s "$BASE/logs/trace/exp-refresh-01"
curl -s "$BASE/logs/trace/verification%20succeeded"
```

An unresolvable reference returns `found: false` with an empty timeline rather
than a 404. The Logs tab exposes both: a free-text search box over the full
history, and an "Auftrag verfolgen" field (plus a per-entry button) that renders
the same trace with its summary.

## Nexus diagnostic commands

`/logs` and `/logs/trace` return log *entries*. The diagnostic commands answer
the operator's actual question — *where did this stop, and is it still alive?* —
by joining the lifecycle state onto those logs, so diagnosing a failure never
means searching the filesystem or inventing shell commands (#188). They need
`x-api-key`, and every one of them is read-only — the state database is opened
`mode=ro` with `PRAGMA query_only`, so these commands cannot change what they
describe:

```bash
curl -sH "x-api-key: $KEY" "$BASE/diagnostics/status/<reference>"
curl -sH "x-api-key: $KEY" "$BASE/diagnostics/logs/<reference>?lines=50"
curl -sH "x-api-key: $KEY" "$BASE/diagnostics/logs/service/mailbox?lines=100"
curl -sH "x-api-key: $KEY" "$BASE/diagnostics/logs/last-failure"
```

`<reference>` is whatever you have: a mailbox task id, an experiment id, a
controller session id, a unique prefix of any of those, an issue or PR number
(`188`, `%23188`, `PR%20%23195`), or an inbox message filename. `<service>`
takes friendly names (`api`, `mailbox`, `llm`) as well as unit names — no
`journalctl` to construct.

The answer carries `lifecycle` (`queued`/`running`/`waiting`/`needs_review`/
`succeeded`/`failed`/`rejected`/`merged`/`abandoned`) with branch, commit,
issue, PR, worker, last error and the correlation ids, plus bounded log
entries. And it carries `progress`: **running**, **interrupted**, **completed**
or **not_started**. That last field is the one #188 exists for. A worker that
dies mid-run never writes a `failed` status — its log just stops, which reads
exactly like work still in flight — so liveness is derived from the newest
progress signal: a controller heartbeat (#162) where there is one, otherwise
the newest log entry, otherwise the row's `updated_at`. `progress.source` says
which answered; `progress.reason` explains the verdict in one line. For the
same reason `last-failure` treats a stalled subject as a failure.

The identical commands are in the operations catalog as `nexus_status`,
`nexus_logs` and `nexus_last_failure`, so ChatGPT and the chat interface reach
them without HTTP — and the failure-analysis routine reads the same backend,
so its diagnosis and your `nexus logs` see the same facts.

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

Thin HTTP wrappers around the coding task API (see "Coding task API" above).
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
