# Integration Test Results (Live API)

Date: 2026-08-11
Target: `https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com`
Execution mode: non-admin, no secrets, read-only endpoint checks

## Endpoint discovery

Discovered via `GET /openapi.json`:

- `GET /health`
- `GET /secure-health`

No chat endpoint is currently published in OpenAPI.

## Executed checks and outcomes

1. HTTPS + connectivity
   - Request: `GET /health`
   - Result: `200 OK`
   - Payload: `{"status":"ok"}`

2. Status/problem endpoints from OpenAPI
   - `GET /health` -> `200 OK`
   - `GET /secure-health` without API key -> `401 Unauthorized` (expected without secrets)

3. Full chat call through CLI
   - Command behavior: `python -m client --base-url <url> chat "Integration test ping"`
   - Result: client executed request and returned clear failure because endpoint is missing
   - HTTP outcome: `404 Not Found` for `/chat`

4. Non-existing endpoint handling
   - Command behavior: `python -m client --base-url <url> chat --chat-path /definitely-does-not-exist "Ping"`
   - Result: clear error output with `HTTP 404`

## Summary

- HTTPS and public health endpoint are reachable and healthy.
- Secure status endpoint exists and correctly enforces authentication.
- No chat endpoint is currently exposed by the live API.
- Client error handling for missing endpoints is understandable and explicit.
