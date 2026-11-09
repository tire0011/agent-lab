# #41: GitHub through the official GitHub MCP server

Nexus reaches GitHub (Issues, pull requests, commits, repository contents)
through [`github/github-mcp-server`](https://github.com/github/github-mcp-server),
spoken to over MCP stdio. There is no second, custom Issues API: the MCP tools
are the interface.

## Setup on the VM (one-time, by the owner)

1. Install the release binary (a single Go executable, light enough for the
   1 GB `Standard_B1s`; Docker is not needed). Check the asset name on the
   releases page if the download fails:

   ```bash
   curl -fsSL -o /tmp/gh-mcp.tgz \
     https://github.com/github/github-mcp-server/releases/latest/download/github-mcp-server_Linux_x86_64.tar.gz
   sudo tar -xzf /tmp/gh-mcp.tgz -C /usr/local/bin github-mcp-server
   github-mcp-server --version
   ```

2. Put the token into `/etc/agent-lab/agent.env`, which every Nexus unit
   already loads (`EnvironmentFile=`). For this experimental phase the token
   may have full repository permissions.

   ```
   GITHUB_PERSONAL_ACCESS_TOKEN=<token>   # or reuse GITHUB_TOKEN / GH_TOKEN
   GITHUB_REPOSITORY=tire0011/agent-lab
   ```

3. Check it: `python scripts/github_mcp_check.py` reads data only.
   `python scripts/github_mcp_check.py --write` also creates, comments on,
   edits and closes a test Issue.

Nothing runs permanently. Each connection starts its own server subprocess
and ends it afterwards, so a restart or refresh of Nexus needs no extra step.
The configuration lives in `agent.env`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | `GITHUB_TOKEN`, then `GH_TOKEN` | token passed to the server's environment, never put on its command line |
| `GITHUB_MCP_SERVER_COMMAND` | `github-mcp-server` | executable, e.g. `docker` |
| `GITHUB_MCP_SERVER_ARGS` | `stdio` | its arguments, e.g. `run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server` |
| `GITHUB_MCP_TOOLSETS` | the server's default toolsets | passed on as `GITHUB_TOOLSETS`; `all` enables every toolset |
| `GITHUB_MCP_TIMEOUT_SECONDS` | `60` | per tool call |
| `GITHUB_REPOSITORY` | `tire0011/agent-lab` | default `owner/repo` |

## How Nexus uses it

- **Controller / tool router**: two operations in
  `modules/operations_registry.py`.
  - `github_mcp_tools` (read-only) lists the server's tools.
  - `github_mcp_call(tool, arguments)` runs one tool. `arguments` is a JSON
    object. The operation is mutating and `controller_allowed`.
  Examples:
  - `list_issues {"owner":"tire0011","repo":"agent-lab","state":"OPEN"}`
  - `issue_write {"method":"create","owner":…,"repo":…,"title":…,"body":…}`
  - `add_issue_comment {…,"issue_number":41,"body":…}`
  - `issue_write {"method":"update",…,"issue_number":41,"state":"closed"}`
  - `list_commits {…,"perPage":5}`

  Older server versions name the tools `get_issue`, `create_issue` and
  `update_issue`. `github_mcp_tools` shows the names the installed version
  uses.
- **Agents SDK path** (`agent.py`): `modules/agent_clients.build_github_server()`
  adds the server next to the filesystem server when a token and the binary
  are present. Without them the agent runs as before.
- **Code**: `modules/github_mcp.py` holds the configuration, the stdio client
  (`open_session`, `list_tools`, `call_tool`) and its readiness check.

The existing `modules/github_work_tracking.py` REST lifecycle (branches, PRs,
CI, merge) is unchanged by this.

## One-time migration of local todos/problems

`scripts/migrate_work_items_to_github.py` (logic in
`modules/github_issue_migration.py`) reads three sources:

- still-open problem goals from the experiment store: the latest attempt failed,
  timed out, was rejected or needs review, no attempt succeeded, and the goal
  names no GitHub Issue;
- ideas in `IDEAS.md` that are `new`, `exploring` or `selected`;
- the proposals in `docs/ideas-to-issues.md` (#340).

Each candidate is compared with every existing Issue, open and closed, by
normalized title and by a `nexus-migration-key:` line in the body. A rerun
creates nothing twice.

The local candidates were reviewed against the code on 2026-11-09. All 13
problem goals found in the store were already done or obsolete: the version
feature exists, the approval step was removed in #264, and the web UI was
removed in #517. They are listed with evidence in `RESOLVED_LOCAL_ITEMS` and
are never created.

```bash
python scripts/migrate_work_items_to_github.py --offline   # local candidates only
python scripts/migrate_work_items_to_github.py             # dry run against GitHub
python scripts/migrate_work_items_to_github.py --apply     # create the Issues
```

From now on, new todos and problems go straight to GitHub Issues. SQLite keeps
runtime, experiment and log state.
