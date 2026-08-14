---
type: task
created_by: rene-and-chatgpt
created_at: 2026-08-14T20:59:01.071Z
---

# Add read-only mailbox service status operation

Implement a small read-only operation named `mailbox_status`.

## Goal

Allow the Operations API, mobile Operations tab, and operation chains to query the health of both:

- `nexus-mailbox.service`
- `nexus-mailbox.timer`

## Required implementation

- Extend the existing central operations registry; do not create a second execution path.
- Use only the two fixed unit names above. Accept no user-controlled service or unit name.
- Invoke systemd with fixed argument arrays and `shell=False`.
- Bound and redact all returned output using the existing helpers.
- Expose the operation through the existing catalog so API, web UI, and operation chains see it automatically.
- Keep the operation read-only.
- Preserve existing operation names and behavior.

## Expected production file

- `modules/operations_registry.py`

Only change another production file if inspection proves it is required; explain why.

## Tests

Add focused tests in `tests/test_operations_registry.py` proving:

- the catalog contains `mailbox_status`,
- both fixed systemd units are queried,
- callers cannot supply another service name,
- fixed argument lists and `shell=False` are used,
- output is bounded/redacted,
- non-zero systemd exit codes are reported safely,
- the operation works through the OperationChainService using the canonical action name.

Run focused tests and the complete test suite. Do not alter unrelated behavior.
