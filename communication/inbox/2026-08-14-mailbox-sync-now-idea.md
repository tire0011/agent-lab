---
type: idea
created_by: rene-and-chatgpt
---

Add an authenticated "Sync Inbox now" action so users do not need to wait for the one-minute mailbox timer.

When the Inbox refresh button is used, first trigger one safe mailbox synchronization cycle: acquire the existing mailbox lock, fetch/pull new Git mailbox messages using the existing guarded workflow, process eligible messages once, and then refresh the Inbox/Outbox/task views. Reuse the timer's implementation rather than duplicating processing logic.

Expose a small authenticated POST endpoint with rate limiting, a short timeout, structured status such as processed_count and already_running, and sanitized errors. Never start a second processor while the timer or another manual sync holds the lock. Do not run when the main working tree is unsafe. Make the button show syncing/succeeded/already-running/failed states and remain mobile-friendly. Keep the periodic timer as a fallback and add concurrency, idempotency, authentication, dirty-tree, pull-failure, and UI tests.
