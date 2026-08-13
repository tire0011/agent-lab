---
type: task
created_by: rene-and-chatgpt
---

Improve the existing agent website so humans can observe Nexus without SSH. Keep the change small and reuse existing components.

Add a safe read-only GET /outbox endpoint that parses communication/outbox/*.md newest-first and returns filename, type, status, processed timestamp, source, summary, and GitHub URL where available. Add an Outbox card to the Web UI with count, recent messages, status, timestamp, summary, and links.

Add an authenticated read-only Activity/Logs area for only agent-api.service and nexus-mailbox.service. Return a bounded number of recent lines, redact secrets, tokens, keys, and authorization values, and never accept arbitrary commands, paths, or systemd unit names. Show current state and scrollable recent logs with refresh and pause controls.

Do not add terminal access, command execution, write/delete/retry controls, dependencies, or changes to mailbox processing. Preserve existing security boundaries. Add focused parser, API, authentication, redaction, and UI tests where practical. Briefly update README.md and ARCHITECTURE.md. Run the complete unit test suite.

If verification succeeds, commit and push the intended changes. Report changed files, API behavior, security controls, tests, commit SHA, and push result. If blocked, create a Problem containing the blocker and a concrete question.
