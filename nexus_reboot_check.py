#!/usr/bin/env python3
"""Nexus reboot check - reboot the VM when Ubuntu asks for it (#332). After a system update Ubuntu
writes ``/var/run/reboot-required``; this runs on a timer (``deploy/nexus-reboot-check.timer``) and,
when the marker exists, schedules the reboot through the existing ``machine_reboot`` operation, which
already refuses while an experiment is selecting/implementing/verifying, so a reboot never kills work
mid-run - the next check simply tries again. No controller and no model call: a file check and one
operation."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from modules.logging_config import configure_logging
from modules.operations_registry import OperationsRegistry, OperationValidationError
from modules.run_store import DEFAULT_DB_PATH

configure_logging()
logger = logging.getLogger("nexus_reboot_check")

REBOOT_REQUIRED_PATH = "/var/run/reboot-required"


def check(registry: OperationsRegistry, *, marker_path: str = REBOOT_REQUIRED_PATH) -> str:
    """Schedule a reboot if one is required and nothing is running; returns ``not_required``, ``busy``,
    ``scheduled`` or ``failed``."""
    if not Path(marker_path).exists():
        logger.info("no reboot required (%s absent)", marker_path)
        return "not_required"
    try:
        result = registry.execute("machine_reboot", {}, caller="nexus-reboot-check")
    except OperationValidationError as exc:
        logger.info("reboot required but deferred: %s", exc)
        return "busy"
    if not result.success:
        logger.warning("reboot required but scheduling failed: %s", result.error_summary)
        return "failed"
    logger.info("reboot required; %s", "; ".join(result.output_lines) or "reboot scheduled")
    return "scheduled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--marker-path", default=REBOOT_REQUIRED_PATH)
    args = parser.parse_args(argv)

    # machine_reboot only reads the experiments table; no experiment service (and so no controller) is
    # needed.
    registry = OperationsRegistry(experiment_service=None, db_path=args.db_path)
    try:
        decision = check(registry, marker_path=args.marker_path)
    finally:
        registry.close()
    return 1 if decision == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
