# OPERATIONS.md

## Purpose

Operational runbook for running and maintaining `agent-lab` on local machine and Azure VM.

## Environments

### Local

- Used for fast iteration and baseline testing.

### Azure VM

- Host: `agent-lab-vm` (Ubuntu 22.04, `Standard_B1s`)
- Resource group: `agent-lab-rg`
- Region: `germanywestcentral`
- Public HTTPS host: `agent-lab-tire0011.germanywestcentral.cloudapp.azure.com`
- Web stack: FastAPI + nginx + Let's Encrypt TLS

## Required environment variables

For `agent.py`:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- Optional: `AZURE_OPENAI_DEPLOYMENT` (default `gpt-4o`)
- Optional: `AGENT_FS_ROOT`
- Optional: `MCP_FILESYSTEM_SERVER_COMMAND`
- Optional: `MCP_FILESYSTEM_SERVER_ARGS`

For API auth in `api.py`:

- `AGENT_API_KEY`
- `AGENT_GITHUB_WEBHOOK_SECRET` (only required when using `POST /mailbox/webhook/github`)

For the mandatory Nexus implementation lifecycle, `/etc/agent-lab/agent.env`
must also provide:

- `GITHUB_REPOSITORY=tire0011/agent-lab`
- `GITHUB_TOKEN=<fine-grained token>` with write access to Issues, Contents and
  Pull requests. The token value must never be logged.

Both `agent-api.service` and `nexus-mailbox.service` already load this shared
environment file. Missing configuration blocks only implementation/resume work;
the API and read-only operations remain available and log an actionable startup
error.

## Service behavior

- Public health endpoint: `GET /health`
- Protected endpoint example: `GET /secure-health` requires `x-api-key` header
- Task approval endpoints: `GET /mailbox/tasks`, `POST /mailbox/tasks/{id}/approve|reject` — require `x-api-key`
- GitHub mailbox webhook: `POST /mailbox/webhook/github` — authenticated via `X-Hub-Signature-256`
- `MailboxTaskWorker` daemon starts at API startup, polls every 10 s for approved tasks to submit

## Operational checks

1. Verify API liveness via `/health`.
2. Verify protected endpoint rejects missing/invalid key.
3. Validate agent run writes new artifact in `state/runs/`.
4. Review run logs for token/runtime anomalies.
5. Check `GET /mailbox/tasks` for pending approvals requiring human review.

## Security operations

- Never commit secrets.
- Runtime secrets/config for `run-agent.sh` must be stored in `/etc/agent-lab/agent.env` (outside `/agent`).
- Set ownership/permissions so only `agent` and root can read it (owner `agent`, mode `600`).
- Keep `.env` local and excluded from VCS.
- Rotate keys immediately if accidental exposure is suspected.
- Apply least privilege for VM managed identity and Azure roles.

## Deployment/infra notes

- Git and GitHub CLI are configured on VM.
- Codex CLI is being installed/evaluated (not yet a required dependency).
- VM managed identity exists but currently has no broad Azure permissions.

## Known limitations

- External ChatGPT direct access to project HTTP endpoint was not reliable; do not depend on this path yet.
- Persistent structured DB logging is not yet implemented (JSON logs are current source).

## Mailbox systemd service

The mailbox processor runs as a systemd oneshot service on a 1-minute timer.
Runtime state now defaults outside the Git worktree:

- SQLite DB: `/var/lib/nexus/agent.db`
- Mailbox lock: `/var/lib/nexus/mailbox.lock`

### Service unit (`deploy/nexus-mailbox.service`)

```
[Unit]
Description=Nexus mailbox processor
After=network.target

[Service]
Type=oneshot
User=agent
WorkingDirectory=/agent
StateDirectory=nexus
EnvironmentFile=/etc/agent-lab/agent.env
Environment=AGENT_DB_PATH=/var/lib/nexus/agent.db
Environment=AGENT_MAILBOX_LOCK_PATH=/var/lib/nexus/mailbox.lock
ExecStart=/agent/.venv/bin/python /agent/mailbox_processor.py
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Timer unit (`deploy/nexus-mailbox.timer`)

```
[Unit]
Description=Run Nexus mailbox processor every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
Unit=nexus-mailbox.service

[Install]
WantedBy=timers.target
```

### Installation

```bash
sudo cp deploy/nexus-mailbox.service /etc/systemd/system/
sudo cp deploy/nexus-mailbox.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nexus-mailbox.timer
sudo systemctl start nexus-mailbox.timer
# Verify
systemctl status nexus-mailbox.timer
```

### Database migration (`/agent/state/agent.db` -> `/var/lib/nexus/agent.db`)

Do this once on the VM before/with service rollout.

```bash
sudo systemctl stop nexus-mailbox.timer nexus-mailbox.service
sudo install -d -o agent -g agent -m 770 /var/lib/nexus
sudo -u agent /agent/.venv/bin/python /agent/mailbox_processor.py --dry-run
```

The first DB open on the new path auto-migrates data from `/agent/state/agent.db`
when `/var/lib/nexus/agent.db` does not yet exist. After that:

```bash
sudo ls -l /var/lib/nexus/agent.db
sudo -u agent /agent/.venv/bin/python - <<'PY'
import sqlite3
conn = sqlite3.connect("/var/lib/nexus/agent.db")
print(conn.execute("SELECT COUNT(*) FROM mailbox_messages").fetchone()[0])
conn.close()
PY
```

Then remove the temporary index workaround and restart:

```bash
cd /agent
git update-index --no-skip-worktree state/agent.db || true
sudo systemctl daemon-reload
sudo systemctl start nexus-mailbox.timer
```

Keep `/agent/state/agent.db` as backup until validation is complete.

### Rollback

If needed, point services back to the in-repo DB temporarily by overriding env vars:

```bash
sudo systemctl stop nexus-mailbox.timer nexus-mailbox.service
echo 'AGENT_DB_PATH=/agent/state/agent.db' | sudo tee -a /etc/agent-lab/agent.env
echo 'AGENT_MAILBOX_LOCK_PATH=/agent/state/mailbox.lock' | sudo tee -a /etc/agent-lab/agent.env
sudo systemctl daemon-reload
sudo systemctl start nexus-mailbox.timer
```

For a clean rollback, remove duplicate keys from `/etc/agent-lab/agent.env` and
keep only the intended values.

## Incident triage (minimal)

If agent/API behavior regresses:

1. Capture failing request/task and timestamp.
2. Correlate with latest `state/runs/*.json` entries.
3. Confirm env var presence and endpoint credentials.
4. Re-run minimal known-good task.
5. Log findings in `IDEAS.md`/`DECISIONS.md` before broad changes.

## Maintenance API (temporary remote-maintenance bridge)

The maintenance API provides a fixed set of named operations for remote VM
maintenance without keeping SSH open.  **It is disabled by default** and must
be explicitly enabled for each maintenance window.

### Enabling the maintenance window

Add to `/etc/agent-lab/agent.env` (or export before starting the API):

```bash
MAINTENANCE_ENABLED=true
MAINTENANCE_TOKEN=<random-long-token>           # second auth factor
MAINTENANCE_EXPIRY=2026-08-20T22:00:00Z        # auto-disable after this UTC time
```

Reload the service:
```bash
sudo systemctl daemon-reload && sudo systemctl restart agent-api
```

### Disabling after use

Remove or unset `MAINTENANCE_ENABLED` and `MAINTENANCE_TOKEN`, then reload:
```bash
sudo systemctl daemon-reload && sudo systemctl restart agent-api
```

### Authentication

All `/maintenance/*` endpoints require **both**:
- `X-Api-Key: <AGENT_API_KEY>` (normal API key)
- `X-Maintenance-Token: <MAINTENANCE_TOKEN>` (second factor, only for `/maintenance/execute`)

`/maintenance/status` and `/maintenance/audit` need only `X-Api-Key`.

### Available operations

| Operation           | Required args              | Description                                      |
|---------------------|---------------------------|--------------------------------------------------|
| git_pull            | —                         | `git pull` in the repo root                      |
| git_status          | —                         | `git status --short`                             |
| git_log             | `lines` (1–50, default 10)| Recent one-line git log                          |
| run_router_test     | `prompt` (required)       | Start `agent.py --use-router <prompt>` in bg     |
| router_task_status  | `task_id`                 | Status of a background router test task          |
| router_task_result  | `task_id`                 | Output of a completed router test task           |
| run_tests           | —                         | Run the repo test suite in background            |
| test_task_status    | `task_id`                 | Status of a background test run                  |
| test_task_result    | `task_id`                 | Output of a completed test run                   |
| local_model_status  | —                         | `GET /v1/models` against local llama-server      |
| local_model_test    | `prompt` (optional)       | Short chat completion against local llama-server |
| service_status      | `service`                 | `systemctl is-active` for allowed service        |
| service_restart     | `service`                 | `sudo systemctl restart` for allowed service     |
| service_logs        | `service`, `lines`        | `journalctl` tail for allowed service            |
| process_status      | —                         | Filtered `ps aux` for known Nexus processes      |

Allowed service names: `llama-server`, `agent-api`.

### Example: run the test suite remotely

```bash
# Launch test run
curl -s -X POST https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com/maintenance/execute \
  -H "X-Api-Key: $AGENT_API_KEY" \
  -H "X-Maintenance-Token: $MAINTENANCE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operation": "run_tests"}' | jq .

# Poll result (use task_id from above)
curl -s -X POST .../maintenance/execute \
  -H "X-Api-Key: $AGENT_API_KEY" \
  -H "X-Maintenance-Token: $MAINTENANCE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operation": "test_task_result", "args": {"task_id": "<id>"}}' | jq .
```

### Security properties

- Disabled by default; no operations execute unless `MAINTENANCE_ENABLED=true`.
- Requires two independent auth factors (`X-Api-Key` + `X-Maintenance-Token`).
- `MAINTENANCE_EXPIRY` auto-disables the window at a specified UTC time.
- No arbitrary command strings; only the named operations above are accepted.
- Only `llama-server` and `agent-api` are accepted as service names.
- All output is truncated and redacted (API keys, tokens, secrets).
- Every operation call is written to an in-memory audit log (`GET /maintenance/audit`).

---

## MCP write tools (`MCP_WRITE_ENABLED`)

The MCP server exposes write-capable tools (shell, git_pull, service_restart, …)
when `MCP_WRITE_ENABLED=true` is set.  These are disabled by default.

### Shell tool cgroup setup on the VM

The `shell` tool wraps every command in a transient systemd scope
(`systemd-run --scope`) to guarantee all spawned processes (including those
that call `setsid()`) are killed when the scope ends.  The tool refuses to
execute without a working scope.

Two modes are probed at startup:

1. **User scope** (`--user`): requires the service user to have an active user
   manager.  Enable this with:
   ```bash
   # Run once as root on the VM:
   loginctl enable-linger <service-user>   # e.g. loginctl enable-linger agent
   ```
   Then add `XDG_RUNTIME_DIR=/run/user/<uid>` to the service env file
   (`/etc/agent-api.env`) and restart `agent-api`.  User scope is the
   preferred mode.

2. **System scope** (no `--user` flag): used when the process has sufficient
   privileges (running as root or with `CAP_SYS_ADMIN`).  Applicable when
   `agent-api` runs as root or is granted the capability explicitly.

If neither probe succeeds the shell tool returns `success=False` with a
descriptive error message.  No unsafe pgid-only fallback is used.

Enable write tools at runtime:
```bash
# Add to /etc/agent-api.env, then restart:
MCP_WRITE_ENABLED=true
XDG_RUNTIME_DIR=/run/user/1001    # replace 1001 with the service user's UID
```
