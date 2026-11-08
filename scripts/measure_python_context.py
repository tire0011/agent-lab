#!/usr/bin/env python3
"""Measure Python code, comment, and docstring size for selected files."""

from __future__ import annotations

import argparse
import ast
import io
import json
import subprocess
import tokenize
from pathlib import Path


def _source(path: Path, revision: str | None) -> str:
    if revision is None:
        return path.read_text(encoding="utf-8")
    result = subprocess.run(
        ["git", "show", f"{revision}:{path.as_posix()}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def measure(path: Path, source: str) -> dict[str, int | str]:
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    docstring_lines: set[int] = set()
    docstring_chars = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        docstring_lines.update(range(value.lineno, value.end_lineno + 1))
        docstring_chars += len(ast.get_source_segment(source, value) or "")

    comment_lines: set[int] = set()
    comment_chars = 0
    code_lines: set[int] = set()
    ignored_tokens = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    for token in tokenize.tokenize(io.BytesIO(source.encode("utf-8")).readline):
        if token.type == tokenize.COMMENT:
            comment_lines.add(token.start[0])
            comment_chars += len(token.string)
        elif token.type not in ignored_tokens:
            code_lines.update(
                line for line in range(token.start[0], token.end[0] + 1)
                if line not in docstring_lines
            )

    prose_chars = comment_chars + docstring_chars
    return {
        "path": path.as_posix(),
        "total_lines": len(lines),
        "code_lines": len(code_lines),
        "comment_lines": len(comment_lines),
        "docstring_lines": len(docstring_lines),
        "comment_chars": comment_chars,
        "docstring_chars": docstring_chars,
        "comment_docstring_chars": prose_chars,
        "approx_comment_docstring_tokens": round(prose_chars / 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--revision", help="read files from a git revision instead of the worktree")
    args = parser.parse_args()

    files = [measure(path, _source(path, args.revision)) for path in args.paths]
    numeric_keys = [key for key in files[0] if key != "path"]
    totals = {key: sum(int(item[key]) for item in files) for key in numeric_keys}
    print(json.dumps({"revision": args.revision or "worktree", "files": files, "total": totals}, indent=2))


if __name__ == "__main__":
    main()
