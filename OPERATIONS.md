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

## Service behavior

- Public health endpoint: `GET /health`
- Protected endpoint example: `GET /secure-health` requires `x-api-key` header

## Operational checks

1. Verify API liveness via `/health`.
2. Verify protected endpoint rejects missing/invalid key.
3. Validate agent run writes new artifact in `state/runs/`.
4. Review run logs for token/runtime anomalies.

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

## Incident triage (minimal)

If agent/API behavior regresses:

1. Capture failing request/task and timestamp.
2. Correlate with latest `state/runs/*.json` entries.
3. Confirm env var presence and endpoint credentials.
4. Re-run minimal known-good task.
5. Log findings in `TODO.md`/`DECISIONS.md` before broad changes.
