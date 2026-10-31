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

For mailbox services:

- `AGENT_MAILBOX_REPO_PATH` is required and names the dedicated mailbox checkout.
  Its only supported layout is root-level `inbox/`, `outbox/`, and `archive/`;
  there is no fallback to `/agent/communication/`.

For the mandatory Nexus implementation lifecycle, `/etc/agent-lab/agent.env`
must also provide:

- `GITHUB_REPOSITORY=tire0011/agent-lab`
- `GITHUB_TOKEN=<fine-grained token>` with write access to Issues, Contents and
  Pull requests. The token value must never be logged.

Optional, for controller work liveness (see ARCHITECTURE.md section 10):

- `NEXUS_HEARTBEAT_INTERVAL_SECONDS` (default 30) - how often a running
  controller turn reports itself alive.
- `NEXUS_STALE_HEARTBEAT_SECONDS` (default 900) - how long a claim may go
  without a heartbeat before it is marked abandoned. Clamped to a safe floor:
  zero, negative or unparsable values fall back to the default rather than
  reaping work that is still running.
- `NEXUS_STALE_SESSION_SECONDS` (default 6 h) - the older `updated_at`-based
  fallback, which now only applies to sessions that never reported a
  heartbeat at all.

Optional, for experiment worktree retention (issue #376). Every experiment
keeps its worktree under `~/agent-worktrees/<experiment-id>` after it ends; the
API at startup and every `nexus_tick` run remove those that are no longer needed.

- `NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS` (default 168, i.e. 7 days) - how
  long after its `ended_at` a terminal experiment (`succeeded`, `failed`,
  `interrupted`, `timed_out`, `needs_review`, `cancelled`, `rejected`) keeps its
  worktree. Only the directory is removed: the experiment row, its log and its
  branch are kept. Worktrees of non-terminal experiments, of experiments with
  an unfinished adoption, of experiments with no `ended_at`, and paths outside
  the worktrees root are never touched. `0` disables cleanup, and so does an
  unparsable or negative value. Each removal is recorded as a
  `worktree_removed` activity event, and each failure as
  `worktree_cleanup_failed`. A failure never blocks startup or a tick.

Optional, for the experiment stall sweep (issue #400). The API every 5 minutes,
and every `nexus_tick` run (at the start and every 5 minutes while it waits for
its experiment), fail active experiments (`queued`, `selecting`,
`implementing`, `verifying`) whose newest experiment log entry or activity
event is older than the phase's hard limit (the coding agent cap,
`NEXUS_AGENT_MAX_SECONDS`, default and upper bound 3 h, a shorter value applies;
for `verifying` the larger of that and `NEXUS_TEST_SAFETY_CAP_SECONDS`) plus a
grace. The worker's process group, if
still recorded and alive, is stopped; the experiment becomes `failed` with the
error `stalled: no progress for <N>s in phase <phase>` and its log gets a
`stalled_detected` entry naming the last event seen. A live coding agent logs
`coding_agent_progress` every 10 minutes, so a healthy run never reaches the
limit. A stalled experiment counts as a failed attempt for issue retries (#374).

- `NEXUS_EXPERIMENT_STALL_GRACE_SECONDS` (default 900, i.e. 15 minutes) - the
  grace on top of the phase limit. An unparsable or negative value falls back
  to the default.

Optional, for the local verification test watchdog (issue #209). Local
verification is bounded by *progress*, not by a fixed wall-clock timeout: the
run continues for as long as tests keep starting and finishing.

- `NEXUS_TEST_STALL_SECONDS` (default 900) - how long a run may go without a
  single test starting or finishing before it is aborted as stalled. Clamped
  to a 120 s floor, so a typo here cannot reintroduce a short fixed timeout.
- `NEXUS_TEST_WATCHDOG_INTERVAL_SECONDS` (default 60) - how often progress is
  checked, and therefore how often the test heartbeat reaches the experiment
  log.
- `NEXUS_TEST_SAFETY_CAP_SECONDS` (default 5400, i.e. 90 min) - the absolute
  ceiling on one local verification run. This is a final guard against a
  permanently orphaned process only; it must never be the mechanism that ends
  a healthy run, so it is clamped to at least 30 min and always kept above the
  stall timeout.
- `NEXUS_TEST_LOG_DIR` (default `<AGENT_LOG_DIR>/test-runs`) - where the
  dedicated per-test logs and heartbeat files are written. They are kept out
  of the global Nexus feed deliberately: the feed would otherwise gain ~3800
  entries per verification run.

To see what a running verification is doing:

```bash
cat "${AGENT_LOG_DIR:-state/experiment-logs}/test-runs/test-<experiment-id>.heartbeat.json"
tail -f "${AGENT_LOG_DIR:-state/experiment-logs}/test-runs/test-<experiment-id>.jsonl"
```

Optional, for the coding agents that execute tasks and experiments (see
ARCHITECTURE.md section 13 and D-022):

- `AGENT_CODEX_BIN` (default `/home/agent/.local/bin/codex`) - path to the
  Codex CLI.
- `AGENT_CLAUDE_BIN` (default: `claude` as resolved on `PATH`) - path to the
  Claude Code CLI. Requires a CLI that understands `--permission-prompts` and
  `--tools` (Claude Code 2.x).
- `AGENT_CLAUDE_MODEL` - optional model override for the Claude worker; unset
  means the CLI's own configured model.
- `AGENT_CODING_PROVIDERS` (default `codex,claude`) - which agents may be
  drawn.
- `AGENT_CODING_PROVIDER` - pin every task to one agent. Use this to stand an
  agent down during an incident (quota exhausted, CLI broken); remove it to
  return to the random draw.

An agent whose CLI is not installed is never drawn, so adding the variables
before installing the CLI is harmless. To check what the VM would draw from:

```bash
cd /agent && .venv/bin/python -c \
  "from modules.coding_provider import available_providers; print(available_providers())"
```

A single task can be run against one agent by name, which is the quickest way
to verify a freshly installed CLI without touching the random path:

```bash
cd /agent && .venv/bin/python -m modules.claude_worker \
  --working-dir /agent "Summarize README.md. Do not change files."
cd /agent && .venv/bin/python -m modules.codex_task_service \
  --provider claude --working-dir /agent "Summarize README.md."
```

Which agent ran a given piece of work is recorded: `provider` on
`GET /codex/tasks/{id}` and `implementation_provider` on
`GET /experiments/{id}`.

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

## Refreshing the live checkout

`scripts/refresh.sh` -> `scripts/nexus-refresh.ps1` -> `scripts/nexus-pull.ps1`
updates `/agent` and restarts the services. The pull step runs
`python -m modules.nexus_refresh --repo-root /agent`, which treats `/agent` as
deployable state rather than a workspace and self-heals a dirty checkout before
updating it (issue #186):

1. Local **work** - tracked modifications, staged changes, and untracked files
   that are not recognizable runtime output - is committed onto a rescue branch
   `nexus/rescue/<timestamp>-<short-head>`. Nothing is reset or force-checked-out,
   so the work stays in the repository and is reachable by name.
2. **Runtime artifacts** - legacy `communication/outbox` and `communication/inbox`
   drops, `state/`, logs, caches, databases, temporary files - are *moved* out of
   the repository into `/var/lib/nexus/runtime-archive/<timestamp>/` instead of
   being committed. Set `NEXUS_RUNTIME_ARCHIVE_DIR` (or `--archive-root`) to
   archive elsewhere; add deployment-specific globs via
   `NEXUS_RUNTIME_ARTIFACT_PATTERNS` (comma-separated) or repeated
   `--artifact-pattern` arguments.
3. Unknown untracked files count as work, not as artifacts: an unrecognized file
   is preserved on the rescue branch and reported, never deleted.
4. Only then is `main` checked out, verified clean, fetched and fast-forwarded.
   If `main` carries local commits, they are first preserved on a rescue branch
   and the branch reference is moved forward - no `git reset --hard`, no force push.
5. Services are restarted only after the updated checkout is verified.

If preservation or archiving fails, the refresh aborts *before* `main` is touched
and the checkout is left exactly as it was - the archive step rolls back any file
it already moved. Re-running the refresh is safe: a clean checkout needs no rescue,
and an artifact whose content is already archived is deduplicated (via
`<archive-root>/archive-index.json`) instead of copied again.

Useful flags:

```bash
# Report only what the refresh would say, in machine-readable form.
python -m modules.nexus_refresh --repo-root /agent --json

# Restart units as part of the refresh itself.
python -m modules.nexus_refresh --repo-root /agent --restart-services agent-api
```

Recovering rescued work:

```bash
git -C /agent branch --list 'nexus/rescue/*'
git -C /agent show <rescue-branch>
ls -R /var/lib/nexus/runtime-archive/
```

Experiments should still run on their own branches; this is a safety net, not
the normal workflow.

## Known limitations

- External ChatGPT direct access to project HTTP endpoint was not reliable; do not depend on this path yet.
- Persistent structured DB logging is not yet implemented (JSON logs are current source).

## Mailbox systemd service

The mailbox processor runs as a systemd oneshot service on a 1-minute timer.
Runtime state now defaults outside the Git worktree:

- SQLite DB: `/var/lib/nexus/agent.db`
- Mailbox lock: `/var/lib/nexus/mailbox.lock`

### Lock contention and inbox drain

Only one processor works the mailbox at a time. A timer invocation that finds
the lock already held logs

```
Mailbox poll skipped: another processor is already running
```

and **exits 0**, so normal contention never shows up as
`Failed to start Nexus mailbox processor`.

Nothing is lost by that early exit: the processor holding the lock re-scans the
inbox after each batch and keeps processing whatever arrived while it was busy,
until the inbox has no new messages. The inbox itself is the queue — there is no
separate pending-work flag.

The drain loop is bounded so continuous traffic cannot pin one worker forever.
Both bounds are checked *between* passes, so a batch in flight always finishes;
whatever is left stays in the inbox for the next timer run.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_MAILBOX_DRAIN_MAX_PASSES` | `5` | Processing passes per invocation, including the first |
| `AGENT_MAILBOX_DRAIN_MAX_SECONDS` | `60` | How long a worker may keep starting new passes |

Reaching either bound is logged (`Mailbox drain stopped ... reason=max_passes`
or `reason=max_seconds`) and still exits 0. Defaults are defined in
`modules/mailbox/config.py` (`MailboxDrainLimits`).

A failed resync is different: it ends the drain, keeps and reports the work
already committed, and the run exits **non-zero** with status `sync_failed`.
Unlike a held lock, that means the checkout is out of step with its remote and
messages are not being delivered — check the mailbox repo and network before
assuming the next timer run will recover.

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
# Let the service user schedule the transient agent-api restart after a finalized task
# (nexus-refresh.ps1 also runs this on every refresh).
sudo bash scripts/setup-nexus-permissions.sh
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

## Automatic reboot check (#332)

`deploy/nexus-reboot-check.timer` runs `deploy/nexus-reboot-check.service`
every 2 hours. The service runs `nexus_reboot_check.py`: when
`/var/run/reboot-required` exists, it calls the `machine_reboot` operation
(`MACHINE_REBOOT_COMMAND` in `modules/operations_registry.py`, reboot in 60
seconds). That operation refuses while an experiment is `selecting`,
`implementing` or `verifying`; the check then does nothing and tries again on
the next run. No controller and no model call is involved. Every attempt is
written to `operations_audit`.

`scripts/nexus-refresh.ps1` installs and enables the timer on every refresh
via `scripts/setup-reboot-check.sh`. To check it manually:

```bash
systemctl list-timers nexus-reboot-check.timer --no-pager
journalctl -u nexus-reboot-check.service -n 20 --no-pager
```

To disable it: `sudo systemctl disable --now nexus-reboot-check.timer`.

## Incident triage (minimal)

If agent/API behavior regresses:

1. Capture failing request/task and timestamp.
2. Correlate with latest `state/runs/*.json` entries.
3. Confirm env var presence and endpoint credentials.
4. Re-run minimal known-good task.
5. For a controller session that looks stuck, ask
   `GET /diagnostics/nexus/liveness` (needs `x-api-key`): it names the owning
   worker, when it last reported itself alive, and whether that claim is
   already stale. A live heartbeat means slow, not dead; a stale one is reaped
   at the next API start or `nexus_tick`, with `terminal_reason` recorded on
   the session.
6. Log findings in `IDEAS.md`/`DECISIONS.md` before broad changes.

Steps 1-2 above are the manual version of the diagnostic commands below; reach
for those first.

## Nexus diagnostic commands (#188)

Diagnosing a Nexus failure should never require composing shell commands,
knowing log file paths, or guessing systemd unit names. These endpoints answer
the usual questions directly. All of them are **read-only**: the backend opens the
state database in SQLite's `mode=ro` and pins it with `PRAGMA query_only`, so
a write is refused by the connection itself rather than merely absent from the
code. Nothing here creates the database, applies a migration, reaps or
retries. All require `x-api-key`.

| Question | Command |
| --- | --- |
| Where is this piece of work now? | `GET /diagnostics/status/<reference>` |
| ...and what do its logs say? | `GET /diagnostics/logs/<reference>?lines=50` |
| What has this service been doing? | `GET /diagnostics/logs/service/<service>?lines=100&since_minutes=60` |
| What broke or stalled most recently? | `GET /diagnostics/logs/last-failure` |

### References

`<reference>` is whatever you have to hand. All of these resolve:

- a mailbox task id, an experiment id or a controller session id;
- a unique **prefix** of any of those (`nexus status 3ef5b93`);
- an **issue or PR number**: `188`, `%23188` (`#188` URL-encoded), `PR%20%23195`;
- an **inbox message filename**: `2026-09-15-implement-188.md`.

`<service>` accepts friendly names - `api`, `mailbox`, `llm` - as well as unit
names. An unrecognized name is validated as a unit name before it is used, so
nothing shell-shaped reaches `journalctl`.

### What comes back

One JSON envelope, the same for every route: `lifecycle` (the state), plus
`log_entries`, `log_sources`, `truncated` and `notes`. The lifecycle object is
flat and stably named, joined across the mailbox, experiment, adoption, GitHub
link and controller-session tables:

```
lifecycle         queued | running | waiting | needs_review | succeeded
                  | failed | rejected | merged | abandoned
status            the owning subsystem's own status value
branch, commit, issue_number, pr_number
worker_id, service
last_error
correlation_ids   mailbox_task_id / experiment_id / session_id
progress          running | interrupted | completed | not_started | unknown
```

`progress` is the part worth reading twice. A worker that died mid-run never
wrote a `failed` status - its log simply stops, which looks exactly like work
still in flight. So liveness is derived from the newest **progress signal**: a
controller heartbeat (#162) when one exists, otherwise the newest log entry,
otherwise the row's `updated_at`. `progress.source` says which of the three
answered, and `progress.reason` is a one-line explanation you can quote. The
staleness threshold is the clamped one from `NEXUS_STALE_HEARTBEAT_SECONDS`;
no query can lower it far enough to call live work dead.

`last-failure` counts a stalled subject as a failure for the same reason.

Output is bounded by default (50 log entries; `lines=` up to 500).

### Worked example: a branch appeared but nothing followed

This is the #186/#187 shape - Nexus created the branch, then produced no
implementation commit and no PR:

```bash
curl -sH "x-api-key: $AGENT_API_KEY" \
  "https://<host>/diagnostics/logs/%23186" \
  | jq '{lifecycle: .lifecycle.lifecycle,
         progress: .lifecycle.progress.state,
         branch: .lifecycle.branch,
         pr: .lifecycle.pr_number,
         worker: .lifecycle.worker_id,
         last_event: .log_entries[-1].event}'
```

```json
{
  "lifecycle": "running",            // the row still claims to be working
  "progress": "interrupted",         // ...but nothing has reported progress since
  "branch": "nexus/issue-186",       // the branch exists
  "pr": null,                        // and nothing came after it
  "worker": "vm:1234:ab12",          // who owned it when it went quiet
  "last_event": "branch_created"     // where execution stopped
}
```

`lifecycle.progress.reason` spells the verdict out in one line, and
`notes` repeats it at the top level when the subject is interrupted.

### From the mailbox or chat

The same backend is in the operations catalog, so ChatGPT and the chat
interface reach it without HTTP:

- `nexus_status` - `reference`
- `nexus_logs` - `reference`, `limit`
- `nexus_last_failure` - `limit`
- `service_logs` - `service`, `limit`, `since_minutes`
- `nexus_llm_job` - `job_id`, `view` (#259)

All five are catalogued `mutates_state=False`, which is what makes them
available to the read-only diagnostic role. A reference that resolves to
nothing - or a database that cannot be read - comes back as an unsuccessful
result with a reason, not as an operation fault. The failure-analysis routine reads
the same backend, so a diagnosis and an operator looking at the same task see
the same facts.

### Inspecting one local LLM job (#259)

`nexus debug llm-job <job-id> [--result|--prompt]` reads one `local_llm_jobs`
row by its exact id, read-only (same `mode=ro` connection as above). On the VM:

```bash
./scripts/nexus.ps1 debug llm-job <job-id>            # summary
./scripts/nexus.ps1 debug llm-job <job-id> --result   # full result
./scripts/nexus.ps1 debug llm-job <job-id> --prompt   # system prompt + prompt
```

`nexus.ps1 debug` runs `python -m modules.nexus_diagnostics debug ...` from the
repository venv against `AGENT_DB_PATH` (default `/var/lib/nexus/agent.db`;
override with `--db`). An unknown id prints `error: no local LLM job ...` and
exits 1. The same answer is available without a shell:

| CLI | HTTP `GET /diagnostics/llm-jobs/<id>` (`x-api-key`) | Operation `nexus_llm_job` |
| --- | --- | --- |
| *(default)* | `?view=summary` | `{"job_id": "<id>"}` |
| `--result` | `?view=result` | `{"job_id": "<id>", "view": "result"}` |
| `--prompt` | `?view=prompt` | `{"job_id": "<id>", "view": "prompt"}` |

The default view shows role, session, status, timestamps, duration,
prompt/result size, error and a result preview; `result` prints the full
result, `prompt` the system prompt and prompt. Credential values are masked
(configured secrets, `sk-`/`ghp_` keys, `Bearer` tokens, passwords in URLs,
and values assigned to `password`/`api_key`/`token`/`secret`/... keys), but
paths and URLs are shown unchanged, so the text stays analysable. The model
name is not stored per job, so only `provider: local` is shown.

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

Service names are not restricted to an allowlist — Nexus must be able to
operate its own units (`nexus-tick`, `nexus-mailbox`, …) as they are added.
Any non-empty unit name is accepted; only an absent name is refused.

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
- Any non-empty systemd unit name is accepted; `service_restart` runs
  `sudo systemctl restart` on it. The gate is the maintenance window itself
  (disabled by default, two tokens, optional expiry), not a name list.
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
