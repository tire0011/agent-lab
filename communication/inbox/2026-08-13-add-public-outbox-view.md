---
type: task
sender: rene-and-chatgpt
created_at: 2026-08-13T14:00:00Z
title: Add a public sanitized Outbox view
---

Implement one small observability improvement: let humans and ChatGPT see Nexus Outbox messages without SSH or an API key.

Scope:
1. Add GET /outbox/public.
2. Return at most the newest 50 Markdown files from communication/outbox.
3. Parse and return only an explicit allowlist: filename, timestamp, message type, short title/status, concise sanitized summary, and GitHub file URL when derivable.
4. Never return raw Markdown, prompts, task bodies, diffs, commands, logs, filesystem paths, environment data, headers, secrets, tokens, credentials, or stack traces.
5. Add an "Outbox" section to the existing website, visible without an API key, refreshing every 10 seconds.
6. Reuse the current public activity response patterns, sanitization helpers, and mobile-friendly UI.
7. Add focused API and security tests proving disallowed raw content cannot reach the public response.
8. Run the complete test suite.

Keep the change limited to this feature. Do not redesign the website or add dependencies.

If all tests pass, review the diff, commit with "Add public sanitized outbox view", push the current branch, restart agent-api.service if required, and verify /health and /outbox/public. If tests fail, preserve the sanitized failure through the existing activity system and do not push broken changes.
