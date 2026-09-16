"""Run the standalone Nexus MCP client over stdio."""

from __future__ import annotations

import logging
import os
import sys
import traceback

from .config import BridgeConfig
from .stdio_proxy import create_stdio_server, run_http_server


def main() -> None:
    log_path = os.path.join(os.environ.get("TEMP", "."), "NexusMcpClient.log")
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = BridgeConfig.from_env()
        server = create_stdio_server(config)
        if config.transport == "streamable-http":
            run_http_server(server, config)
        else:
            server.run(transport=config.transport)
    except Exception:
        details = traceback.format_exc()
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(details + "\n")
        except OSError:
            pass
        print(f"NexusMcpClient startup failed; details: {log_path}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
