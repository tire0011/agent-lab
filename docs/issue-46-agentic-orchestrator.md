# Issue 46: agentic task orchestration

The local Nexus model is the workflow controller for approved free-form tasks.

Flow:

1. Receive the natural-language task.
2. Present the local model with the currently available Nexus capabilities.
3. The model may call a bounded read/inspection operation.
4. Feed the real operation result back to the model.
5. Repeat for a small bounded number of steps.
6. The model chooses the final execution primitive:
   - new experiment
   - resume existing experiment
   - command
   - direct answer
   - clarification
   - missing capability
7. Only then is the selected primitive executed.

Existing experiment, command, and operation implementations remain separate primitives. The orchestrator does not require users to provide workflow syntax.

A `missing_capability` result is persisted as a structured JSON requirement under `state/requirements/` so it can later feed Nexus self-improvement or GitHub issue creation.

The orchestrator deliberately has no automatic `new_experiment` fallback when the local model fails. If orchestration itself is unavailable, the task stops for review rather than accidentally entering an unrelated lifecycle pipeline.
