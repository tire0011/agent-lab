---
base_commit: 5ecd7ffd3bb40df75f2af6c12fc444cc5ea906ee
expected_files: data_sources.py, api.py, nexus_mcp.py, modules/operations_registry.py
test_modules: tests.test_api, tests.test_operations_registry
tests_green_points: 50
expected_files_points: 30
evaluation_points: 20
---
GitHub Issue #434: problems: list recent experiment failures newest first, with limit and since filters, instead of only stale V0 run logs

**Problem**
`problems` returns only entries from `state/runs/*.json` (`data_sources.list_problems`), sorted by file name, so the oldest come first and recent experiment failures do not appear at all, because `list_problems` never reads the experiments table. The large stale output also wastes planner context.

**Change**
- In `data_sources.list_problems`, also collect experiments whose status is `failed`, `timed_out`, `rejected` or `needs_review` from the experiment store. Each becomes a `Problem` with `source="experiment"`, `id=<experiment id>`, the issue number from the `GitHub Issue #N:` prefix, the summary from `error` (cut to 200 characters), and `failure_phase`/`failure_type` when they are set. Skip an experiment when a later experiment for the same issue `succeeded`.
- Sort all problems by timestamp, newest first.
- Add optional `limit` (default 20) and `since` (ISO timestamp) parameters to `GET /problems`, to the `problems` MCP tool and to `_op_problems_list`.
- Keep the existing `run_log` entries, which stay subject to the same sort and limit.

**Test** (`tests/test_api.py`, and a new `tests/test_problems_experiments.py` with a temporary store and runs dir)
1. With an old failed run log and a newer needs_review experiment, `/problems` lists the experiment first, with `source="experiment"` and its error text.
2. A failed experiment for issue #7 followed by a succeeded one for #7 is not listed.
3. `limit=1` returns one entry, and `since` filters out older entries.
4. The existing `test_problems_*` tests stay green (empty runs dir, corrupt JSON file).
5. The MCP tool and the operation pass `limit` through.
