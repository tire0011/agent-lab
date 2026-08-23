# Issue 46: persistent agentic Nexus controller

The local Nexus model is the high-level controller. It is not an intent classifier and a new experiment is not the default workflow.

## Inputs

The same controller can work on natural-language sources such as:

- mailbox tasks;
- GitHub issues supplied by a GitHub/MCP adapter;
- ideas from `IDEAS.md`;
- a user reply in an existing Nexus discussion session.

Each source has a stable `source_type` and optional `source_id`. Nexus stores a durable task session and event journal in SQLite so a clarification can be answered later without losing the original task, previous tool observations, or discussion context.

`NexusControllerService` is the generic code-level entry point. GitHub fetching stays outside the controller: `issue_source(...)` accepts issue data supplied by the official GitHub MCP or a future global MCP gateway. This keeps orchestration independent from one GitHub client implementation.

## Control loop

A bounded run follows this shape:

1. Give the local model the current request, compact persistent task history and available capabilities.
2. Let the model choose one next step.
3. If it chooses a tool, execute the real tool and return the observation to the local model.
4. Persist the observation and let the local model decide again.
5. Finish only when the local model selects an execution/final action or requests clarification.

The local model therefore keeps control after every tool and delegate call.

## Tools

### Nexus operations

The existing `OperationsRegistry` catalog is exposed as an inspection/tool surface. This includes experiment state, logs, git status, worktrees, service state and other registered operations.

### Memory search

`memory_search` searches existing Nexus memory instead of injecting all history into every prompt. The initial low-cost backend searches:

- SQLite `knowledge` entries;
- previous Nexus task/discussion events;
- `PROJECT_HISTORY.md`;
- `DECISIONS.md`;
- `ARCHITECTURE.md`;
- `IDEAS.md`.

Only compact matches enter the local model context. The tool interface is independent of the backend, so lexical search can later be replaced by embeddings/RAG without changing the controller.

### LLM delegation

`delegate` lets the local controller ask another bounded model for a sub-problem and then inspect the returned result before deciding again.

Available delegation providers currently include:

- `local_worker`: another bounded local-model reasoning pass;
- `codex_readonly`: existing Codex task infrastructure in read-only mode for difficult repository analysis/review;
- `azure_openai`: only advertised when Azure OpenAI is configured.

Delegation is not a transfer of control. External models return observations to the local Nexus model. Prompts explicitly prefer local/cheap work first and reserve external models for cases where they add material value.

## Final actions

The local model may choose:

- `new_experiment`: start the existing isolated implementation pipeline;
- `resume_experiment`: continue an existing persisted experiment;
- `command`: execute an explicit operational command through the trusted development command primitive;
- `answer`: finish directly from its own reasoning/tool/delegate results;
- `clarification`: pause the persistent session and ask the user one material question;
- `missing_capability`: stop honestly and record a structured requirement.

There is deliberately no automatic `new_experiment` fallback if local orchestration fails.

## Persistent discussion

A clarification marks the Nexus session as `waiting_for_user`. A later reply can resume the same session. Mailbox tasks use the approval id as their stable source id; `continue_discussion(...)` returns the task to orchestration with the same identity and preserves the follow-up in the eventual experiment goal when implementation is selected.

The code-level generic controller also exposes `reply(session_id, message)` so future API, web and MCP surfaces can continue the same persistent session directly.

## Missing capabilities

A `missing_capability` result is written as structured JSON under `state/requirements/`, including the triggering task, session, requirement and reason. This creates a durable input for later self-improvement or automatic Issue/tool creation.

## Resume correctness

Resume is explicit rather than guessed:

- `queued` starts the same experiment id through its normal pipeline;
- `verifying`, `timed_out` and verification-related `needs_review` reuse the existing worktree and rerun verification without implementation;
- `succeeded` is already complete;
- ambiguous/unsafe lifecycle states such as `implementing`, `selecting`, `failed` or `rejected` are rejected instead of being silently forced into verification.

This fixes the original regression where Nexus could correctly understand “resume experiment X” yet create or run the wrong lifecycle.

## Context/cost bounds

Persistent storage is larger than prompt context. Recent discussion history, memory matches and delegate/tool observations are truncated/bounded before being sent to the local model. This keeps the architecture usable with a small local model and avoids making long-term memory equivalent to an ever-growing prompt.

## Verification status

Focused unit tests were added for tool-driven resume, memory search, delegation, persistent clarification/reply, generic task/issue/idea sources, mailbox discussion continuation and timed-out verification resume. This branch still requires an actual test-suite/runtime run on Nexus with the local model before merge; the repository currently has no GitHub Actions run for these commits.
