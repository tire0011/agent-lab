solve github issue #348

GitHub Issue #348: Per-experiment token budget for coding agents, shadow mode first

From `docs/ideas-to-issues.md` (#340, P6). This is idea-54.

Token use is only reported after an experiment ends (`implementation_usage_json`). One task used more than a million tokens.

**Change**
- Add a configurable budget per experiment, `NEXUS_EXPERIMENT_TOKEN_BUDGET`.
- `modules/experiment_service/service.py` checks it against the usage the coding agents already report while they run: Claude's `stream-json` events in `modules/claude_worker.py`, and Codex's JSON events.
- **Shadow mode:** it only logs where it would have stopped.
- **Enforce mode:** it stops the worker and ends the experiment in `needs_review`. The record keeps the stop reason, the budget and the amount consumed.

**Test**
A fake worker stream crosses the budget. In shadow mode it logs and continues; in enforce mode it stops and records the reason.
