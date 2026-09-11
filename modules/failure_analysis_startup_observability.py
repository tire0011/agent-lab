"""Startup-time status logging for failure-analysis observability (#151).

The mailbox package is imported before Nexus configures its central logging
handlers.  Patch the service constructor during import, then emit the feature
status when the mailbox service is actually created (after configure_logging in
both API and mailbox entry points).  No FailureAnalysisService or LLM client is
constructed here.
"""

from __future__ import annotations

import functools
from typing import Any

from modules.failure_analysis_observability import _db_identity, _event, _log_directory


def install() -> None:
    from modules.mailbox import task_service as base

    service_cls = base.MailboxTaskService
    if getattr(service_cls, "_failure_analysis_startup_observability_installed", False):
        return

    original_init = service_cls.__init__

    @functools.wraps(original_init)
    def observed_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _event(
            "failure_analysis_feature_status",
            importable=True,
            configured=True,
            implementation="failure_analysis_observability_v1",
            analyzer_provider="local",
            call_site="mailbox_service_startup",
            db_identity=_db_identity(getattr(self, "db_path", "")),
            log_directory=_log_directory(self._resolve_log_dir()),
        )

    service_cls.__init__ = observed_init
    service_cls._failure_analysis_startup_observability_installed = True


__all__ = ["install"]
