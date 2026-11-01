---
base_commit: 60b5cb9fd3115810a9e54b8c50c8977a599341b7
expected_files: modules/task_orchestrator.py
test_modules: tests.test_task_orchestrator
tests_green_points: 50
expected_files_points: 30
evaluation_points: 20
---
GitHub Issue #435: Controller delegate without a provider stops the tick

Two ticks in a row ended with `TaskOrchestratorError: delegation provider '' is not available`: the controller asked to delegate but left `provider` empty.

Change: `_delegate_with_fallback` uses `local_worker` (or the first catalog provider) when no provider is given.
