# DECISIONS.md

Decision log for architecture and operating choices.

## Accepted

### D-001: Start with a minimal measurable baseline (V0)

- Status: Accepted
- Why: Early measurement and iteration are more valuable than broad up-front architecture.
- Evidence: `agent.py` exists and logs per-run metrics.

### D-002: Use Azure OpenAI deployment `gpt-4o` as initial baseline

- Status: Accepted
- Why: Already available and tested in current subscription/resource setup.
- Evidence: API connectivity confirmed; deployment details recorded in repo docs.

### D-003: Prefer MCP servers/SDKs over custom orchestration where possible

- Status: Accepted
- Why: Reduces custom complexity and improves maintainability.
- Evidence: V1 direction and current MCP filesystem integration in `agent.py`.

### D-004: Keep initial persistence local and low cost (SQLite first)

- Status: Accepted
- Why: Budget constraints and fast iteration; avoid paid DB before clear need.

### D-005: Keep `/health` public, protect sensitive API routes

- Status: Accepted
- Why: Operational monitoring needs a simple public liveness check; protected routes require API key auth.
- Evidence: `api.py` exposes `/health` and protects `/secure-health`.

### D-006: Security baseline - never store secrets in repo

- Status: Accepted
- Why: Prevent credential leakage and reduce operational risk.

### D-010: Store runtime secrets outside `/agent`

- Status: Accepted
- Why: The MCP filesystem server is intentionally limited to `/agent`; storing secrets in `/etc/agent-lab/agent.env` prevents tool-mediated exposure of runtime credentials/config.
- Evidence: `run-agent.sh` sources `/etc/agent-lab/agent.env` and keeps `AGENT_FS_ROOT` constrained to `/agent`.

### D-011: V2 run persistence uses SQLite with table-driven estimated pricing

- Status: Accepted
- Why: Queryable structured run history is required for controlled experiments; table-driven pricing enables historical model-rate changes without hard-coding prices in calculation logic.
- Scope: SQLite at `/agent/state/agent.db` with `runs`, `model_prices`, `tool_calls`, and `knowledge`; estimated cost is recorded per run, while actual Azure reconciliation is deferred to V2.1.

### D-012: Log reading is open - no API key, no reduced allowlist

- Status: Accepted
- Why: Nexus is a single-operator experiment on a dedicated VM, and
  analysing what it did is the entire purpose of the log surface. Making
  every analysis step depend on entering a key - and then seeing only an
  allowlisted subset without command/stdout/stderr/exception detail -
  made the logs materially less useful for the one job they exist for.
- Scope: `GET /logs`, `GET /logs/public` (now an alias returning the same
  complete entries) and `GET /logs/trace/{reference}` require no
  authentication. **Write endpoints are unchanged and still require
  `x-api-key`**, including the shell-backed `/operations/*` routes.
- Risk accepted: the API is reachable over the public internet, so log
  content is world-readable. Redaction still applies on both the write
  and the read path.
- Mitigation: because a leaked `AGENT_API_KEY` would turn open *read*
  access into *write* access, `modules.redaction` additionally scrubs the
  literal values of `AGENT_API_KEY`, `AGENT_GITHUB_WEBHOOK_SECRET`,
  `GITHUB_TOKEN` and `AZURE_OPENAI_API_KEY` wherever they appear - not
  only where a keyword-shaped pattern happens to introduce them.
- Revisit if: the VM stops being single-operator/experimental, or log
  content starts carrying third-party data.

### D-013: A persisted adoption failure is evidence, not a permanent verdict

- Status: Accepted
- Why: Issue #136. A `failed` adoption row can record two very different
  things: that the tests *ran and rejected* the change, or that the local
  run never reached a verdict at all (it timed out, git failed, the
  worktree could not be built). Only the first is a statement about the
  implementation. Treating both as permanent meant a lifecycle fix could
  never reach the experiments it was written for: after #134 removed the
  local full suite as a mandatory gate, experiment `3946adc3` still ended
  in `needs_review` on every retry because its 2026-09-06 timeout row was
  reused instead of re-evaluated.
- Rule: `test_exit_code` set to a non-zero value is a verdict and stays
  terminal; no later evidence overrules it. An unset `test_exit_code`
  means no verdict was reached, and that failure is superseded only when
  authoritative GitHub CI for the *exact* verified checkpoint is green
  now - the same evidence adoption itself accepts (#134). The decision is
  never made by matching words in the failure text.
- Consequence: a genuinely broken implementation is never retried, because
  its CI is red or missing. Every retry reuses the same adoption row and
  appends the failure it replaced to `experiment_adoption_failures`.
- Budget: `stale_retry_attempts` (max 2) bounds only how often *changed
  evidence* may reopen an exhausted lifecycle, so only a restart the
  caller made *because* of a successful assessment spends it. Ordinary
  retries - a finalization retry inside its own budget, an operator
  pressing adopt - leave it untouched; otherwise two ordinary retries
  would exhaust the evidence budget before any evidence had changed and
  this decision would be unreachable in exactly the case it exists for.
  It is separate again from #125's restart-recovery budget, which bounds a
  third thing (crashes mid-pipeline).
- Revisit if: adoption gains other gates whose failures leave no exit
  code, or if CI stops being the authoritative verification source.

### D-014: An accepted CI verdict is a durable fact, not a live lookup

- Status: Accepted
- Why: Issue #138. D-013 made the retry depend on exact-commit CI, but
  the evidence was re-derived with a live GitHub call every time. On the
  real #115/#116 retry that call returned `GitHub CI identity
  unavailable: configure GITHUB_TOKEN/GH_TOKEN`, so evidence Nexus had
  already accepted for commit `28399a0a` (run `33898643539`, success)
  simply vanished and the adoption stayed stuck at `no_ci_evidence`.
  Restart-safety (#125/#128) is worthless if a lifecycle fact only exists
  while the process that observed it is alive and credentialed.
- Rule: when a GitHub Actions run *completes*, its conclusion for that
  exact commit is written to `github_ci_verifications`, keyed by commit
  SHA. `wait_for_ci` does the writing, so every caller - the verification
  path included - populates the store. Only `passed`/`failed` are stored;
  `unavailable` says nothing about the commit and caching it would let an
  outage masquerade as evidence. No token or secret is persisted.
- The base is part of the question. #134 only calls exact-checkpoint CI
  authoritative while the checkpoint's parent is still the target head,
  because cherry-picking the same checkpoint onto a moved base produces a
  tree CI never saw. So the base is stored *with* the verdict, and a
  stored verdict is reused only when it matches the base this adoption
  will actually integrate onto - resolved from the local
  `origin/<target>` ref, which needs no credentials and is the base
  adoption really uses. A row with no recorded base cannot prove the rule
  and is not reused. Otherwise a green verdict cached while `main=A`
  would survive `main` advancing to `B` and skip the integration
  verification that case requires.
- Precedence: the most recent authoritative observation for a checkpoint
  SHA, taken on that same base, wins. A live lookup runs only when no
  such verdict is stored, and whatever it finds is written through, so a
  re-run that flips the conclusion supersedes the old record. An
  implementation replaced through the normal lifecycle gets a *new*
  checkpoint SHA, which has no record and therefore goes live - that, not
  cache expiry, is how new work gets re-verified.
- Consequence: a green verdict is never reused for another commit (the
  lookup is an exact-SHA match, so there is simply no row to find) nor on
  another base, and a red verdict stays terminal.
- Revisit if: verdicts ever need to expire, or a second CI provider is
  added - `provider` is stored so the store can tell them apart.

### D-015: The CI-evidence decision path is a named event vocabulary

- Status: Accepted
- Why: Issue #141. D-013 and D-014 made adoption retries turn on evidence,
  but the logs only showed the outcome. The real #115/#116 retry surfaced
  nothing but `finalization retries exhausted after adoption status=failed:
  Die Integrationstests konnten nicht erfolgreich abgeschlossen werden.` -
  which reads like a failing test suite, when the actual cause was a
  missing durable row, a live lookup, and no credentials to make it. That
  took SQLite queries and hand-called internals to find. A decision the
  system makes on evidence has to be readable from the evidence it used.
- The events, which are a stable interface and not free text:
  `ci_evidence_lookup` (was anything stored for this checkpoint),
  `ci_evidence_reused` (a durable verdict was accepted, with the run and
  base it came from), `ci_evidence_rejected` (evidence exists but cannot
  support this integration, with `rejection_reason` one of
  `target_base_moved`, `incomplete_base_evidence`, `checkpoint_mismatch`,
  `red_verdict`), `ci_evidence_live_fallback` and
  `ci_evidence_live_result` (why GitHub was asked and what it said - this
  is where a credential problem reads as itself),
  `stale_adoption_assessment`, `stale_adoption_requeued` and
  `finalization_blocked`.
- Accepted and rejected are exclusive. `ci_evidence_reused` means a
  verdict that can actually support the retry, so a *red* stored verdict
  is loaded the same way but reported only as `red_verdict` - logging one
  piece of evidence as both would defeat the point of a vocabulary meant
  for automated diagnosis. `red_verdict` is emitted once, where the
  verdict is resolved, and carries `source` (`persisted` or `live`) so it
  reads the same whichever path produced it; consumers never repeat it.
- One decision, one event. `assess_failure` logs its verdict once at the
  boundary rather than at each of its six return paths; the evidence
  events beneath it come from `_exact_commit_ci`. That method runs twice
  per retry - once to assess, once inside the pipeline - so its events
  carry the calling `phase` (`assessment` vs `adoption`) to tell the two
  passes apart rather than being suppressed.
- Detail belongs in structured fields, never in prose. That is also what
  keeps secrets out: `ExperimentLogger` redacts every string field through
  `_bounded_text`, so nothing hand-rolled is needed and nothing bypasses
  it.
- Revisit if: an automated diagnosis consumer appears - it would make
  these names a versioned contract rather than a convention.

### D-016: Adoption judges content, not worktree layout

- Status: Accepted
- Why: Experiment `3946adc3` was implemented, verified, and green in GitHub
  Actions for commit `28399a0a` on 2026-09-04 - and still ended in
  `needs_review` on eight consecutive retries between 2026-09-06 and
  2026-09-10. `_validate_startable` compared the *text* of `git status
  --short` and `git diff --stat` against what verification had stored.
  Adoption's own first step commits the verified changes, which turns the
  dirty worktree clean and makes those strings differ. The precondition the
  retry checked was destroyed by the attempt that preceded it, so the retry
  could never converge. Tree comparison proved the content was
  byte-identical to what CI verified the whole time.
- Rule: when the stored status/stat text no longer matches, that is not a
  verdict. Adoption then asks the question the text was only approximating:
  is the tree that would be integrated identical to the tree of the verified
  checkpoint? `_capture_snapshot` already builds a scratch index holding HEAD
  plus every worktree change, so its `write-tree` *is* the content adoption
  would integrate. Equal trees continue; anything else keeps the old refusal.
- This is stricter, not looser. A tree hash covers every byte of every file;
  `git status` text covers which paths changed. Content CI never saw cannot
  pass under either arrangement, which is what the accompanying regressions
  pin down.
- The fallback matters: with no checkpoint SHA recorded there is nothing
  authoritative to compare against, so the conservative text comparison still
  applies rather than defaulting to permissive.
- Consequence: D-013 through D-015 reasoned carefully about CI evidence, but
  no evidence rule could ever have unblocked this - the guard aborted one
  layer above, before any CI logic was consulted. The misleading message it
  produced ("Die Integrationstests konnten nicht erfolgreich abgeschlossen
  werden") is what kept the diagnosis pointed at the wrong layer.
- Revisit if: adoption ever integrates something other than the worktree's
  full tree (a partial or path-filtered adoption would need its own
  invariant).

### D-017: Nexus selects its own work, one item at a time

- Status: Accepted
- Why: Every entry point was reactive - the mailbox timer waits for a human
  to drop a file, `/chat` waits for typing, `MailboxTaskWorker` only moves
  what a human already approved. Nothing inspected Nexus's own state and
  decided what to do. `NexusControllerService` was written for exactly this
  and was instantiated only in tests. Autonomy was not blocked by a bug; the
  component that starts work had never been connected.
- Rule: `modules/nexus_initiative.py` picks exactly one item per tick,
  persists the decision *before* dispatching, and hands it to the pipeline
  that already exists. It adds no lifecycle stage of its own.
- Priority order is deliberate: unintegrated verified work first, then open
  issues not already linked to work, then `new` ideas. Finishing started work is cheaper
  and more valuable than starting new work, and the live database held
  **seven** succeeded experiments that were never integrated - the dominant
  failure mode, not an edge case.
- Running states are only trusted while fresh. The live database holds an
  experiment queued since 2026-08-14 and six mailbox tasks at `running`,
  untouched since 2026-09-02 at the latest. A busy check that believed them
  would never run again, so past `NEXUS_INITIATIVE_STALE_AFTER_SECONDS` a row
  is treated as abandoned and reported rather than obeyed. Cleanup is left to
  the services that own that state.
- Bounds: one item per tick, a no-op while anything is genuinely running, and
  `NEXUS_INITIATIVE_DAILY_BUDGET` (default 5) self-started items per rolling
  24h. Open issues do not require a human-applied label: the daily budget,
  active-work gate, and persisted issue links bound autonomous dispatch while
  allowing Nexus to plan its own backlog.
- Only dispatchable work is listed. Under `--integration-only` no controller
  exists, so ideas and issues are omitted from the backlog entirely rather
  than reported as work that cannot be started.
- Revisit if: a tick should ever run more than one item concurrently - that
  needs a real work-claiming mechanism, not a wider busy check.

### D-018: Liveness is something a worker states, not something a row implies

- Status: Accepted
- Why: Nexus inferred liveness from `updated_at`, which only says "this row
  changed recently". That is wrong in both directions: a healthy controller
  turn can spend minutes inside one local inference call without writing
  anything, while a dead one keeps its last `updated_at` forever. D-017's
  staleness rule and #160's reaper both had to pick thresholds in hours
  because no better signal existed - the live database held 17 of 41 sessions
  stuck `active` for 8 to 19 days.
- Rule: long-running work is *claimed* (`worker_id`, `claimed_at`), refreshes
  `last_heartbeat_at` while its owner is alive, and is released with an
  explicit `terminal_reason` when it ends. `modules/heartbeat.py` holds the
  shared semantics; each subsystem persists them in its own table.
- A heartbeat never touches `updated_at`. Otherwise it would re-arm every
  timestamp-based check in the system and quietly make dead work look busy.
- Silence only means death where a worker was supposed to be working.
  `waiting_for_user` and `blocked_missing_capability` are deliberate resting
  states with no owner by design, and are never reaped.
- A timeout of zero, a negative number or an unparsable one is a
  configuration mistake, not a request to reap everything in flight: it falls
  back to the default, and no configuration can push the timeout below a safe
  floor. Calling live work dead is worse than noticing dead work late.
- Reconciliation clears the heartbeat it selects on, so it is idempotent by
  construction, and it never re-runs the work - re-running a turn costs a
  model call and may repeat whatever killed it (as in #160).
- Reaping is a compare-and-set, not a two-step read-then-write. The reaper
  runs in a different process from the worker, so the window between
  selecting a candidate and ending it is real: the write re-checks the owner
  and the exact heartbeat it selected, and a worker that beat in between
  keeps its claim.
- Claiming is a prerequisite for running a turn, not bookkeeping around one.
  A claim that fails aborts the turn instead of degrading to unowned writes:
  the fallback would disable the ownership rules precisely in the failure mode
  they exist for, and refusing leaves the session to whoever does own it.
- Every write a claimed turn makes is scoped to its own worker id, and the
  terminal write is a single transaction. A turn that raises or is cancelled
  says so (`failed`, with `turn_failed`/`turn_cancelled`); `heartbeat_timeout`
  is reserved for a worker that never got to say anything at all, so the two
  facts stay distinguishable.
- Ownership survives reaping: `worker_id` stays on the row so "who was
  running this" is answerable afterwards.
- Revisit if: concurrency arrives. Claiming is currently unconditional
  because one worker drives a session at a time; multiple workers competing
  for the same item would need a conditional claim (compare-and-set on the
  existing owner) on top of the same columns.

### D-019: Nexus has one local-model execution mechanism

- Status: Accepted
- Why: `FailureAnalysisService` kept its own `LlamaCppClient` and called
  llama-server directly with a 60s read timeout, while controller inference
  already went through the queued `LocalLlmJobService`. In the #115/#116 E2E
  rerun that second path did exactly what a second path does: finalization
  reached `needs_review`, the diagnosis started, the direct HTTP call timed
  out on a model that routinely needs minutes, and a healthy analysis was
  persisted as `analysis_unavailable` (record
  `2fcc742c2b4e4d479ae321b458b11978`). Two paths also means two concurrent
  requests to a single-worker llama-server.
- Rule: every feature that needs the local model submits to
  `modules/local_llm_jobs.py` and awaits the job. Queueing, serialization,
  liveness, cancellation, stuck-job detection, logging and resource control
  live there, not in each feature. A caller that needs a second inference
  mechanism needs a reason, not a convenience.
- One service per database per process (`acquire_local_llm_job_service`),
  because sharing the *class* would still mean two workers in one process.
  Callers that bring their own client (tests) keep an isolated worker;
  production callers bring none and therefore share.
- A process-wide singleton is not the invariant, though: Nexus runs the
  controller (`nexus_tick`) and the API in separate processes against one
  database, so "one request at a time" has to be decided where both can see
  it. The single inference slot is claimed in SQLite (`BEGIN IMMEDIATE`:
  release dead claims, refuse if a live claim exists, else take the oldest
  queued job), and the claim states its liveness under D-018 (`worker_id`,
  `lease_expires_at`). A lapsed lease means a dead process, and its slot is
  released; a live lease is never touched by another process's startup,
  which is how the old blanket "mark every running row failed at startup"
  recovery would have killed a healthy job in another process.
- Every local-model caller means every one: the `local_worker` delegate is a
  role on this queue too, not a private client call. Only genuinely
  different resources (Codex, Azure OpenAI) keep their own paths.
- Callers are told apart by a job `role`, not by a separate mechanism. The
  role is observability only - it never changes how a job runs - so
  "what did failure analysis ask the model, and how did those jobs end?"
  is answerable without filtering controller traffic by hand.
- No feature-side deadline on inference. How long a valid answer takes is
  the model's business; the job service's watchdog and stuck-job sweep decide
  when a job is genuinely dead. A fixed per-feature timeout is how a slow
  success became a durable failure in the first place.
- A failed, cancelled or stuck job still produces the feature's normal
  degraded result (for failure analysis: exactly one `analysis_unavailable`
  record), and never re-enters the feature that produced it - diagnosing a
  failed diagnosis is a loop, not a recovery.
- Revisit if: local inference stops being the bottleneck resource - e.g. a
  model host that can genuinely serve concurrent requests. Then the shared
  component grows a concurrency limit; the callers still do not grow their
  own HTTP paths.

### D-020: Nexus has one tool mechanism, used by several roles

- Status: Accepted
- Why: Tool discovery and tool execution lived inside
  `NexusTaskOrchestrator`, so only the persistent controller could reach
  them. Every other LLM role could describe the evidence it was handed but
  never check anything. The #115/#116 rerun showed the cost: finalization
  returned an old persisted `needs_review` reason saying
  `GITHUB_TOKEN/GH_TOKEN` was unavailable, failure analysis produced a
  correct, well-formed diagnosis of exactly that claim, and asked a human to
  verify a token the running `agent-api` process already had. The real cause
  was stale persisted adoption state (#171). No amount of better reasoning
  over the supplied evidence could have found that - only current evidence
  could.
- Rule: `modules/nexus_tool_router.py` owns capability discovery
  (`find_tool`) and read-only capability execution (`call_tool`) for every
  role: one `OperationsRegistry` catalog, one read-only denylist, one
  candidate-scoring and resolver prompt, one "`call_tool` needs a prior
  `find_tool` grant" rule, one set of registry-side validation, redaction and
  audit logging. A role that needs tools uses this; it does not grow its own.
- Roles differ by permission, not by mechanism. The controller may also run
  commands, execute Python and delegate; failure analysis may only discover
  and call read-only capabilities, and its action vocabulary contains nothing
  else. That is the whole difference.
- Read-only is declared per capability (`mutates_state` on its
  `OperationSpec`, required and keyword-only), never inferred from a denylist
  of known-mutating names. A denylist gets the default backwards: an
  operation added later would be read-only until somebody remembered to
  exclude it, so a role that may only read would gain privileges nobody
  granted it. Declaring it means a new mutating operation is invisible to
  that role with no central list to maintain, and a spec that does not answer
  the question does not construct. A spec that declares nothing - a
  duck-typed one from elsewhere - is treated as mutating, because the
  direction that needs a decision is the unsafe one.
- Two things stay injected, because they are genuinely per-role: inference
  (the controller routes resolver calls through a memory budget with an
  external-provider fallback; a diagnosis just queues a job under D-019) and
  where grants live (persistent session events for the controller, the
  current run for a one-shot diagnostic role).
- A persisted failure reason is a claim about the past. When it asserts
  something about the runtime or the configuration, diagnosis checks it
  before interpreting it, and current evidence that contradicts it wins -
  the stale string then becomes a finding rather than the conclusion.
- Diagnosis gathers evidence; it never acts. No lifecycle transitions, no
  code edits, no merges, no restarts, no retries - enforced by the read-only
  catalog rather than by asking the model nicely.
- Bounded like every other tool loop: a small step budget, the same
  duplicate/non-progressing loop guard the controller uses, and tool output
  redacted and length-capped before it reaches a prompt, a log, the persisted
  analysis or the outbox.
- No tools, or no capability for what was asked, is a valid outcome that must
  be *said*: the diagnosis reports the missing evidence instead of assuming
  either answer. Silence here is what made an unverified analysis
  indistinguishable from a verified one.
- Revisit if: a role needs a capability that is not read-only. That is a
  permission model on the router (per-role allowlists), not a second router.

### D-021: The live checkout is deployable state, and refresh self-heals it

- Status: Accepted
- Why: Refresh aborted whenever `/agent` was dirty. The invariant it was
  protecting - never deploy over somebody's uncommitted work - was right, but
  refusing to act was the wrong way to hold it. In the case that forced this
  (#186) the only blocker was one *generated* file,
  `communication/outbox/20260914170857-failure-analysis-7a2bc0c1.md`, while the
  checkout sat nine commits behind `origin/main`. The deployment stalled and the
  artifact still had to be cleared by hand, so the failure mode was "old code
  keeps running" plus "a human is needed anyway".
- Rule: `main` on the Nexus VM never retains local working-tree changes after
  refresh handling. `modules/checkout_rescue.py` inspects the checkout and makes
  it clean before `modules/nexus_refresh.py` updates it.
- Work and runtime output are preserved differently, because they are different
  things. Tracked/staged changes and unrecognized untracked files are *work*:
  they are committed onto `nexus/rescue/<timestamp>-<short-head>` and stay in the
  repository. Recognizable Nexus output - legacy in-repo mailbox drops, `state/`,
  logs, caches, databases, temporary files - is *archived* outside the repository
  under `/var/lib/nexus/runtime-archive/<timestamp>/`. Generated files must never
  become a code commit, and code must never end up in an archive directory
  nobody reads.
- Unknown files are work, not artifacts. Preserving a file that turns out to be
  junk costs an unused branch; deleting one that turns out to matter is
  unrecoverable, so the classification defaults to the recoverable mistake.
- Preservation is never destructive: no `git reset --hard`, no force checkout,
  no force push, no silent delete. A deployment branch carrying local commits is
  realigned by naming those commits on a rescue branch first and then moving the
  branch reference - the commits stay reachable, which is what makes the move
  safe.
- A failed rescue or archive aborts the refresh *before* `main` is touched, and
  the archive step rolls back files it already moved. A refresh that cannot
  preserve must not deploy.
- Rescue and archive are idempotent. A clean checkout needs no rescue, and an
  artifact whose content is already recorded in the archive index is
  deduplicated rather than copied again, so retries cannot accumulate
  duplicates.
- Services restart only after the updated checkout is verified (right branch,
  clean tree, expected commit), and the result reports what was rescued, what
  was archived, the previous and new HEAD, and how the services came back.
- This is a safety net, not a workflow. Experiments still belong on their own
  branches; a rescue branch appearing at all is a signal that something wrote to
  the deployment checkout.
- Revisit if: rescue branches start accumulating in normal operation. That
  means something is using `/agent` as a workspace, which is a problem to fix at
  the source rather than to keep rescuing.

### D-022: Coding work rotates between interchangeable CLI agents

- Status: Accepted
- Why: Every piece of real coding work — mailbox task, `/codex/tasks` call,
  experiment — went to the Codex CLI, because it was the only agent the
  repository could reach. That is a single point of failure (quota, outage,
  model regression) and it also made "which agent is better at this kind of
  work" unanswerable, since only one of them ever ran. The Claude Code CLI is
  installed on the Nexus VM as well and offers the same non-interactive,
  single-call contract (#193).
- Rule: a coding agent is a *worker* behind one contract
  (`modules/coding_worker.py`): same `run()` signature, same result type, same
  sandbox vocabulary, same audit entry. `CodexWorker` and `ClaudeWorker` are
  implementations of it, and nothing above them may assume which one it got.
- The provider rotates in stable round-robin order per unit of work
  (`modules/coding_provider.py`). The last unpinned selection is persisted in
  SQLite so alternation survives process and host restarts. Neither agent is
  the default and there is no "primary" to fall back from: alternating is the point, both because it
  removes the single dependency and because comparable runs accumulate on both
  sides in the same store.
- The rotation is narrowed, never widened: `AGENT_CODING_PROVIDER` pins one agent
  (incident, debugging), `AGENT_CODING_PROVIDERS` is the allow list, and an
  agent whose CLI is not installed is dropped before selection. A configuration
  typo falls back to all known agents rather than leaving the system unable to
  run anything.
- One experiment runs on one agent. The selection is sticky per worktree, so the
  implementation call and its repair iterations stay with the same agent; the
  next experiment rotates again. A run that mixed agents mid-flight would be
  neither reproducible nor attributable.
- Naming an agent beats rotation, and a named agent is never swapped: the
  `codex_readonly` / `claude_readonly` delegates and the router's `codex`
  provider pin what they ask for. Rotation applies only where nobody chose;
  pinned selections do not advance its cursor.
- Whichever agent ran is recorded — `WorkerResult.provider`, `codex_tasks.provider`,
  `experiments.implementation_provider`, and the `runs` audit row — so a failure
  can be attributed and the two agents can be compared later. Rows written
  before this existed read back as `codex`, which is what they were.
- Capability is bounded by what the agent *can do*, not by what it is asked to
  do: read-only means the write tools do not exist for that run, and nothing
  may wait for a human, because a non-interactive run has nobody to answer.
- Revisit if: measured quality or cost differs enough between the agents that a
  uniform rotation is demonstrably wasteful. The replacement may be weighted selection
  based on recorded outcomes — not a return to a single hard-coded agent.


### D-023: Diagnosing Nexus is a command, and liveness is derived from a progress signal

- Status: Accepted
- Why: Finding out why a task failed meant composing ad-hoc shell commands
  through the mailbox — probe Git for a branch, guess the systemd unit, build a
  `journalctl` invocation, then correlate that against whichever of the four id
  namespaces (mailbox task, experiment, controller session, issue/PR) the
  operator happened to know. The #186/#187 investigation is the worked example:
  Nexus created a branch, produced no implementation commit and no PR, and
  establishing *where* execution stopped took a chain of hand-built commands.
  Those commands were also the diagnosis's only evidence path, so a slightly
  different guess produced a slightly different answer.
- Decision: one read-only backend (`modules/nexus_diagnostics.py`) resolves any
  reference an operator plausibly holds — a task/experiment/session id, a
  unique prefix, an issue or PR number, an inbox filename — reads the lifecycle
  out of the subsystem that owns it, and returns that together with bounded,
  correlated logs. It is exposed three ways over the same code: HTTP
  (`/diagnostics/status/...`, `/diagnostics/logs/...`), the operations catalog
  (`nexus_status`, `nexus_logs`, `nexus_last_failure`), and the
  failure-analysis routine, which no longer keeps a log lookup of its own.
- Liveness is the part that needed a new idea. D-018 made a controller session
  state that it is alive; mailbox tasks and experiments cannot, because they
  are driven through several processes and no single owner is alive across the
  span. Rather than inventing a second liveness mechanism, the same staleness
  rule is applied to the newest **progress signal**: the claiming session's
  heartbeat where there is one, otherwise the newest structured log entry,
  otherwise the row's `updated_at`. The verdict names which source answered,
  because "a worker stopped beating 40 minutes ago" and "the last log line is
  40 minutes old" are different strengths of the same claim. The timeout keeps
  `heartbeat.resolve_stale_timeout`'s clamped floor, so no caller can ask a
  question that declares live work dead.
- A stalled subject therefore counts as a failure for `last-failure`. A process
  that died mid-run never wrote a `failed` status; its log simply stops, which
  reads identically to work still in flight. Searching for explicit failures
  alone would miss exactly the case the command exists for.
- Read-only is structural, not a convention: the database is opened in
  SQLite's `mode=ro` and pinned with `PRAGMA query_only`, so a write is
  refused by the connection rather than merely absent from the code. Notably
  this means *not* using `run_store.init_db`, the write path's entry point,
  which creates the file and applies every additive migration and the
  mailbox-approval backfill — a diagnostic built on it would have mutated the
  database it was asked to describe, and would have done so on the incomplete
  databases most worth inspecting. Missing tables and columns degrade to
  "nothing known about that" instead.
- Prefix resolution refuses an ambiguous shorthand, and ambiguity is judged
  across every id namespace at once. Checking one table at a time and taking
  the first hit would let the order of an internal list, rather than the
  operator's input, decide between a mailbox task and an experiment.
- Revisit if: the lifecycle vocabulary stops fitting a subsystem, or the
  progress signal proves too coarse for a phase that is legitimately quiet for
  longer than the stale timeout. The fix there is a per-phase timeout, not
  dropping the derivation.

### D-027: An explicit "do not merge" stops every merge at one gate

- Status: Accepted
- Decision: A task's no-merge instruction is stored on its approval, and
  `GitHubWorkTrackingService.merge_and_complete` refuses and audits the merge
  for the experiment, the task and the pull request. Adoption and task end at
  `ready_for_review`, which Nexus neither retries nor resumes. Only a human
  merges such a pull request (#185).
- Why: Green CI merged PR #182 although its task said not to. The detection is
  broad on purpose: a wrong match costs a review, a missed one is irreversible.

### D-024: The mailbox inbox is the queue, and lock contention is not a failure

- Status: Accepted
- Why: The mailbox processor is a `Type=oneshot` unit on a one-minute timer
  guarded by a single-process lock. A timer invocation that started while
  another processor was working returned `busy` and exited 1, so systemd
  recorded `Failed to start Nexus mailbox processor` for a mailbox that was
  perfectly healthy and actively being drained. Worse, that exit lost work: a
  message that arrived while a processor was running had no one to pick it up —
  the invocation that would have seen it could not take the lock, and the
  active processor had already listed the inbox.
- Decision: `busy` is a successful outcome. The CLI and the webhook path log
  that another processor is already running and exit 0; only real faults
  (dirty worktree, failed sync with the remote, processing error) exit
  non-zero. A sync that fails partway through the drain is one of those
  faults, not a quiet early stop: the completed batches are still reported,
  but the run's status is `sync_failed`, the same as an initial sync failure.
  Anything else would let a mailbox checkout that is out of step with its
  remote — so not delivering messages at all — exit as a clean poll.
- The processor that owns the lock is therefore the only one that can notice
  new work, so it re-scans the inbox after each batch and keeps going until a
  scan turns up nothing new. The inbox stays the queue: no pending-work flag,
  no second queue, nothing to keep in sync with the files that are already the
  source of truth. Each pass resyncs the mailbox checkout first, because new
  messages reach this worker as commits, not as local writes.
- Only files no earlier pass has seen are processed again. A message left in
  the inbox deliberately — deferred, awaiting a retry, or out of retries — must
  not be re-run within the same invocation, or the drain would spin on it and
  burn its retry budget in seconds.
- The loop is bounded (`MailboxDrainLimits` in `modules/mailbox/config.py`:
  5 passes, 60 s, both overridable by environment variable), because continuous
  inbox traffic would otherwise pin one worker forever. Both bounds are checked
  *between* passes, never inside one, so the batch in flight always finishes
  and whatever is left is simply still in the inbox when the next timer run
  starts. Hitting a bound is logged and still exits 0.
- Evidence: `tests/test_mailbox_drain.py`.
- Revisit if: a single message's processing routinely outruns the timer period.
  The fix there is bounding per-message work, not removing the drain.

### D-025: The experiment benchmark measures at the worker and stores outside production

- Status: Accepted
- Decision: `benchmarks/tasks/*.md` are merged issues pinned to the commit they
  started from, scored by tests, expected files and the evaluation verdict. Runs
  go to a separate `benchmark.db`; usage is counted around the worker.
- Why: in the production database adoption, the planner and `problems` would act
  on benchmark experiments. `implementation_usage` keeps only the first call, so
  a model that needs repairs would look cheaper than one that does not.
- Evidence: `tests/test_benchmark.py`.
- Revisit if: the task set stops representing what `new_experiment` is asked to do.

### D-026: The tool-router MCP server is never the controller

- Status: Accepted
- Decision: `modules/tool_router_mcp.py` runs `NexusToolRouter` with
  `controller=False`, and its grants live in memory for one stdio process.
- Why: `service_restart` and `machine_reboot` are `controller_allowed` for the
  controller's own session. A local MCP client exists to test the router
  contract and must not inherit restart rights through it.
- Evidence: `tests/test_issue_118_tool_router_mcp.py`.
- Revisit if: an external client needs a mutating operation; that needs its own
  authorization rule, not the controller flag.

### D-027: The prompt budget is the context window; host memory is a signal

- Status: Accepted (#230)
- Decision: `compute_budget` bounds the whole rendered prompt by the context
  window minus reserved output. The never-compacted sections (rules, capability
  catalog, framing) are measured with `len()` of the rendered empty prompt and
  subtracted as-is; the remainder goes to the current input (capped at 20 %),
  observations, and history (10 % floor plus whatever observations leave).
  `_build_prompt` checks the rendered length and trims observations, then
  history, until it fits. If the fixed sections leave under 2000 characters,
  the turn fails with `prompt budget exhausted` instead of running blind.
  `low_memory`, `safe_local_budget_chars` and `memory_budget_chars` are
  logged and never shrink a section or gate a local call.
- Why: the old fixed shares (instructions 12 %, tool schema 18 %, ...) covered
  sections that were never compacted. A 500-char "budget" sat next to a
  9946-char prompt, and the only real effect was 150 characters for
  observations. On the VM a full-context prompt costs ~109 MB RSS more than a
  tiny one, once, bounded by `n_ctx`. That is not worth blinding the controller.
- Evidence: `tests/test_issue_230_prompt_budget.py`, `tests/test_prompt_budget.py`,
  `tests/test_task_orchestrator.py`
  (`test_measured_memory_pressure_still_sends_a_full_prompt_to_the_local_model`).
- Revisit if: a host shows real OOM kills from prompt size (not model load), or
  `CHARS_PER_TOKEN = 4` proves too optimistic for the prompt's JSON.

## Under evaluation

### D-007: Replace unnecessary custom orchestration with OpenAI Agents SDK patterns

- Status: In evaluation
- Why: Might simplify architecture, but needs evidence from V1 completion and benchmarks.

### D-008: Use Codex CLI as coding sub-agent on VM workflows

- Status: In evaluation
- Why: Potential productivity gains; needs practical reliability and cost evidence.
- Note: no longer the only such sub-agent — see D-022, which makes the CLI
  coding agent interchangeable and rotates it per task.

## Deferred

### D-009: Provision paid managed cloud database

- Status: Deferred
- Why: Explicitly postponed until SQLite limitations become a measured blocker.

## Open questions

- Best schema and retention policy for long-term experiment data.
- Best model/provider mix for quality per dollar.
- Formal safety/evaluation policy before any autonomous self-modification.
