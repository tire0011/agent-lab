import os
from pathlib import Path

from coding_tools.base import CodingTool


class CodexTool(CodingTool):
    name = "codex"

    def __init__(self, timeout_seconds: int = 600):
        super().__init__(os.getenv("CODEX_COMMAND", "codex"), timeout_seconds)

    def build_args(self, task: str, working_dir: Path) -> list[str]:
        return [
            "exec",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--cd",
            str(working_dir),
            *self.extra_args("CODEX_EXTRA_ARGS"),
            task,
        ]
