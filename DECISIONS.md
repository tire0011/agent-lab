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

### D-011: V2 run persistence uses SQLite with table-driven estimated pricing

- Status: Accepted
- Why: Queryable structured run history is required for controlled experiments; table-driven pricing enables historical model-rate changes without hard-coding prices in calculation logic.
- Scope: SQLite at `/agent/state/agent.db` with `runs`, `model_prices`, `tool_calls`, and `knowledge`; estimated cost is recorded per run, while actual Azure reconciliation is deferred to V2.1.

### D-012: Log reading is open - no API key, no reduced allowlist

- Status: Accepted
- Why: Nexus is a single-operator experiment on a dedicated VM, and
  analysing what it did is the entire purpose of the log surface. Making
  every analysis step depend on entering a key - and then seeing only an
  allowlisted subset without command/stdout/stderr/exception detail -
  made the logs materially less useful for the one job they exist for.
- Scope: `GET /logs`, `GET /logs/public` (now an alias returning the same
  complete entries) and `GET /logs/trace/{reference}` require no
  authentication. **Write endpoints are unchanged and still require
  `x-api-key`**, including the shell-backed `/operations/*` routes.
- Risk accepted: the API is reachable over the public internet, so log
  content is world-readable. Redaction still applies on both the write
  and the read path.
- Mitigation: because a leaked `AGENT_API_KEY` would turn open *read*
  access into *write* access, `modules.redaction` additionally scrubs the
  literal values of `AGENT_API_KEY`, `AGENT_GITHUB_WEBHOOK_SECRET`,
  `GITHUB_TOKEN` and `AZURE_OPENAI_API_KEY` wherever they appear - not
  only where a keyword-shaped pattern happens to introduce them.
- Revisit if: the VM stops being single-operator/experimental, or log
  content starts carrying third-party data.

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

