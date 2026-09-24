# Live tests

Tests that run against the real Nexus environment on the VM: the real local
llama-server, the real prompt budget, the production prompt. They answer "does
this fix hold up on Nexus?", which the unit tests in `tests/` cannot.

They are run by hand, one at a time. They are **not** part of CI: `pytest.ini`
only collects `tests/`, so neither CI nor a plain `pytest` sees this folder.

## Running on the VM

`scripts/nexus-live-check.sh` puts a branch into a throwaway worktree, prints
commit, llama-server state and free memory, runs pytest there and always cleans
up:

```bash
/agent/scripts/nexus-live-check.sh <branch> live_tests/test_issue_246_controller_role.py
```

Everything after the branch goes to pytest unchanged, so unit tests and single
scenarios work too:

```bash
/agent/scripts/nexus-live-check.sh <branch> tests/test_issue_246_controller_role_check.py
/agent/scripts/nexus-live-check.sh <branch> "live_tests/test_issue_246_controller_role.py::test_controller_role[after-read]"
```

As a mailbox command see `examples/live-check-template.md` in the mailbox repo.
Until the runner is on `/agent`'s checked-out branch, run it from the branch
itself:

```bash
git -C /agent fetch origin <branch>
git -C /agent show FETCH_HEAD:scripts/nexus-live-check.sh | bash -s -- <branch> live_tests/...
```

## Reading the result

- `PASSED` - the behaviour holds on Nexus.
- `FAILED` - the regression reproduced; the message says why.
- `ERROR` - the environment could not answer (e.g. llama-server down).

At the end pytest prints one JSON line per test under `live results (json)`
with commit, model, outcome, duration and whatever the test recorded (for
controller turns: provider, prompt size, LLM time, decision, verdict reason).

## Writing one

Fixtures from `conftest.py`:

- `controller_turn(task, observations)` - one real controller turn on a scratch
  database; returns the parsed decision, executes nothing.
- `orchestrator` - the underlying `NexusTaskOrchestrator`, for anything else.
- `llama_server` - `{"url", "model"}`; turns the test into an ERROR if the
  server is unreachable.
- `live_result` - a dict; its fields land in the test's JSON line.

A test is usually a handful of lines: build the observations, call
`controller_turn`, assert on the decision. See
`test_issue_246_controller_role.py`. The model is not deterministic, so when a
verdict is borderline run it a few times before drawing conclusions.
