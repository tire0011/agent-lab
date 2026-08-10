from coding_tools.base import CodingTool, CodingToolResult
from coding_tools.codex import CodexTool
from coding_tools.copilot import CopilotTool


def create_coding_tool(name: str, timeout_seconds: int = 600) -> CodingTool:
    normalized = name.strip().lower()
    if normalized == "codex":
        return CodexTool(timeout_seconds=timeout_seconds)
    if normalized == "copilot":
        return CopilotTool(timeout_seconds=timeout_seconds)
    raise ValueError(f"Unknown coding tool: {name}")


__all__ = [
    "CodingTool",
    "CodingToolResult",
    "CodexTool",
    "CopilotTool",
    "create_coding_tool",
]
