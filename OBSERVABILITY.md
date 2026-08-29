# Runtime Observability (issue #76)

Lightweight, in-process runtime observability for Nexus: a live "what is
Nexus doing right now" view, on top of (not replacing) the existing
structured per-experiment JSONL logs (`modules/experiment_logger.py`).

This is intentionally small and requires **no additional infrastructure**
on the Nexus VM: no Docker, no OpenTelemetry Collector, no local Grafana /
Loki / Tempo / Prometheus, no extra database. Everything lives inside the
existing Nexus Python process.

## Components

- `modules/runtime_observability.py` — `RuntimeState`: a thread-safe
  singleton tracking the current task/phase/operation, a heartbeat, and a
  small bounded history of recent operations (spans). Never derived by
  parsing logs — explicitly updated as the pipeline runs.
- `modules/otel_adapter.py` — a vendor-neutral, fully optional adapter that
  mirrors each `RuntimeState` operation as an OpenTelemetry span and
  exports it via OTLP when configured. A complete no-op (never raises, no
  network calls) if the `opentelemetry-*` packages are not installed or no
  OTLP endpoint is configured.
- `modules/experiment_logger.py` — every JSONL log entry is now also
  tagged with `trace_id` / `span_id` / `parent_span_id` / `operation` when
  a runtime trace is active, so existing logs stay correlated with both
  the runtime status API and any exported OpenTelemetry trace.
- `GET /api/runtime/status` / `GET /api/runtime/trace` — small, fast,
  read-only HTTP endpoints (see below), the primary interface for an LLM
  or automation to ask "what is Nexus doing right now".

`modules/experiment_service.py` wraps the meaningful phases of the
experiment pipeline (`git.worktree_add`, `llm.generate`, `git.status`,
`git.diff`, `tests.run`) in `runtime_observability.operation(...)` blocks.
Each of those automatically gets a heartbeat (a background thread ticks
`RuntimeState.heartbeat()` every ~10s for as long as the block is open) and
is recorded in the bounded operation history — no per-call-site heartbeat
bookkeeping required.

## `GET /api/runtime/status`

Requires the same `x-api-key` header as `/secure-health` / `/activity`.

```json
{"status": "idle"}
```

```json
{
  "status": "running",
  "task_id": "task-123",
  "experiment_id": "abc123",
  "trace_id": "5e6f...",
  "phase": "verifying",
  "operation": "tests.run",
  "worker": "nexus",
  "task_duration_seconds": 482.1,
  "operation_duration_seconds": 121.4,
  "last_heartbeat_seconds_ago": 3.2,
  "healthy": true
}
```

`healthy` is `false` once `last_heartbeat_seconds_ago` exceeds the stall
threshold (default 60s) — this is how a caller distinguishes a slow-but-
working operation (e.g. a long local LLM generation, still ticking its
heartbeat) from a stalled one.

Immediately after a task finishes, `status` returns to `"idle"` (explicit
reset — the whole point is that "current" state is always accurate) but a
bounded `last_task` summary (`result`, `error`, `trace_id`, ...) is
included so a caller polling right after completion still sees how it
ended.

## `GET /api/runtime/trace`

```json
{
  "trace_id": "5e6f...",
  "operations": [
    {"name": "git.worktree_add", "span_id": "...", "parent_span_id": null, "phase": "implementation", "status": "completed", "duration_ms": 340},
    {"name": "llm.generate", "span_id": "...", "parent_span_id": null, "phase": "implementation", "status": "completed", "duration_ms": 382000},
    {"name": "tests.run", "span_id": "...", "parent_span_id": null, "phase": "verifying", "status": "running", "duration_ms": 4210}
  ]
}
```

Bounded (last 200 operations by default) and cheap — not a replacement for
the full JSONL logs (`GET /experiments/{id}/logs`), just enough recent
structure for an LLM to understand how execution got to its current state.

## Connecting to Grafana Cloud (optional)

1. Install the optional OTel dependencies on the Nexus VM:
   ```bash
   pip install -r requirements-otel.txt
   ```
2. In your Grafana Cloud stack, create/copy an OTLP (HTTP) access
   token and endpoint (Grafana Cloud → your stack → "Configure" →
   "OpenTelemetry" / "Connections → Add new connection → OpenTelemetry").
   Grafana Cloud typically gives you a base64 `instanceID:token` value for
   the `Authorization` header.
3. Set standard OpenTelemetry environment variables before starting
   Nexus (e.g. in the systemd unit's `Environment=` lines, **not** in the
   git repo):
   ```bash
   export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp-gateway-<region>.grafana.net/otlp"
   export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64 instanceID:token>"
   export OTEL_SERVICE_NAME="nexus"
   ```
4. Restart Nexus. If the packages are missing or the endpoint is
   unreachable, `modules/otel_adapter.py` logs a warning once and disables
   itself for the rest of the process — the actual Nexus task is
   unaffected either way.

No Grafana credentials or endpoints are ever hard-coded or committed to
this repository.

## Manual end-to-end verification checklist

1. `pip install -r requirements-otel.txt` on the target host.
2. Export `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` /
   `OTEL_SERVICE_NAME` for your Grafana Cloud stack, then (re)start Nexus.
3. Trigger a small experiment/task (mailbox task approval, or
   `POST /codex/tasks` + `.../run`, or any existing path that calls
   `ExperimentService.run_experiment`).
4. While it runs, poll `GET /api/runtime/status` with your API key —
   confirm `operation`/`phase` change over time and
   `last_heartbeat_seconds_ago` stays low (`healthy: true`).
5. In Grafana Cloud → Explore → Traces (or the Grafana mobile app), search
   for the `nexus` service and confirm one trace with child spans named
   `git.worktree_add`, `llm.generate`, `git.status`, `git.diff`,
   `tests.run` (durations and any recorded exception visible on the failed
   span, if the task failed).
6. Unset/break `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g. point it at an
   unreachable host) and restart Nexus; re-run step 3 and confirm the task
   still completes normally and `/api/runtime/status` still works — only
   the Grafana Cloud trace is missing.

## Tests

`tests/test_runtime_observability.py` and `tests/test_runtime_status_api.py`
cover: idle/running state, phase & operation transitions, heartbeat
behavior (including a stalled operation being marked unhealthy),
duration calculations, task completion resetting current status while
preserving `last_task`, trace-id consistency across operations,
parent/child span relationships, bounded operation history, the status/
trace HTTP endpoints (including the API-key requirement), and that a
missing/unreachable/misconfigured OTLP endpoint never raises or fails the
underlying task.
