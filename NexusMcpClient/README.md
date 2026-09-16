# NexusMcpClient

Standalone stdio MCP client that connects an MCP host to the remote Nexus MCP server.

## Run

Set `AGENT_API_KEY`, then run:

`python -m NexusMcpClient`

Optional variables:

- `NEXUS_MCP_URL` — defaults to `https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com/mcp/`
- `NEXUS_MCP_TIMEOUT_SECONDS` — defaults to `30`
- `MCP_WRITE_ENABLED` — enables remote write tools only when the Nexus server also enables them
- `GITHUB_TOKEN`, `NEXUS_MAILBOX_GITHUB_REPOSITORY`, `NEXUS_MAILBOX_GITHUB_BRANCH` — optional GitHub inbox fallback

Install dependencies from `requirements.txt`. The bridge mirrors the remote tool catalog and keeps MCP protocol traffic on stdout; logs go to stderr.
