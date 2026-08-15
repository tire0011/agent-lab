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
