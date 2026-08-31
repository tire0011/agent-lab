# Coding guidelines

## Scope and design

- Prefer small, focused modules with a single clear responsibility.


## Python conventions

- Target Python 3.10+ 
- Use `pathlib.Path` for filesystem paths and UTF-8 for text files.
- Keep database access parameterized and commit transactional state changes
  deliberately.

## Mailbox package

- Keep mailbox implementation under `modules/mailbox/`:
  - `core.py` contains parsing, validation, persistence helpers, locks, and Git
    primitives.
  - `processing.py` contains inbox processing and workflow orchestration.
  - `__init__.py` is the stable `modules.mailbox` compatibility boundary.
- New mailbox callers should import public symbols from `modules.mailbox`.
  Use internal modules only when working on their specific responsibility.