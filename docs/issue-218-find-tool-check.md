# #218 manual check: find_tool alternatives against the real local model

`tests/test_issue_218_find_tool_alternatives.py` scripts llama-server's
responses, so it proves what the orchestrator does with a given answer but not
what the real local model decides. `scripts/check_issue_218_find_tool.py` runs
the genuine path - real `NexusTaskOrchestrator`, real `OperationsRegistry`,
real llama-server - over the task text that produced session
`1ca2d781836a481d8152744f5c1ff94d` and reports whether the failure signature is
back.

| Exit | Meaning |
| --- | --- |
| 0 | the failure signature did not reappear |
| 1 | `find_tool` resolved twice for the identical goal, or the loop guard ended the task |
| 2 | nothing was verified — the controller never reached `find_tool` |

Exit 2 is deliberately not a pass. A run that could not reach llama-server, or
that went straight to a final action, proves nothing about the path this check
exists to exercise.

## Running it on the VM

`scripts/nexus-check-218.ps1` does the whole thing — checks that llama-server
is up *and answering*, puts the branch on disk, runs the check and prints a
verdict:

```powershell
cd /agent
pwsh scripts/nexus-check-218.ps1
```

It returns the same exit codes as the Python check. Useful switches:

| Switch | Default | Purpose |
| --- | --- | --- |
| `-Branch` | `claude/wizardly-hopper-7icxf3` | branch to fetch and check out |
| `-SkipCheckout` | off | test the current checkout, touch nothing |
| `-Task` | `fix github issue #196` | task text to drive the controller with |
| `-MaxSteps` | `4` | controller turns before it must finish |
| `-Json` | off | also dump the raw observations |

## Asking the resolver one question

A full controller run costs several minutes per turn. To ask only "can the
resolver resolve this goal at all?", which is one model call:

```bash
cd /agent
.venv/bin/python scripts/check_issue_218_find_tool.py --resolver-only "I need to know the git state"
```

It prints the status, the reason, the candidates that were offered, the
described alternatives — and `what the model actually answered`, the raw
response. When an answer cannot be used, that raw text is the only thing that
explains why. Exit 0 if the resolver resolved the goal or deliberately rejected
the candidates, 1 if it produced no usable answer at all.

## Running the full check

To run the Python check on its own:

```bash
cd /agent
.venv/bin/python scripts/check_issue_218_find_tool.py
```

**It does not need `/etc/agent-lab/agent.env`.** That file is root-owned by
design (see `scripts/nexus.ps1`), so sourcing it as the `agent` user fails with
*Permission denied* — and the check has no use for it. The only thing it talks
to is llama-server on `127.0.0.1:8080`, which it reaches through the client's
own default; set `LOCAL_MODEL_URL` if yours listens elsewhere.

A `Standard_B1s` runs the local model slowly; allow a few minutes per
controller turn. One run is one sample of a small model — re-run it a few times
before concluding anything.

## What it writes

Session state goes to a scratch database (`/tmp/nexus-issue-218-check.db` by
default, deleted and recreated on each run), so production Nexus state is
untouched. The repository it inspects is the real `/agent` checkout.

This is a real controller turn: the controller may run read-only operations and
shell commands exactly as it would for any task. `--max-steps` (default 4)
bounds how many turns it gets.

## Python options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--task` | `fix github issue #196` | the task text to drive the controller with |
| `--db-path` | `/tmp/nexus-issue-218-check.db` | scratch session database |
| `--repo-root` | `/agent` | the checkout the operations inspect |
| `--max-steps` | `4` | controller turns before it must finish |
| `--json` | off | also dump the raw observations |

## Reading the output

The decision trace prints one line per observation. For a `not_found` it also
prints `recommended_next_action`, `recommended_operation`,
`direct_capabilities` and the `related_operations` names, which is what the
controller had to choose from. Any `find_tool_redirected` event is printed
below it.

Two shapes both count as a pass:

- the controller reads the alternatives and resolves one itself, or the goal it
  asked for is answered with the capability that owns it (`command`,
  `python_run`);
- it repeats the dead goal once, the redirect narrows it to the recommended
  operation, and the task continues.

A refused repeat that the session then recovers from is reported as a note, not
a failure — the loop guard doing its job without ending the task is the
intended last-resort behaviour.

## Is the resolver's schema even enforced?

`TOOL_RESOLVER_RESPONSE_FORMAT` declares `status` as an enum of four values,
and the #218 manual run returned a status outside it. That is only possible if
llama-server is not applying the schema, so `scripts/check_llama_json_schema.py`
asks the server instead of reasoning about it:

```bash
cd /agent
.venv/bin/python scripts/check_llama_json_schema.py
```

It sends a schema allowing exactly `"found"` together with a prompt asking for
`"banana"`. A constrained decoder cannot emit banana, so the answer settles it.
Four request shapes are compared — the one Nexus sends today, the OpenAI spec
shape, llama.cpp's own shape, and no schema at all as a control — and the
verdict names a shape that works if one does. Exit 0 if today's shape is
enforced, 1 if it is not.

Run against the VM's llama-server on 2026-09-17, this found that the shape
Nexus was sending was **not** enforced (it returned the forbidden value, fenced
in Markdown) while both alternatives were. Nexus now sends the OpenAI envelope,
which that probe showed working and which Azure OpenAI reads too; keep the
probe for the next time a server or model is swapped.

## Running it through Nexus instead of over SSH

The mailbox's `command` channel runs a shell command on the VM and puts the
result in the outbox, so neither of these checks needs an SSH session. Drop a
file in the mailbox repository's `inbox/`:

```markdown
---
type: command
created_by: <you>
---

set -eu
/agent/.venv/bin/python /agent/scripts/check_llama_json_schema.py
```

A push to `inbox/` triggers processing immediately through the GitHub webhook;
the one-minute timer is the fallback. The command runs detached, so a check
that takes minutes is fine — `timeout:` in the front matter is not read by
anything.

`inbox/20260917-1500-check-issue218-schema-claude.md` in the mailbox repository
runs both checks against this branch from a temporary worktree, so `/agent`'s
deployed checkout and its HEAD are never touched.

Nexus can also run the schema check itself: `local_model_schema_check` is a
read-only operation, so `find_tool` can resolve it and the controller can call
it without anyone dropping a file anywhere. It probes only the shape Nexus
sends plus the control — two model calls rather than four.

## Related evaluation

`python -m modules.tool_resolver_evaluation --mode local-llm` measures
`find_tool` selection accuracy across the standing case list against the same
local model. That answers "does the resolver pick the right operation"; this
script answers "does the controller escape a terminal `not_found`".
