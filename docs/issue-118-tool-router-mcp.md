# #118: the tool router as a standalone MCP stdio server

`python -m modules.tool_router_mcp` exposes the Nexus tool router to any MCP
client over stdio, without FastAPI and without the task orchestrator. It runs
the same `NexusToolRouter` and `OperationsRegistry` as the controller.

Run it from the repository root, or the `modules` package will not be found.

| Option | Default | Purpose |
| --- | --- | --- |
| `--repo-root` | `/agent` | repository the git operations read |
| `--resolver` | `local` | `local` queues on llama-server through the shared job queue; `deterministic` is the evaluator's lexical resolver and needs no model |

`AGENT_DB_PATH` selects the database, as everywhere else in Nexus. Operation
calls are written to its audit tables with the caller `tool router mcp`.

## Tools

Exactly two tools are exposed.

- `find_tool(goal)` returns the router's result unchanged. `status` is `found`,
  `invalid_goal`, `not_found` or `unavailable`. On `found`, `resolved_tool`,
  `operation` and `arguments_schema` describe the selected operation.
- `call_tool(tool, arguments)` runs an operation. `arguments` may be omitted
  for an operation that takes none.

`call_tool` sets the MCP `isError` flag whenever nothing usable came back. The
structured result says why:

| Result | Meaning |
| --- | --- |
| `error: operation_not_granted` | no `status=found` for this operation on this connection |
| `error: operation_rejected` | the operation is not available here, or the registry refused the arguments |
| `result.success: false` | the operation ran and failed |

## What differs from the controller

- **Read-only only.** The server runs with `controller=False`, so
  `service_restart` and `machine_reboot` can be neither found nor called, and
  neither can `inbox_refresh`.
- **Grants live as long as the connection.** One stdio connection is one server
  process. Its grants are held in memory and are gone when it exits. Only
  `status=found` grants, and only the selected operation; `related_tools` and
  `related_operations` never do (#117).
- **`direct_capabilities` still names controller actions.** A `not_found` can
  recommend `command` or `python_run`. The answer is deliberately identical to
  the controller's, but an MCP client has no such action.

## Timeouts

With `--resolver local`, `find_tool` waits its turn on the single llama-server
slot, and a single resolver call has taken three minutes on the VM. Set the
client's tool timeout accordingly. The job keeps running if the client gives up.

## Copilot CLI

Copilot reads local servers from `~/.copilot/mcp-config.json`, or for one run
from `--additional-mcp-config`:

```json
{
  "mcpServers": {
    "nexus-tool-router": {
      "type": "local",
      "command": "/agent/.venv/bin/python",
      "args": ["-m", "modules.tool_router_mcp", "--repo-root", "/agent"],
      "cwd": "/agent",
      "tools": ["*"]
    }
  }
}
```

`cwd` is required, so prefer this JSON over `copilot mcp add`, which is
documented only with command, args and environment.

Smoke test, with the config saved as `tool-router.json`. It uses the
deterministic resolver if you add `"--resolver", "deterministic"` to `args`:

```bash
copilot -p 'Use only the nexus-tool-router MCP server. Call find_tool with goal
"inspect repository changes", then call_tool with the operation it selected and
arguments {}. Report status, operation and the output lines verbatim.' \
  --additional-mcp-config @tool-router.json \
  --allow-tool=nexus-tool-router --disable-builtin-mcps --no-ask-user
```

It passes when Copilot reports `status=found`, `git_status`, and the real status
output, and when `operations_audit` has a new `git_status` row.

### Verification status

Not complete. On 2026-10-31, with Copilot CLI 1.0.98, an equivalent command
using the deterministic resolver bound the server: the Copilot log shows the MCP handshake with
`server_info: nexus-tool-router` over stdio. The run then stopped at
`You have exceeded your monthly quota` before any model call, and
`operations_audit` stayed empty, so Copilot has not yet called either tool.
Rerun the smoke test once quota is available, then replace this section.

## Tests

`tests/test_issue_118_tool_router_mcp.py` starts the real subprocess and talks
MCP to it, covering the outcomes above plus grant isolation between connections
and a raw check that stdout carries only JSON-RPC. The `related_tools` case runs
against a scripted llama-server endpoint, because the deterministic resolver
never returns suggestions.
