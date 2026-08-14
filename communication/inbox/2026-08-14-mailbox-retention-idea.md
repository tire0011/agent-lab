---
type: idea
created_by: rene-and-chatgpt
---

Add a bounded retention policy for the Git-backed mailbox so communication/inbox and communication/outbox remain small and easy to inspect.

Keep at most the newest five processed messages/results visible in each active mailbox folder. Never delete pending, unprocessed, retrying, or otherwise unresolved inbox messages. Preserve traceability through Git history and preferably move older processed entries to an archive before considering permanent deletion. Make retention deterministic, configurable, logged, tested, and safe during concurrent mailbox processing or failed Git pushes.
