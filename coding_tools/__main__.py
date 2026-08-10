import argparse
import asyncio

from coding_tools import create_coding_tool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a coding CLI through agent-lab")
    parser.add_argument("tool", choices=["codex", "copilot"])
    parser.add_argument("working_dir")
    parser.add_argument("task", nargs="+")
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    tool = create_coding_tool(args.tool, timeout_seconds=args.timeout)
    result = await tool.run(" ".join(args.task), args.working_dir)

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")

    print(
        f"tool={result.tool} success={result.success} exit={result.exit_code} "
        f"runtime_ms={result.runtime_ms} timed_out={result.timed_out}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
