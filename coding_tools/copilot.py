import os
from pathlib import Path

from coding_tools.base import CodingTool


class CopilotTool(CodingTool):
    name = "copilot"

    def __init__(self, timeout_seconds: int = 600):
        super().__init__(os.getenv("COPILOT_COMMAND", "copilot"), timeout_seconds)

    def build_args(self, task: str, working_dir: Path) -> list[str]:
        return [
            "-p",
            task,
            "-s",
            "--no-ask-user",
            *self.extra_args("COPILOT_EXTRA_ARGS"),
        ]
