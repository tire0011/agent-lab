---
type: task
created_by: rene-and-chatgpt
---

Improve the existing agent website so humans can observe Nexus without SSH.

Keep the change small and build on existing components.

Required outcome:

1. Add a read-only GET /outbox endpoint that safely parses communication/outbox/*.md newest-first and returns filename, type, status, processed timestamp, source, summary, and GitHub URL where available.
2. Add an Outbox card to the existing Web UI showing count and recent messages, refreshed with the existing overview.
3. Add an authenticated read-only Activity/Logs area for the fixed services agent-api.service and nexus-mailbox.service.
4. Return only a bounded number of recent log lines, redact secrets/tokens/authorization values, and never accept arbitrary commands, paths, or systemd unit names from the browser.
5. Show current state and a scrollable recent-log view in the Web UI with a small refresh interval and a pause control.
6. Do not add terminal access, command execution, write/delete/retry controls, dependencies, or changes to mailbox processing.
7. Add focused parser, API, auth, redaction, and UI tests where practical.
8. Update README.md and ARCHITECTURE.md briefly.
9. Preserve all existing security boundaries and behavior.
10. Run the complete unit test suite.

If the existing autonomous task/experiment workflow permits this task, investigate, implement, verify, commit, and push the bounded change. If it does not permit automatic execution, record the task for review and clearly report that limitation in the Outbox. If blocked or a decision is needed, report it through the existing Problems mechanism with a concrete question.

Report changed files, API behavior, security controls, tests, commit SHA, push result, or the precise reason work was not executed.
