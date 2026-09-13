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
- Priority order is deliberate: unintegrated verified work first, then
  labelled open issues, then `new` ideas. Finishing started work is cheaper
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
  `NEXUS_INITIATIVE_DAILY_BUDGET` (default 8) self-started items per 24h.
  Issues must carry the `nexus` label, so opening an issue is not the same as
  dispatching an agent at it.
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
- Ownership survives reaping: `worker_id` stays on the row so "who was
  running this" is answerable afterwards.
- Revisit if: concurrency arrives. Claiming is currently unconditional
  because one worker drives a session at a time; multiple workers competing
  for the same item would need a conditional claim (compare-and-set on the
  existing owner) on top of the same columns.

## Under evaluation

### D-007: Replace unnecessary custom orchestration with OpenAI Agents SDK patterns

- Status: In evaluation
- Why: Might simplify architecture, but needs evidence from V1 completion and benchmarks.

### D-008: Use Codex CLI as coding sub-agent on VM workflows

- Status: In evaluation
- Why: Potential productivity gains; needs practical reliability and cost evidence.

## Deferred

### D-009: Provision paid managed cloud database

- Status: Deferred
- Why: Explicitly postponed until SQLite limitations become a measured blocker.

## Open questions

- Best schema and retention policy for long-term experiment data.
- Best model/provider mix for quality per dollar.
- Formal safety/evaluation policy before any autonomous self-modification.

