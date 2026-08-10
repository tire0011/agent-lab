# Coding tools

Small provider-independent wrapper around external coding CLIs.

## Design

- `base.py` - process execution, timeout, result model, working-directory boundary.
- `codex.py` - OpenAI Codex CLI adapter.
- `copilot.py` - GitHub Copilot CLI adapter.
- `__init__.py` - provider factory.
- `__main__.py` - isolated smoke-test command.

The core agent does not automatically receive this write-capable tool yet. First validate each CLI independently on the VM, measure runtime/cost/quality, then expose the selected adapter to the orchestrator.

## Safety boundary

`CODING_TOOL_ROOT` defines the highest directory a coding tool may use. It defaults to `AGENT_FS_ROOT`, then `/agent`.

Codex is started with its working directory as the explicit `--cd` workspace and with `workspace-write` sandboxing. Child stdin is closed so non-interactive runs cannot wait indefinitely for parent input.

Copilot is started non-interactively without global automatic tool approval. Additional CLI permissions can be supplied deliberately with `COPILOT_EXTRA_ARGS` after isolated testing.

## Configuration

- `CODING_TOOL_ROOT` - allowed root, default `/agent`.
- `CODEX_COMMAND` - Codex executable, default `codex`.
- `CODEX_EXTRA_ARGS` - optional additional Codex CLI arguments.
- `COPILOT_COMMAND` - Copilot executable, default `copilot`.
- `COPILOT_EXTRA_ARGS` - optional additional Copilot CLI arguments.

## Smoke test

From the repository root:

`python -m coding_tools codex /agent "Inspect README.md and summarize the project. Do not change files."`

or:

`python -m coding_tools copilot /agent "Inspect README.md and summarize the project. Do not change files."`

The command prints the CLI output followed by tool name, success, exit code, runtime and timeout state.
