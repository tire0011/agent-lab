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
  - `GET /diagnostics/status/{reference}`, `GET /diagnostics/logs/{reference}`,
    `GET /diagnostics/logs/service/{service}` and
    `GET /diagnostics/logs/last-failure` (API key) — first-class diagnostic
    commands (#188). Where `/logs` and `/logs/trace` return entries, these
    join the lifecycle state onto them: `modules/nexus_diagnostics.py`
    resolves a task/experiment/session id, a unique prefix, an issue or PR
    number, or an inbox filename, reads the owning subsystem's row, and
    returns lifecycle, branch, commit, PR, worker, last error and a
    running/interrupted/completed progress verdict alongside bounded logs.
    Read-only by construction: the database is opened `mode=ro` with
    `PRAGMA query_only` — deliberately not through `run_store.init_db`, which
    is the write path's entry point and would have made a diagnostic read
    apply migrations and backfills to the database it was asked to describe.
    That is what makes it safe to expose through the mailbox/chat catalog as
    `nexus_status`/`nexus_logs`/`nexus_last_failure`; `modules/failure_analysis_service.py` reads the same
    backend rather than a log lookup of its own.
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
    the existing `modules.codex_task_service.CodexTaskService`. The path is
    historical: a task created here runs on whichever coding agent was drawn
    for it (section 13), and the response says which one in `provider`; no worker
    logic duplicated in `api.py`. `working_dir` (`/agent`) and `sandbox`
    (`read-only`) are fixed server-side; `timeout_seconds` is capped at
    120s. Synchronous only — no queue, no parallel execution, no chat
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
- Mailbox repository: the checkout configured by `AGENT_MAILBOX_REPO_PATH`
- Inbox: root-level `inbox/` — Markdown files with YAML front matter
- Outbox: root-level `outbox/` — response files written per processed message
- Message types: `idea` (appended to IDEAS.md), `task` (body stored, `queued` record created in `mailbox_task_approvals` and handed to Nexus), `question` (acknowledged), `context` (stored in `knowledge` table)
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
- Communication PR notification (`modules/outbox_pr_notifier.py`, Issue #196):
  once an outbox message is pushed, `OutboxPublisher` hands it to the
  notifier, which posts one top-level comment carrying the `[NEXUS-OUTBOX]`
  marker, the message id, its timestamp, and the content to the persistent
  *Nexus communication* PR (#194, override with `NEXUS_COMMUNICATION_PR`).
  That comment is what wakes the external ChatGPT Work trigger. Delivery is
  keyed by message id in `outbox_pr_notifications`, so reprocessing never
  comments twice, and a GitHub failure only leaves the notification
  `pending` — the outbox entry itself is already committed and pushed. The
  notification row is written *before* the push, not after: a record that
  reaches `pushed` is never selected by `fetch_pending()` again, so a crash
  in that window would otherwise strand the message with no row for the
  retry sweep to find. The cost is that a message whose publish then fails
  is still announced, from content the producer had already committed to
  SQLite, while the file itself is retried by the next poll. The poll
  retries pending notifications with bounded exponential backoff. A
  closed PR is reopened (recreating its head branch first if GitHub deleted
  it); only a genuinely unusable PR causes an equivalent channel to be
  recreated — on a freshly cut branch with its own commit, because after a
  merge the old branch is identical to base and GitHub refuses a PR with no
  commits between them — which is logged at ERROR with the new number and URL because
  the external trigger is still scoped to #194. The PR is never merged from
  here.
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

### 7) Mailbox task bridge

- Module: `modules/mailbox/task_service.py`
- Table: `mailbox_task_approvals` in SQLite (name kept from the removed approval step, #264)
- State machine: `queued → submitted → running → succeeded / failed / needs_review`
- No approval step: a queued task is handed to the local Nexus controller, which chooses the action
- For `new_experiment`, `ExperimentService.create_experiment` + `run_experiment` run in a daemon thread
- `MailboxTaskWorker` daemon polls every 10 s (`submit_queued` + `poll_running`)
- Legacy `pending_approval`/`rejected` rows stay readable and are never run; `approved` rows were migrated to `queued`
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
  `ExperimentAdoptionService.start_adoption`) → `open_issue` (ordinary open
  GitHub issues not already linked to work, via
  `NexusControllerService.handle_issue`) → `new_idea` (IDEAS.md entries in
  state `new`, via `handle_idea`).
- Bounds: one item per tick; `NEXUS_INITIATIVE_DAILY_BUDGET` (default 5) per
  rolling 24h. `--integration-only` remains available for manual recovery and
  builds no LLM controller at all.
- Staleness: a row in a running state counts as busy only while it is newer
  than `NEXUS_INITIATIVE_STALE_AFTER_SECONDS` (default 2h). Older rows are
  reported through `abandoned_work()` and otherwise ignored — see D-017.
- Audit: `nexus_initiative_log` records every tick (decision, chosen source,
  reason, what else was considered, failure detail), readable via
  `NexusInitiativeService.history()`. An open issue out of attempts is logged
  as `skipped_attempts_exhausted` once per set of experiments, gets one GitHub
  comment and is listed in the planning prompt, but stays unselectable.
  A pipeline crash is not an attempt (#467): after two crashes the issue is
  reported the same way, but returns once the local `origin/main` ref has a
  commit newer than the last crash. Crashes stay in the daily budget.

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
- Extended to work that has no heartbeat of its own by
  `modules/nexus_diagnostics.py` (#188). Mailbox tasks and experiments are
  driven through several processes and phases, so no single owner is alive
  across the span and there is nothing to beat - but "is this still running?"
  is just as real a question there. Their liveness is derived from the newest
  *progress signal*: the claiming session's heartbeat where one exists,
  otherwise the newest structured log entry, otherwise the row's
  `updated_at`. The staleness rule is `heartbeat.resolve_stale_timeout`
  unchanged, floor and all, and the verdict reports which of the three
  sources answered - "a worker stopped beating 40 minutes ago" and "the last
  log line is 40 minutes old" are different strengths of the same claim.
- Local verification tests carry that contract into the one place it was
  missing (#209). Modules: `modules/test_progress.py` (parse the verbose
  `unittest` stream into per-test events and a heartbeat),
  `modules/test_run_log.py` (the dedicated per-test log, the heartbeat file,
  and the bridge into the global feed), `modules/verification_runner.py` (the
  watchdog). The test process's own progress - which test is running, how
  many have finished, when anything last happened - *is* its heartbeat: the
  watchdog wakes every `NEXUS_TEST_WATCHDOG_INTERVAL_SECONDS`, publishes one
  summary entry into the experiment's log (so `assess_progress` above sees a
  live subject), and aborts the run only when progress stops for
  `NEXUS_TEST_STALL_SECONDS` - never merely because the suite has been
  running a while. A fixed wall-clock timeout could not tell a healthy slow
  suite from a stuck test, which is how experiment
  `df97d67bc08e45b4bc5c573db0c84d85` became a `timed_out` with no exit code
  and nothing to diagnose. `NEXUS_TEST_SAFETY_CAP_SECONDS` survives as a
  final guard against an orphaned process only.
- Per-test detail (two entries per test, timestamps, result and duration)
  goes to `<AGENT_LOG_DIR>/test-runs/`, deliberately a subdirectory: the
  unified feed globs `*.jsonl` in the log directory itself and does not
  recurse, so ~3800 entries per run cannot drown the feed an operator reads.

### 11) Local-model execution (one shared job path)

- Modules: `modules/local_llm_jobs.py` (queue, claim, worker, persistence,
  liveness), callers `modules/task_orchestrator.py` (roles `controller`,
  `tool_resolver`), `modules/nexus_delegation.py` (role `local_worker`) and
  `modules/failure_analysis_service.py` (role `failure_analysis`).
- Every feature that needs the local model submits a job and awaits it.
  Nobody calls llama-server directly: `LlamaCppClient` is the transport the
  job worker uses, not an interface features wire up themselves. External
  delegates (`codex_readonly`, `claude_readonly`, `azure_openai`) are a
  different resource and keep their own paths.
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
- No caller-side deadline. A waiter blocks until its job is terminal; the
  service's own watchdog (`NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS`, default 15m,
  clamped 1m-2h) is one hard deadline per model call, for every role. A call
  that reaches it is cancelled and fails with `error_kind=timeout` (#236),
  and a `running` job that outlives it by the grace window (a wedged worker)
  is swept to `failed` with `error_kind=stuck`. Either way the waiter is
  released; the controller then uses its external-provider fallback.
- Terminal writes are guarded by status *and* ownership: a job cancelled
  (`cancel()`), swept as stuck, or reclaimed from a lapsed lease while its
  request was still in flight stays terminal, and the late result is
  discarded rather than resurrecting it.
- A caller that cannot get a result degrades in its own terms - failure
  analysis persists exactly one `analysis_unavailable` record - and never
  re-enters itself over its own failed job.

### 12) Tool discovery and execution (one shared router, several roles)

- Modules: `modules/nexus_tool_router.py` (discovery, read-only execution,
  grants, not-found metadata), backed by `modules/operations_registry.py`
  (the capability catalog, argument validation, redaction, audit log) and
  `modules/tool_loop_guard.py` (duplicate/non-progressing protection).
  Callers: `modules/task_orchestrator.py` (the persistent controller) and
  `modules/failure_analysis_service.py` (diagnosis, #172).
  `modules/tool_router_mcp.py` exposes the same router to external MCP clients
  over stdio, read-only (#118, `docs/issue-118-tool-router-mcp.md`).
- `find_tool` resolves one plain-language capability request against the
  read-only catalog and returns that operation's schema; `call_tool` runs an
  operation only when a `find_tool` result in the same session already
  returned its schema. Both rules apply to every role - a role never guesses
  a tool name, and never runs one it did not resolve.
- Every JSON schema Nexus sends a model goes in the OpenAI envelope,
  `response_format.json_schema.schema`, built by
  `llama_cpp_client.json_schema_response_format`. It used to sit one level
  higher, where neither llama-server nor Azure OpenAI looks, so every schema
  was silently ignored and every declared enum was only a suggestion - which
  is how the resolver answered with a `status` outside its four values and had
  a correct selection discarded (#218). An ignored schema is invisible until a
  model stops complying, so the shape is pinned by a test and checkable at
  runtime: `local_model_schema_check` is a read-only capability that asks the
  server whether it applies Nexus's own schema, using a schema the answer
  cannot satisfy by accident plus an unconstrained control request - without
  which a pass would be coincidence rather than evidence.
- A terminal `not_found` is an answer, not a dead end (#218). It carries
  `related_operations` as descriptors built from the registry's own
  `OperationSpec` - operation, description, why it was offered, argument
  schema - plus `direct_capabilities` naming the controller action that owns
  the goal when `find_tool` structurally cannot (`command`, `python_run`),
  a `recommended_next_action`, and `guidance`. Bounded to the best three,
  because the reader is a small local model working inside an
  already-budgeted prompt.
- A goal asking to *run* something is recommended the action that owns it
  ahead of any related operation. The scorer produces candidates for such a
  goal anyway, and answering "run the test suite" with `git_status` would be
  right-looking evidence about the wrong subject.
- Only when resolving a related operation really is the recommendation does
  the response carry `recommended_operation`, and only then is a repeat of
  the identical dead goal redirected - to a fresh lookup naming that
  operation, rather than refused. The redirect is recorded as a
  `find_tool_redirected` event and stays an ordinary `find_tool` call, so
  the loop guard counts the redirected lookup like any other call and still
  ends a session that has run out of alternatives; the dead goal's own
  repeat is durably recorded as that event rather than as a duplicate.
  A redirect never targets the goal it came from, and the guard's verdict on
  a refused redirect still carries that lookup's own `not_found` guidance.
- Roles differ only in permission. The controller additionally has
  `command`, `python_run`, `memory_search` and `delegate`; failure analysis
  has discovery and read-only execution and nothing else, so a diagnosis
  cannot transition lifecycle state, edit code, merge, restart or retry.
- What counts as read-only is declared by each capability (`mutates_state` on
  its `OperationSpec`, required and keyword-only) and surfaced in
  `GET /operations/catalog`. The router filters on that declaration, so an
  operation added later is outside a read-only role's surface until it
  declares itself read-only - no denylist to keep in step with the catalog.
  A spec that declares nothing is treated as mutating.
- Inference and grant storage are injected, because they are genuinely
  per-role: the controller resolves local-first (memory pressure is only
  logged, see D-027) with an external fallback after a real local failure and reads its grants back from persistent session events; failure
  analysis queues a job under section 11 and keeps grants for the one
  analysis they belong to.
- Diagnosis runs a bounded evidence-gathering phase *before* it diagnoses:
  a recorded failure reason is a claim about the past, and a runtime or
  configuration claim ("GITHUB_TOKEN is unavailable") is checked against
  current state rather than repeated. `github_status` is the read-only
  capability for that specific claim - it reports whether a repository and a
  credential resolve right now, never their values.
- Bounds are the controller's, reused: a small step budget, the same loop
  guard, and every tool result redacted and length-capped before it reaches
  a prompt, a log, the persisted analysis or the outbox. No available
  capability for what was asked is a valid outcome, and the diagnosis states
  the missing evidence instead of assuming either answer.

### 13) Coding agents (one contract, persistent rotation)

- Modules: `modules/coding_worker.py` (shared contract), `modules/codex_worker.py`
  (`CodexWorker`), `modules/claude_worker.py` (`ClaudeWorker`),
  `modules/coding_provider.py` (the choice). Callers:
  `modules/codex_task_service.py` (tasks and the `/codex/tasks` API),
  `modules/experiment_service/service.py` (implementation + repair),
  `modules/nexus_delegation.py` (named read-only consultation).
- One contract, two CLIs. A worker takes a task, a working directory, a sandbox
  mode and a timeout, runs its CLI as a subprocess (never `shell=True`), and
  returns a `WorkerResult`: success, exit code, capped stdout/stderr with
  truncation flags, runtime, session/thread id, final message, token usage,
  error — and `provider`, the agent that produced it. Callers never branch on
  which agent they got.
- Sandbox modes mean the same thing for both agents: `read-only` (may inspect
  the working directory, cannot change it), `workspace-write` (may change files
  inside it — used for the experiment worktree), `danger-full-access` (never a
  default). Codex gets these from its own sandbox flags; Claude gets them from
  its tool set, since a non-interactive run has nobody to answer a permission
  prompt: in read-only mode no write or shell tool exists at all, and anything
  that would prompt is denied instead of waiting.
- Neither agent is preferred. The provider is selected in persistent round-robin order per
  unit of work, narrowed first by `AGENT_CODING_PROVIDER` (pin),
  `AGENT_CODING_PROVIDERS` (allow list) and by which CLI is actually installed
  (see D-022). Tasks select at creation; experiments select once per worktree, so
  an implementation and its repair iterations stay on one agent.
- A caller that names an agent gets that agent: the `codex_readonly` /
  `claude_readonly` delegates and the router's `codex` provider pin their
  choice, and an injected worker pins the whole service (tests,
  single-provider deployments).
- The result is recorded where it can be read back: `codex_tasks.provider`,
  `experiments.implementation_provider`, and a `runs` audit row per call
  (Claude's carries the CLI's reported cost as `actual_cost`). Rows from
  before the second agent existed read back as `codex`.
- Environment: `AGENT_CODEX_BIN`, `AGENT_CLAUDE_BIN`, `AGENT_CLAUDE_MODEL`,
  `AGENT_CODING_PROVIDER`, `AGENT_CODING_PROVIDERS`. Neither worker ever
  receives the full environment — only what its CLI needs, so `AGENT_API_KEY`
  and other application secrets stay out of the subprocess.

## Near-term target architecture (planned, not complete)

1. **V1 MCP agent path**
   - Expand practical MCP usage while keeping capability boundaries explicit.
2. **Persistent storage**
   - Keep SQLite-backed structured run store and evolve reporting/migrations as needed.
3. **Benchmark harness**
   - Reproducible V0 vs V1 comparison pipeline. For `new_experiment` this exists: see `benchmarks/README.md`.
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
- The experiment benchmark (`benchmarks/`, #343) has no stored baseline yet.
- No finalized policy/eval framework for autonomous self-modification yet.
- External ChatGPT-to-endpoint integration is not yet dependable.
