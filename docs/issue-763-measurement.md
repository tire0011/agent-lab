# Issue #763 measurement

Use the UTC rollout time of the revision that first includes the fix. Keep a
table with experiment ID, first implementation start time, first provider,
format rejection evidence, terminal status and error, checkpoint or PR,
paid implementation call starts, and issue attempt charged. Record the
baseline and follow-up cutoffs with the table.

## Before rollout

1. Fetch recent rows with `experiments_list`. Take the last 10 starts on
   Mistral before rollout, ordered by implementation start time. Confirm the
   initial provider in `experiment_logs` using
   `implementation_provider_selected`. The row's implementation provider may
   reflect a later handoff. If the list limit omits rows, use a read-only
   snapshot of the experiments database ordered by
   `implementation_started_at` and fetch the logs by ID. Report the sample
   size and any gaps due to missing logs.
2. Look for `mistral_patch_rejected`. Count a charged no-change attempt when
   the experiment failed due to that rejection, had no publishable checkpoint,
   and counted against the linked issue's attempts. The terminal error may
   include “implementation produced no publishable … changes”. Record an
   unknown when the evidence is incomplete. Experiment `b3ad4d55` (#743) is
   the known baseline example.
3. For each charged attempt, look for the next experiment ID linked to the same
   issue. Count paid implementation starts before the first publishable
   checkpoint or the retry window ends. Report those observed starts and how
   many retries had no follow-up. Keep planning and review calls separate if
   the logs identify them.
4. Guard deferrals (`free-tier guard deferred coding` errors and
   `free_tier_guard_deferred` tick decisions) are not format failures. Report
   them separately: a deferral after planning is a charged attempt and a paid
   start; a tick deferral starts nothing.

## After rollout

1. Use the same sampling method for the first 10 Mistral implementation starts
   on or after rollout. Count each experiment ID once. Keep in-flight rows in
   the cohort and wait for terminal outcomes before reporting the result.
   Compare the logs with `free_tier_provider` and
   `free_tier_format_failures`. A NULL outcome denotes an incomplete or
   interrupted call. Mistral used after another provider is outside this
   cohort. The status issue lists daily completed outcomes by provider.
2. Count charged no-change attempts using the baseline rule. After rollout
   the outcome field or an `implementation_provider_fallback` event with
   `reason=free_tier_format_failure` can identify a rejection in addition to
   `mistral_patch_rejected`. Distinguish failures after a paid worker ran from
   runs ending on the original format rejection. The target is **0 attempts
   charged solely to the format rejection**.
3. Count actual paid starts using `codex_call_start` and `claude_call_start`
   in phase `implementation` after the format handoff event. A run gets at
   most one same-run handoff, so at most one paid implementation start follows
   a format failure; a later overload of that coder ends the run. Record paid repair starts separately.
   A handoff event with `paid_fallback_used=true` identifies the route. Use
   the worker events to confirm calls started.
4. For the comparison, estimate one paid implementation start per clean format
   failure if a full retry started directly on a paid coder. State this
   assumption and report the observed historical paid retry starts from step
   3 of the baseline separately. Include cases where the fallback could not
   start and do not infer actual usage from the estimate.

Post the baseline and follow-up counts to #763 with the experiment IDs,
missing evidence, charged attempts, actual paid fallback starts, and observed
and estimated full-retry paid starts. The follow-up result is pending until
the 10 starts have occurred and reached terminal outcomes.
