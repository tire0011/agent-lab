# DECISIONS.md

Decision log for architecture and operating choices.

## Accepted

### D-001: Start with a minimal measurable baseline (V0)

- Status: Accepted
- Why: Early measurement and iteration are more valuable than broad up-front architecture.
- Evidence: `agent.py` exists and logs per-run metrics.

### D-002: Use Azure OpenAI deployment `gpt-4o` as initial baseline

- Status: Accepted
- Why: Already available and tested in current subscription/resource setup.
- Evidence: API connectivity confirmed; deployment details recorded in repo docs.

### D-003: Prefer MCP servers/SDKs over custom orchestration where possible

- Status: Accepted
- Why: Reduces custom complexity and improves maintainability.
- Evidence: V1 direction and current MCP filesystem integration in `agent.py`.

### D-004: Keep initial persistence local and low cost (SQLite first)

- Status: Accepted
- Why: Budget constraints and fast iteration; avoid paid DB before clear need.

### D-005: Keep `/health` public, protect sensitive API routes

- Status: Accepted
- Why: Operational monitoring needs a simple public liveness check; protected routes require API key auth.
- Evidence: `api.py` exposes `/health` and protects `/secure-health`.

### D-006: Security baseline - never store secrets in repo

- Status: Accepted
- Why: Prevent credential leakage and reduce operational risk.

### D-010: Store runtime secrets outside `/agent`

- Status: Accepted
- Why: The MCP filesystem server is intentionally limited to `/agent`; storing secrets in `/etc/agent-lab/agent.env` prevents tool-mediated exposure of runtime credentials/config.
- Evidence: `run-agent.sh` sources `/etc/agent-lab/agent.env` and keeps `AGENT_FS_ROOT` constrained to `/agent`.

## Under evaluation

### D-007: Replace unnecessary custom orchestration with OpenAI Agents SDK patterns

- Status: In evaluation
- Why: Might simplify architecture, but needs evidence from V1 completion and benchmarks.

### D-008: Use Codex CLI as coding sub-agent on VM workflows

- Status: In evaluation
- Why: Potential productivity gains; needs practical reliability and cost evidence.

## Deferred

### D-009: Provision paid managed cloud database

- Status: Deferred
- Why: Explicitly postponed until SQLite limitations become a measured blocker.

## Open questions

- Best schema and retention policy for long-term experiment data.
- Best model/provider mix for quality per dollar.
- Formal safety/evaluation policy before any autonomous self-modification.

