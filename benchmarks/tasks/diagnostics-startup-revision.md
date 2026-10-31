---
base_commit: ee2f9a6c56bbfab9fd8975a39af33d274f4be8c9
expected_files: api.py, nexus_tick.py, modules/nexus_initiative.py
test_modules: tests.test_diagnostics_api, tests.test_nexus_initiative
tests_green_points: 50
expected_files_points: 30
evaluation_points: 20
---
GitHub Issue #441: Diagnostics report the git revision the process was started from, and flag a running service that lags behind the checkout

**Problem**
`GET /diagnostics/public` hides that the API process runs older code than the checkout. `_git_revision()` in `api.py` is an `lru_cache` that runs `git rev-parse HEAD` lazily on the first request, not at process start. It returns whatever the checkout points to at that moment, not the code that was loaded. So Nexus cannot tell whether a merged change is live.

**Change**
- In `api.py`, capture the revision once at import time (`_STARTUP_REVISION`, next to `_PROCESS_START_TIME`) and report it as `service.git_revision`.
- Add `service.checkout_revision`, the current `git rev-parse --short HEAD` (not cached), and `service.stale: bool`, which is true when the two differ. Expose the same fields in `GET /health` or `GET /status`.
- At the start of the initiative tick, compare the running API's `git_revision` with the checkout HEAD. If they differ, record one activity entry `service_stale` with both SHAs and trigger the existing restart hand-off (`modules/task_finalization.py` restart path). Do not start a new experiment in that tick.

**Test**
- `tests/test_api.py`: patch `subprocess.run` so it returns SHA A at import/startup and SHA B later. Diagnostics then report `git_revision=A`, `checkout_revision=B`, `stale=true`. When both are equal, `stale=false`.
- A tick test with a fake diagnostics client: on a stale result, exactly one `service_stale` activity entry is recorded, the restart hook is called once, and no experiment is submitted. On a fresh result, the tick proceeds as normal.
