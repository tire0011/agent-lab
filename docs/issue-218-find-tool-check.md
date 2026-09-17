# #218 manual check: find_tool alternatives against the real local model

`tests/test_issue_218_find_tool_alternatives.py` scripts llama-server's
responses, so it proves what the orchestrator does with a given answer but not
what the real local model decides. `scripts/check_issue_218_find_tool.py` runs
the genuine path - real `NexusTaskOrchestrator`, real `OperationsRegistry`,
real llama-server - over the task text that produced session
`1ca2d781836a481d8152744f5c1ff94d` and reports whether the failure signature is
back.

It exits non-zero when either half of that signature reappears:

- `find_tool` resolved more than once for the identical goal, or
- the loop guard is what ended the task.

## Running it on the VM

Load the same environment file the Nexus services use, and make sure
llama-server is up first — the controller has nowhere to run otherwise:

```bash
systemctl status llama-server --no-pager
cd /agent
git fetch origin
git checkout claude/wizardly-hopper-7icxf3
set -a
. /etc/agent-lab/agent.env
set +a
.venv/bin/python scripts/check_issue_218_find_tool.py
```

From Windows PowerShell over SSH:

```powershell
ssh agent-lab-vm "cd /agent && set -a && . /etc/agent-lab/agent.env && set +a && .venv/bin/python scripts/check_issue_218_find_tool.py"
```

A `Standard_B1s` runs the local model slowly; allow a few minutes per
controller turn.

## What it writes

Session state goes to a scratch database (`/tmp/nexus-issue-218-check.db` by
default, deleted and recreated on each run), so production Nexus state is
untouched. The repository it inspects is the real `/agent` checkout.

This is a real controller turn: the controller may run read-only operations and
shell commands exactly as it would for any task. `--max-steps` (default 4)
bounds how many turns it gets.

## Options

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

## Related evaluation

`python -m modules.tool_resolver_evaluation --mode local-llm` measures
`find_tool` selection accuracy across the standing case list against the same
local model. That answers "does the resolver pick the right operation"; this
script answers "does the controller escape a terminal `not_found`".
