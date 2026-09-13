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
  - `GET /logs` (no auth, see DECISIONS.md D-012) — one merged,
    chronological timeline across every experiment/mailbox-task/chat log
    plus the `agent-api`/`nexus-mailbox`/`llama-server` journals.
    `search` is executed inside the read path (a bounded full scan of the
    retained JSONL history, plus `journalctl --grep`), so it finds matches
    anywhere in history rather than only within the page being displayed;
    `truncated: true` reports that the scan hit its byte/file budget.
    `level`/`phase`/`source`/`experiment_id`/`mailbox_task_id`/`job_id`/
    `session_id` narrow the feed, and `cursor` pages further back.
  - `GET /logs/public` — backwards-compatible alias of `GET /logs`,
    returning the identical complete entries (the former reduced
    allowlist is gone).
  - `GET /logs/trace/{reference}` (no auth) — everything Nexus did for
    one order. `reference` may be an inbox message filename, an
    experiment id, a mailbox task id, a controller session or chat job
    id, or any distinctive fragment; `modules/log_trace.py` finds the
    entries that reference identifies, reads the correlation ids off
    them, and expands until the whole chain (mailbox intake → approval →
    controller → implementation → verification → result) is returned,
    with a `summary` derived from those same entries. An unresolvable
    reference returns `found: false`, not a 404.
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
- Retention (`modules/mailbox/retention.py`, Issue #129): each poll deletes
  outbox responses older than `NEXUS_MAILBOX_RETENTION_DAYS` (default 5,
  `0` disables; an unparsable value also disables rather than guessing),
  and drops inbox messages whose record shows they exhausted `MAX_RETRY` —
  those are terminal and were otherwise re-read and re-skipped on every
  poll forever. A message's reason for removal is logged first, so
  `GET /logs/trace/{reference}` still explains it after the file is gone.
  Pruning runs *after* `retry_pending_git_commits()` and never deletes a
  file that is still needed: anything referenced by a `mailbox_messages`
  row that is not `done`, or by an `operation_chains`/
  `mailbox_task_approvals` row whose `publish_status` is not `pushed`, is
  protected — deleting it would strand that record permanently. That
  constraint is why retention lives in the processor (which holds the
  SQLite state) rather than in an external cron or `git rm` job. Deletions
  are committed with explicit pathspecs and pushed by the poll's existing
  fetch/rebase/push sync; a poll with nothing to prune makes no commit.
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

### 8) Adoption restart recovery

- Modules: `modules/experiment_adoption_service.py` (adoption itself),
  `modules/task_finalization.py` (the late lifecycle around it:
  `merging → deploying → restart_requested → validating_live → succeeded`)
- A process restart is an expected boundary, not a failure — the refresh
  flow restarts Nexus deliberately — so `recover_after_restart()`
  reconstructs an adoption caught in `queued`/`adopting` from durable
  evidence instead of marking it `interrupted` (Issue #125; that status
  previously propagated into a misleading `needs_review`):
  - GitHub already reports the pull request merged → recorded as
    `adopted` with the real merge commit. `merge_and_complete()` is
    idempotent for the same reason: it checks `merged_commit_sha()`
    first, so a resumed adoption can never merge twice.
  - Otherwise the **same** adoption record is resumed — same adoption id,
    experiment, issue, PR and branch; no new record is created. The
    pipeline re-derives its state from git, and `experiment_commit_sha`
    is persisted the moment the commit exists so a restart after that
    step resumes from it rather than failing on a now-clean worktree.
  - `recovery_attempts` (additive column) bounds resumes at
    `_MAX_RECOVERY_ATTEMPTS`; beyond that, or when the experiment/worktree
    is genuinely gone, the adoption becomes `needs_review` naming the
    actual obstacle.
  - Adoptions are serialized by design, so if several are found stuck at
    once only the oldest is resumed and the rest are flagged for review
    rather than left to fight over the adoption lock.

### 9) Initiative tick (self-directed work selection)

- Modules: `modules/nexus_initiative.py`, CLI entry point `nexus_tick.py`,
  units `deploy/nexus-tick.{service,timer}` (every 15 min)
- This is the step that makes Nexus self-directed rather than purely
  reactive. Every other entry point waits to be asked; this one inspects
  Nexus's own state and starts one piece of work.
- Per tick: refuse if anything is genuinely running → refuse if the daily
  budget is spent → collect dispatchable candidates → take the first →
  persist the decision → dispatch. Persisting *before* dispatch means a
  crash mid-dispatch leaves a record of intent, not a silent gap.
- Priority: `integration_backlog` (succeeded experiments whose adoption is
  `not_started`/`needs_review`/`interrupted`, dispatched to
  `ExperimentAdoptionService.start_adoption`) → `open_issue` (open GitHub
  issues labelled `nexus`, via `NexusControllerService.handle_issue`) →
  `new_idea` (IDEAS.md entries in state `new`, via `handle_idea`).
- Bounds: one item per tick; `NEXUS_INITIATIVE_DAILY_BUDGET` (default 8) per
  24h; issues require the `nexus` label. `--integration-only` builds no LLM
  controller at all and drains only already-verified work.
- Staleness: a row in a running state counts as busy only while it is newer
  than `NEXUS_INITIATIVE_STALE_AFTER_SECONDS` (default 2h). Older rows are
  reported through `abandoned_work()` and otherwise ignored — see D-017.
- Audit: `nexus_initiative_log` records every tick (decision, chosen source,
  reason, what else was considered, failure detail), readable via
  `NexusInitiativeService.history()`.

### 10) Work heartbeats (explicit liveness)

- Modules: `modules/heartbeat.py` (shared semantics), persistence in
  `modules/nexus_task_store.py`, refreshed by `modules/task_orchestrator.py`
- Answers "is this work still owned by a live worker?" without overloading
  `updated_at`. A controller turn claims its session (`worker_id`,
  `claimed_at`), a background thread refreshes `last_heartbeat_at` every
  `NEXUS_HEARTBEAT_INTERVAL_SECONDS` (default 30) while the turn runs, and
  the turn releases the claim with an explicit `terminal_reason` when it
  ends.
- A heartbeat updates `last_heartbeat_at` only. `updated_at` keeps meaning
  "the session changed", so a heartbeat can never disguise a dead turn as
  recent activity.
- No claim, no turn: if claiming raises or finds nothing to claim, the turn
  fails with a `TaskOrchestratorError` before the heartbeat starts and before
  anything is written. Proceeding unclaimed would make every later write fall
  back to "no owner" and switch the ownership rules off exactly when
  persistence is already misbehaving.
- Every write a claimed turn makes is scoped to its own `worker_id`: status
  changes, the final decision and the release all miss if the session has
  since been taken over, so a superseded worker cannot drive or end a session
  it no longer owns. The terminal write is one transaction
  (`NexusTaskStore.finalize_turn`) rather than three, so a turn can never end
  half in one worker's world and half in another's.
- A turn that raises or is cancelled records that: `failed` with
  `terminal_reason=turn_failed`/`turn_cancelled`. Only a worker that vanished
  without a chance to say anything (a killed process) falls to the reaper, so
  `heartbeat_timeout` keeps meaning "nobody ever told us".
- Stale claims: a claim quiet for longer than
  `NEXUS_STALE_HEARTBEAT_SECONDS` (default 15 min, clamped to a safe floor)
  is reconciled to `abandoned` with `terminal_reason=heartbeat_timeout`, once
  - reconciliation clears the heartbeat it selects on, so repeating it is a
  no-op. Run at API startup and on every `nexus_tick`. It never resumes the
  work. Selection and reconciliation are separate statements in separate
  processes, so the write re-checks the whole predicate - working status, the
  same owner, the exact heartbeat that was selected, still past the cutoff -
  and does nothing if the worker beat or the session was taken over in
  between.
- Resting states (`waiting_for_user`, `blocked_missing_capability`) have no
  owner by design and are never reaped. `waiting_for_llm` is not resting: the
  worker is blocked on its own inference job and keeps beating.
- Sessions that never claimed (rows predating this, or a caller that does not
  claim) still fall back to the six-hour `updated_at` reaper from #160, and
  are counted separately so the gap stays visible.
- Inspection: `GET /diagnostics/nexus/liveness` (API key) reports every
  claim with its owner, heartbeat age and staleness, plus status counts. It
  reports only - it never reaps.

### 11) Local-model execution (one shared job path)

- Modules: `modules/local_llm_jobs.py` (queue, claim, worker, persistence,
  liveness), callers `modules/task_orchestrator.py` (roles `controller`,
  `tool_resolver`), `modules/nexus_delegation.py` (role `local_worker`) and
  `modules/failure_analysis_service.py` (role `failure_analysis`).
- Every feature that needs the local model submits a job and awaits it.
  Nobody calls llama-server directly: `LlamaCppClient` is the transport the
  job worker uses, not an interface features wire up themselves. External
  delegates (`codex_readonly`, `azure_openai`) are a different resource and
  keep their own paths.
- One service per database per process
  (`acquire_local_llm_job_service`/`release_local_llm_job_service`), so
  features in one process share a worker. Callers that inject their own
  client (tests) get an isolated worker instead of silently sharing someone
  else's.
- Serialization is enforced across processes, in the database, because Nexus
  runs the controller (`nexus_tick`) and the API as separate processes
  against the same `local_llm_jobs` table. A worker executes a job only after
  claiming the single inference slot in one `BEGIN IMMEDIATE` transaction:
  release dead claims, refuse if a live claim exists, otherwise take the
  oldest queued job. Queued work is visible to every worker, so a job
  submitted by a process that then exits is still executed.
- A claim states its own liveness (`worker_id`, `lease_expires_at`, refreshed
  by `modules/heartbeat.py` while the request is in flight), per D-018. A
  claim whose lease lapsed belonged to a dead process and is released with
  `error_kind=interrupted` - by any worker, at startup or during a sweep -
  so one crash cannot hold the inference slot. A startup never touches a
  claim that is still being refreshed: that one belongs to a live process.
- Jobs are rows in `local_llm_jobs`: `role`, status, prompt/system,
  `created_at`/`started_at`/`finished_at`, result or `error` +
  `error_kind`, and the session they belong to. `role` is observability
  only - it never changes how a job runs - so one feature's local-model
  traffic can be read back without filtering another's.
- No caller-side deadline. A waiter blocks for as long as the job legitimately
  takes; the service's own watchdog
  (`NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS`, default 1h, clamped 5m-2h) is the
  HTTP read timeout, and a `running` job that outlives it by the grace window
  is swept to `failed` with `error_kind=stuck`, which releases its waiter.
- Terminal writes are guarded by status *and* ownership: a job cancelled
  (`cancel()`), swept as stuck, or reclaimed from a lapsed lease while its
  request was still in flight stays terminal, and the late result is
  discarded rather than resurrecting it.
- A caller that cannot get a result degrades in its own terms - failure
  analysis persists exactly one `analysis_unavailable` record - and never
  re-enters itself over its own failed job.

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
