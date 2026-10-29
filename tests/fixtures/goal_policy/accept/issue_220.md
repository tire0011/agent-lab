solve github issue #220

GitHub Issue #220: Mailbox: accept all commands and redact secrets only in output

## Problem

The mailbox currently rejects command messages when the input contains text that looks like a secret.

Example from the VM:

- `20260918-1346-trace-issue196-session-chatgpt.md`
- rejected with: `Potential secret detected in message content`
- the command itself only referenced `$AGENT_API_KEY` so it could call the local agent API.

This makes the mailbox unreliable for diagnostics and administration commands. A command should be allowed to reference secret environment variables without the whole message being rejected.

## Desired behavior

For mailbox messages of `type: command`:

1. **Do not reject the command because the input contains or references a secret/key/token.**
2. Execute the command normally, subject to the existing command execution policy / timeout / sandbox rules.
3. Before writing command stdout/stderr/result to the mailbox outbox, scan the result for secrets.
4. Replace detected secret values with `****`.
5. The redaction must happen **before** anything is committed/pushed to the mailbox repository.
6. Also redact secrets from error messages, exception text, diagnostic metadata and any other user-visible output generated from the command.

## Important distinction

Secret detection should protect **outbound data**, not block legitimate command input.

For example this input must be accepted:

```bash
curl -H "x-api-key: $AGENT_API_KEY" http://127.0.0.1:8000/...
```

If the executed command prints the actual API key, the outbox must contain:

```text
x-api-key: ****
```

and never the real value.

## Suggested implementation

Introduce a single output-redaction step in the command processing pipeline immediately before outbox serialization/commit.

The redactor should preferably cover both:

- known secret values available from the process environment/configuration
- generic token/key patterns already recognized by the current secret detector

Do not rely only on key names: the actual secret **value** must be removed wherever it appears in stdout/stderr.

Keep the existing secret protection for other relevant ingestion paths if needed; this issue specifically changes the behavior for trusted mailbox `command` messages.

## Tests

Add tests covering at least:

- command containing `$AGENT_API_KEY` is accepted
- command containing words such as `token`, `api_key`, etc. is not rejected merely because of those words
- actual configured secret printed to stdout becomes `****`
- actual configured secret printed to stderr becomes `****`
- secret embedded inside a larger line/URL/header becomes `****`
- multiple occurrences are all redacted
- non-secret command output is unchanged
- secret never appears in generated outbox content
- existing command timeout/error handling still works

## Acceptance criterion

A diagnostic mailbox command can safely use credentials from the VM environment, while no credential value can leave the VM through command results written to the mailbox repository.
