---
base_commit: d1d32906c13176f21f47d923e0ce53925be8134e
expected_files: modules/nexus_initiative.py, modules/nexus_planner.py
test_modules: tests.test_nexus_initiative, tests.test_issue_354_planning_step
tests_green_points: 50
expected_files_points: 30
evaluation_points: 20
---
GitHub Issue #439: Planner history: show the adoption outcome next to each experiment, so a review-blocked 'succeeded' is not read as done

**Problem**
In the planning prompt, experiments whose merge was blocked by review are listed as plain `succeeded`. `NexusInitiativeService._recent_experiments` reads only `goal, status, error` from `experiments`, so `planning_prompt` prints `- succeeded: GitHub Issue #418: ...`. A planner that does not check each experiment by hand could treat those issues as finished.

**Change**
- In `_recent_experiments`, LEFT JOIN the newest `experiment_adoptions` row for each experiment and return `adoption_status` and `adoption_reason` (whitespace collapsed, cut to 200 characters). Do the same for `needs_review` experiments that have an adoption row.
- In `planning_prompt`, render each history line as `- <status> / merge: <adoption_status>: <title> (<reason or error>)`. Examples: `succeeded / merge: blocked by review: ...`, `succeeded / merge: merged`, and `succeeded / merge: none` when there is no adoption row.
- If a review blocked the merge, use the label `blocked by review` instead of the raw `failed`, because `adoption_reason` starts with `Review by`. Any other failed adoption keeps the `failed` label.
- Keep today's output when the `experiment_adoptions` table is missing (OperationalError).

**Test** (`tests/test_nexus_initiative.py` / `tests/test_nexus_planner.py`, temporary SQLite)
1. A succeeded experiment plus a failed adoption whose reason is `Review by codex blocked the merge: X` is rendered as `succeeded / merge: blocked by review` and contains `X`.
2. A succeeded experiment plus a succeeded adoption is rendered as `merge: merged`.
3. A succeeded experiment without an adoption row is rendered as `merge: none`.
4. With two adoption rows for one experiment, only the newest is used.
5. A database without an `experiment_adoptions` table returns the old three fields, and the prompt has no `merge:` part.
6. A long reason is cut to 200 characters.
