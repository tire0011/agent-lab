# #689 measurement: slow-machine check replay of 67c00298 and the last 10 merges

Measured 2026-11-14 on `agent-lab-vm` (Python 3.10, pytest 8.4.2, no
`pytest-asyncio`), script at experiment checkout `22747dbfa98c` + this change.
Raw output (one JSON record per commit): `docs/issue-689-timing-check-replay.jsonl`.
Posted to the issue: https://github.com/tire0011/agent-lab/issues/689#issuecomment-6876627657

```sh
python3 scripts/timing_check_replay.py 67c0029804119fbedf136e9174464e922c2f543c \
  $(git log --first-parent -11 --format=%H main | grep -v ^67c0029 | head -10)
# real 14m42s, exit 1 (some commit not ok)
```

## Definitions

- **Last 10 merges**: the 10 newest first-parent commits of `main`
  (a8d1e69 … 1d75615), not counting 67c00298 itself. 67c00298 was reverted
  (e4c3a68) because of the CI failures being checked, so it is the positive
  case, not a green merge.
- **Green**: the GitHub `tests` push run for that sha concluded `success`
  (checked with `gh api repos/.../actions/runs?head_sha=...`; all 10 were
  green).
- **False positive**: a green merge whose replay verdict is `failed`
  (verification would have rejected it with the timing hint). `needs_review`
  would be counted separately; there were none.

## 67c00298: both CI failures are flagged

CI run 43422855667 failed with exactly two tests:

| CI failure | replay |
|---|---|
| `tests/test_issue_642_local_model_timeout.py::TimingsLoggingTests::test_budget_fields_are_logged_next_to_n_ctx` (`3776 != 3775`) | flagged, `drift_only` |
| `tests/test_issue_667_llama_streaming.py::WorkerTests::test_admission_refuses_when_the_output_budget_is_below_768` (`360` vs `359` tokens) | flagged, `drift_only` |

Verdict `failed` (77.5 s). In total it flagged 11 drift-only tests: the 2
above, 2 more in test_issue_642 that CI did not fail
(`WorkerTimeoutTests::test_raw_httpx_timeout_with_empty_message_gets_a_named_reason`,
`CallTimeoutRestoreTests::test_too_slow_reports_the_applied_timeout`), and the
7 test_issue_667 tests that today carry `real_clock` (see below). The 18
async tests in test_llama_cpp_client/test_llama_router/test_multi_provider_orchestrator
also failed under drift, but the control run without drift showed they fail on
the VM anyway (`pytest-asyncio` missing). They went to `fails_without_drift`
and were not counted as timing failures. Both CI-failing tests are not exempt
today, so the commit would still be `failed` with today's exemptions.

## False positives on the last 10 merges

| commit | verdict | s | drift-only flags |
|---|---|---|---|
| a8d1e69 | ok | 98.3 | - |
| adf8f94 | **failed** | 97.4 | 7 × test_issue_667 (now `real_clock`) |
| 7fd3573 | ok | 0.6 | - |
| c05fcc2 | ok | 1.9 | - |
| 5fa585b | **failed** | 132.6 | 7 × test_issue_667 (now `real_clock`) |
| 19248a3 | ok | 54.8 | - |
| ac990ed | **failed** | 96.0 | `test_public_logs_api.py::PublicUnifiedLogsApiTests::test_returns_the_same_complete_entries_as_the_authenticated_feed` |
| f35a98d | **failed** | 117.8 | 7 × test_issue_667 (now `real_clock`) |
| e4c3a68 | **failed** | 70.3 | 7 × test_issue_667 (now `real_clock`) |
| 1d75615 | **failed** | 125.4 | 7 × test_issue_667 (now `real_clock`) |

- **As replayed (historical test code): 6 of 10 false positives.**
- 5 of the 6 come only from the same 7 test_issue_667 tests
  (`StreamingClientTests` ×6 and `WorkerTests::test_early_stop_stores_measured_partial_rates_not_guesses`).
  There a fake server sleeps in real time. These commits predate the
  `real_clock` exemption that #689 adds. On today's checkout,
  `NEXUS_TEST_CLOCK_DRIFT_MS=50 pytest tests/test_issue_667_llama_streaming.py tests/test_issue_642_local_model_timeout.py`
  gives 55 passed.
- **With today's exemptions: 1 of 10** (ac990ed). That test reads the VM's
  host journal: under drift the two requests see different `journal:*`
  entries (`journal:llama-server` vs `journal:nexus-mailbox`). It is flaky.
  On today's checkout it failed in 2 of 3 drift runs and in 0 of 6 runs
  without drift (4 other tests in that file always fail on the VM because of
  the host journal). CI has no such journal, so this is a VM-only false
  positive. It is not fixed here; the clean fix is to isolate that test from
  the host journal.
