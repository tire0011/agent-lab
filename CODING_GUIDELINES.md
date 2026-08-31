# Coding guidelines

## Scope and design

- Prefer small, focused modules with a single clear responsibility.

## Python conventions

- Target Python 3.10+ 
- Use `pathlib.Path` for filesystem paths and UTF-8 for text files.
- Keep database access parameterized and commit transactional state changes
  deliberately.