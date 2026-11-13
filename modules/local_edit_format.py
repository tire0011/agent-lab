"""SEARCH/REPLACE edit blocks for the small local coding model.

The format deliberately avoids unified-diff hunk counts, which small models
frequently get wrong.  This module is side-effect free: it builds before and
after trees in a temporary directory and asks Git to produce the patch consumed
by the local worker's existing validation pipeline.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class EditFormatError(ValueError):
    """A model response is malformed or cannot be matched safely."""


@dataclass(frozen=True)
class EditBlock:
    path: str
    search: str
    replacement: str
    number: int


_BLOCK_RE = re.compile(
    r"(?m)^(?P<path>[^\r\n]+)\r?\n"
    r"<<<<<<< SEARCH\r?\n(?P<search>.*?)"
    r"^=======\r?\n(?P<replacement>.*?)"
    r"^>>>>>>> REPLACE\s*$",
    re.DOTALL,
)


def parse_edit_blocks(text: str) -> list[EditBlock]:
    """Parse all edit blocks in *text*, rejecting non-format output."""
    fenced = re.search(r"```(?:\w+)?\s*\n(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    matches = list(_BLOCK_RE.finditer(candidate.strip()))
    if not matches:
        raise EditFormatError("local model did not return any SEARCH/REPLACE edit blocks")
    residue = _BLOCK_RE.sub("", candidate.strip()).strip()
    if residue:
        raise EditFormatError(f"unexpected text outside SEARCH/REPLACE blocks: {residue[:160]!r}")
    return [EditBlock(m.group("path").strip(), m.group("search"), m.group("replacement"), i)
            for i, m in enumerate(matches, 1)]


def _safe_path(root: Path, path: str, number: int) -> Path:
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or not path or "\\" in path:
        raise EditFormatError(f"edit block {number} has unsafe path {path!r}")
    full = root.joinpath(*posix.parts)
    try:
        full.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise EditFormatError(f"edit block {number} has unsafe path {path!r}") from exc
    return full


def _normal(line: str) -> str:
    return " ".join(line.split())


def _closest_hint(content: str, search: str) -> str:
    file_lines = content.splitlines()
    wanted = [line for line in search.splitlines() if line.strip()]
    scored: list[tuple[float, int, str]] = []
    for index, line in enumerate(file_lines):
        score = max((difflib.SequenceMatcher(None, _normal(needle), _normal(line)).ratio()
                     for needle in wanted), default=0.0)
        scored.append((score, index, line))
    best = sorted(scored, reverse=True)[:3]
    if not best:
        return "closest file lines: (file is empty)"
    return "closest file lines: " + "; ".join(f"{index + 1}: {line!r}" for _, index, line in best)


def _replace(block: EditBlock, content: str, exists: bool) -> str:
    if not block.search:
        if exists:
            raise EditFormatError(
                f"edit block {block.number} ({block.path}) has empty SEARCH but the path already exists"
            )
        return block.replacement
    exact = [m.start() for m in re.finditer(re.escape(block.search), content)]
    if len(exact) == 1:
        start = exact[0]
        return content[:start] + block.replacement + content[start + len(block.search):]
    if len(exact) > 1:
        raise EditFormatError(
            f"edit block {block.number} ({block.path}) SEARCH is ambiguous ({len(exact)} exact matches); "
            + _closest_hint(content, block.search)
        )

    search_lines = block.search.splitlines(keepends=True)
    file_lines = content.splitlines(keepends=True)
    width = len(search_lines)
    matches = [i for i in range(max(0, len(file_lines) - width + 1))
               if [_normal(line) for line in file_lines[i:i + width]] ==
               [_normal(line) for line in search_lines]]
    if len(matches) == 1:
        start = sum(len(line) for line in file_lines[:matches[0]])
        length = sum(len(line) for line in file_lines[matches[0]:matches[0] + width])
        return content[:start] + block.replacement + content[start + length:]
    qualifier = f"ambiguous ({len(matches)} whitespace-normalised matches)" if matches else "not found"
    raise EditFormatError(
        f"edit block {block.number} ({block.path}) SEARCH was {qualifier}; " +
        _closest_hint(content, block.search)
    )


def edits_to_diff(text: str, working_dir: str, timeout: float = 30,
                  allowed_paths: set[str] | None = None) -> str:
    """Return a Git unified diff for *text* without changing *working_dir*."""
    root = Path(working_dir).resolve()
    blocks = parse_edit_blocks(text)
    paths = {block.path for block in blocks}
    if allowed_paths is not None:
        outside = sorted(paths - allowed_paths)
        if outside:
            raise EditFormatError(
                f"local model edits files outside this subtask: {', '.join(outside)}; "
                f"only change {', '.join(sorted(allowed_paths))}"
            )
    contents: dict[str, tuple[bool, str]] = {}
    for block in blocks:
        full = _safe_path(root, block.path, block.number)
        if block.path in contents and not block.search:
            raise EditFormatError(
                f"edit block {block.number} ({block.path}) has empty SEARCH but an earlier block created it"
            )
        if block.path not in contents:
            if full.is_symlink() or (full.exists() and not full.is_file()):
                raise EditFormatError(f"edit block {block.number} ({block.path}) is not a regular file")
            exists = full.is_file()
            try:
                # decode bytes directly: unlike Path.read_text(), this keeps CRLF
                # sequences intact so an edit cannot rewrite unrelated newlines.
                content = full.read_bytes().decode("utf-8") if exists else ""
            except (OSError, UnicodeDecodeError) as exc:
                raise EditFormatError(
                    f"edit block {block.number} ({block.path}) could not read UTF-8 file: {exc}"
                ) from exc
            contents[block.path] = (exists, content)
        existed, content = contents[block.path]
        contents[block.path] = (existed, _replace(block, content, existed))

    with tempfile.TemporaryDirectory(prefix="local-edit-format-") as temporary:
        temp = Path(temporary)
        before, after = temp / "a", temp / "b"
        before.mkdir()
        after.mkdir()
        for path, (existed, result) in contents.items():
            source = root / path
            try:
                if existed:
                    before_target = before / path
                    after_target = after / path
                    before_target.parent.mkdir(parents=True, exist_ok=True)
                    after_target.parent.mkdir(parents=True, exist_ok=True)
                    # Seed both sides from the source so permissions (notably
                    # the executable bit) and metadata remain identical.
                    shutil.copy2(source, before_target)
                    shutil.copy2(source, after_target)
                else:
                    after_target = after / path
                    after_target.parent.mkdir(parents=True, exist_ok=True)
                after_target.write_bytes(result.encode("utf-8"))
            except (OSError, UnicodeEncodeError) as exc:
                raise EditFormatError(f"could not stage edit block for {path}: {exc}") from exc
        done = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--src-prefix=", "--dst-prefix=",
             "--", "a", "b"], cwd=temp,
            capture_output=True, timeout=timeout,
        )
        if done.returncode not in (0, 1):
            detail = done.stderr.decode("utf-8", errors="replace").strip()
            raise EditFormatError(detail or "git diff failed for SEARCH/REPLACE edits")
        # Do not use subprocess text mode here: universal-newline conversion
        # would strip CR bytes from patch context for CRLF source files.
        return done.stdout.decode("utf-8")
