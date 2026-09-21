# #223 manual check: does the controller reuse a successful tool result?

In session `3e2e565e49134703934e59bd57ad9118`, for the task `solve github
issue #196`, the controller fetched the issue successfully and then asked for
the identical `github_issue(196)` again. The issue text was in the prompt, but
wrapped in the whole execution envelope — tool wrapper, operation name, args,
`grant_event_id`, the operation schema, a nested result object and the rest of
the execution metadata. For a 7B local controller the answer it already had was
buried in the record of how it had been obtained.

`tests/test_issue_223_observation_compaction.py` pins the compaction itself,
but it scripts llama-server's answers: it proves what the orchestrator does
with a given decision and says nothing about what the real model decides after
a successful tool result. `scripts/check_issue_196_controller_progress.py` runs
the genuine path — real `NexusTaskOrchestrator`, real `OperationsRegistry`,
real llama-server — over the same task text.

| Exit | Meaning |
| --- | --- |
| 0 | the operation succeeded and was not requested again |
| 1 | it was executed or requested twice with the same arguments, or the loop guard ended the task |
| 2 | nothing was verified — the operation never succeeded |

Exit 2 is deliberately not a pass. A run that never obtained a successful
result cannot say whether the controller reuses one, and reporting it as a pass
would be the same mistake as trusting a test that never ran.

**#223 counts as verified only when the canonical `github_issue(196)` run
exits 0** — see "Verifying the canonical scenario on the VM" below. The
`git_status` variant is a secondary smoke test.

Repeating a call that keeps *failing* is a loop, but it is not this one, so it
reports 2 rather than 1 — with the repeats printed. There is no result to
reuse, and calling it the #223 regression would send the next reader hunting
for a prompt bug behind a broken token.

## Verifying the canonical scenario on the VM

`github_issue` refuses without a credential:

> GitHub lifecycle is required for Nexus implementation work. Configure
> `GITHUB_TOKEN`/`GH_TOKEN` before starting or resuming implementation.

The mailbox **`command`** channel cannot supply one, and that is deliberate:
`modules/mailbox/development.py` drops every variable whose name contains
`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AZURE_OPENAI` from the
child environment, so a shell command can never exfiltrate a credential. Do
not work around it. Copying the token into the mailbox, or printing it, is
exactly what that filter exists to prevent.

The mailbox **`task`** channel has no such problem. `modules/mailbox/
task_service.py` builds the orchestrator *in-process*, inside a service whose
unit carries `EnvironmentFile=/etc/agent-lab/agent.env`, so the controller
reaches GitHub with the real credential and nothing is copied anywhere. So the
canonical run happens there, and the check reads back what it did.

### Step 1 — run the canonical task

```markdown
---
type: task
created_by: <you>
---

solve github issue #196
```

### Step 2 — judge that session

Take the session id from the outbox response (or `GET /logs/trace/{id}`) and:

```markdown
---
type: command
created_by: <you>
---

cd /agent
/agent/.venv/bin/python scripts/check_issue_196_controller_progress.py \
    --session-id <session id> \
    --db-path /var/lib/nexus/agent.db
```

`--session-id` judges a run that already happened instead of driving one. It
opens the database read-only and never deletes it, unlike the scratch database
the driving mode uses. Exit codes are the same, and **this is the run whose 0
means #223 is verified**.

What the journal carries is enough for the verdict: every tool observation is
recorded as it happens, loop-guard refusals included. The one thing it does not
carry is a request the guard refused *before* execution, which is why the
verdict also reads the guard's own refusal events rather than relying on the
controller's decisions.

### The `git_status` variant

```bash
cd /agent
/agent/.venv/bin/python scripts/check_issue_196_controller_progress.py \
    --task "report whether the agent-lab repository has uncommitted changes right now" \
    --operation git_status
```

This runs entirely through the `command` channel, needs no credential, and
exercises the same compaction and loop-guard path. It is a **secondary smoke
test**: it does not carry the shape that produced the bug - a large issue body,
the `issue_number` argument identity, and how those compact under the VM's
observation budget. A green `git_status` run does not on its own mean #223 is
verified.

## Running it on the VM

`scripts/nexus-check-223.ps1` does the whole thing — checks that llama-server
is up *and answering*, warns up front when no token is present, puts the branch
on disk, runs the check and prints a verdict:

```powershell
cd /agent
pwsh scripts/nexus-check-223.ps1
```

It returns the same exit codes as the Python check. Useful switches:

| Switch | Default | Purpose |
| --- | --- | --- |
| `-Branch` | `claude/issue-223-local-test-2ibh4c` | branch to fetch and check out |
| `-SkipCheckout` | off | test the current checkout, touch nothing |
| `-Task` | `solve github issue #196` | task text to drive the controller with |
| `-Operation` | `github_issue` | the call whose repetition is the regression |
| `-MaxSteps` | `8` | controller turns before it must finish |
| `-ShowPrompts` | off | print the observation section of every prompt |
| `-Json` | off | also dump the raw observations |

## The exact mailbox command

The mailbox's `command` channel runs a shell command on the VM and puts the
result in the outbox, so this needs no SSH session. Drop a file in the mailbox
repository's `inbox/`:

```markdown
---
type: command
created_by: <you>
---

cd /agent
/agent/.venv/bin/python scripts/check_issue_196_controller_progress.py
```

A push to `inbox/` triggers processing immediately through the GitHub webhook;
the one-minute timer is the fallback. The command runs detached, so a check
that takes twenty minutes is fine.

That command tests whatever `/agent` currently has checked out. To test a
branch without touching the deployed checkout or its HEAD, run it from a
temporary worktree:

```markdown
---
type: command
created_by: <you>
---

set -u
BRANCH=claude/issue-223-local-test-2ibh4c
WT=/tmp/nexus-223-check

git -C /agent fetch origin "$BRANCH"
git -C /agent worktree remove --force "$WT" 2>/dev/null || true
rm -rf "$WT"
git -C /agent worktree add --detach "$WT" FETCH_HEAD

/agent/.venv/bin/python "$WT/scripts/check_issue_196_controller_progress.py" \
    --repo-root "$WT" \
    --db-path /tmp/nexus-223-check.db \
    --max-steps 8 \
    --show-prompts
printf '\n[check exited %s]\n' "$?"

git -C /agent worktree remove --force "$WT" || true
rm -rf "$WT"
```

`inbox/20260920-0700-run-issue223-controller-claude.md` in the mailbox
repository is that command as it was actually run.

## What it writes

Session state goes to a scratch database (`/tmp/nexus-issue-196-check.db` by
default, deleted and recreated on each run), so production Nexus state is
untouched. The repository it inspects is the real `/agent` checkout.

This is a real controller turn: the controller may run read-only operations and
shell commands exactly as it would for any task. `--max-steps` (default 8)
bounds how many turns it gets.

A `Standard_B1s` runs the local model slowly — the first turn of the 2026-09-20
run took 274 seconds, the whole run 667. Allow twenty minutes. One run is one
sample of a small model; re-run it a few times before concluding anything.

## Python options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--session-id` | – | judge a recorded session instead of driving one |
| `--task` | `solve github issue #196` | the task text to drive the controller with |
| `--operation` | `github_issue` | the call whose repetition is the regression |
| `--expect-arg` | `issue_number=196` | only count calls carrying this argument; repeatable |
| `--db-path` | `/tmp/nexus-issue-196-check.db` | scratch session database |
| `--repo-root` | `/agent` | the checkout the operations inspect |
| `--max-steps` | `8` | controller turns before it must finish |
| `--show-prompts` | off | print the observation section of every prompt |
| `--json` | off | also dump the raw observations |

`--expect-arg` defaults to `issue_number=196` only while `--operation` is
`github_issue`; a substitute capability has no issue number, so watching the
operation itself is the whole filter. Matching is a subset test, so a call
carrying extra defaults still counts.

## Reading the output

Four sections, in the order a diagnosis needs them:

- **controller decisions** — every decision the model produced, including ones
  the loop guard then refused. A repeat that never executes is still the
  controller failing to use a result it already has, so a check that inspected
  observations alone would miss it.
- **tool events** — what actually ran, with each event id.
- **compact observations given to the controller** — the observation section of
  each prompt, redacted. This is the gap #223 is about: printing only the
  stored event cannot show a problem that exists in the view.
- the verdict, and on failure the repeated decision verbatim.

Nothing prints a secret: decisions, reasons and prompt sections all go through
`redact_secrets`.

## The observation budget matters as much as the envelope

On a memory-pressured VM the budget collapses: the 2026-09-20 run reported
`effective_budget_chars: 500` and `low_memory: True`, which leaves about 150
characters for observations. Every observation became a bare stub, and the
prompt the controller read carried no tool output at all — only
`{"tool":"memory_search","truncated":true,"original_chars":5787,...}`.

That is why the fix also extends the fields a stub keeps. Stripping the
envelope takes a successful `github_issue(196)` from 669 characters to 311,
which is still over that budget, so the stub is what the controller actually
reads. With `args` and `status` among the identity fields it reads
`github_issue(issue_number 196) succeeded, event 158` — which is the whole
decision. A fix that only improved the full-fidelity rendering would never have
reached the run it was written for.

## Related checks

`scripts/check_issue_218_find_tool.py` answers "does the controller escape a
terminal `not_found`"; this one answers "does it use a result it already has".
Both drive the same real controller path.
