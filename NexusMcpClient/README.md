# NexusMcpClient

Standalone MCP bridge that connects an MCP host to the remote Nexus MCP server.

## Run

Set `AGENT_API_KEY`, then run:

`python -m NexusMcpClient`

The default transport is stdio. To run the bridge as a Streamable HTTP MCP
server instead:

`NEXUS_MCP_TRANSPORT=streamable-http python -m NexusMcpClient`

The HTTP endpoint is available at `/mcp`.

Optional variables:

- `NEXUS_MCP_URL` — defaults to `https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com/mcp/`
- `NEXUS_MCP_TIMEOUT_SECONDS` — defaults to `30`
- `NEXUS_MCP_TRANSPORT` — `stdio` (default) or `streamable-http`
- `NEXUS_MCP_HTTP_HOST` — HTTP bind host, defaults to `127.0.0.1`
- `NEXUS_MCP_HTTP_PORT` — HTTP bind port, defaults to `8001`
- `NEXUS_MCP_HTTP_CORS_ORIGINS` — comma-separated allowed origins for browser-based MCP clients, defaults to `*`
- `MCP_WRITE_ENABLED` — enables remote write tools only when the Nexus server also enables them
- `GITHUB_TOKEN`, `NEXUS_MAILBOX_GITHUB_REPOSITORY`, `NEXUS_MAILBOX_GITHUB_BRANCH` — optional GitHub inbox fallback

Install dependencies from `requirements.txt`. The bridge mirrors the remote tool catalog and keeps MCP protocol traffic on stdout; logs go to stderr.
