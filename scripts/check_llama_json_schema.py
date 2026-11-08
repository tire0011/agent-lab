#!/usr/bin/env python3
"""Is the local model server applying the JSON schemas Nexus sends? The method and reasoning live in
``modules.llm_schema_probe``; this is the command-line way in. Nexus can also run it as the read-only
operation ``local_model_schema_check`` (Nexus shape plus control only), while this compares all four shapes."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.llm_schema_probe import SHAPE_LABELS, probe, verdict


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOCAL_MODEL_URL")
        or os.getenv("LLAMA_CPP_BASE_URL")
        or "http://127.0.0.1:8080/v1",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LOCAL_MODEL_NAME")
        or os.getenv("LLAMA_CPP_MODEL")
        or "qwen2.5-coder-7b-instruct",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--nexus-only",
        action="store_true",
        help="only the shape Nexus sends plus the control: two model calls, not four",
    )
    args = parser.parse_args()

    print("llama-server JSON schema enforcement probe")
    print(f"server: {args.base_url}")
    print(f"model:  {args.model}")
    print('schema: status must be exactly "found"')
    print('prompt: asks for "banana"')
    print()

    results = probe(
        base_url=args.base_url,
        model=args.model,
        timeout=args.timeout,
        nexus_only=args.nexus_only,
    )
    for key, outcome, detail in results:
        print(SHAPE_LABELS.get(key, key))
        print(f"  -> {outcome}: {detail}")
        print()

    healthy, lines = verdict(results)
    print("verdict")
    for line in lines:
        print(f"  {line}")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
