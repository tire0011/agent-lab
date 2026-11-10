"""Which Nexus MCP tools read and which change something.

The planner (#384) may use every read-only tool, so a new read tool is
available to it as soon as it is listed here. A test keeps this list in step
with the tools ``nexus_mcp.py`` registers: every tool must be in exactly one
of the two tuples.
"""

from __future__ import annotations

READ_ONLY_TOOLS: tuple[str, ...] = (
    "health",
    "secure_health",
    "status",
    "problems",
    "ideas",
    "codex_tasks_list",
    "codex_task_get",
    "mailbox_tasks_list",
    "mailbox_task_get",
    "mailbox_messages_list",
    "experiments_list",
    "experiments_gate_recheck",
    "experiment_get",
    "experiment_drift_get",
    "experiment_logs",
    "experiment_adoption_get",
    "experiment_public_get",
    "activity_public_list",
    "activity_list",
    "diagnostics_public_get",
    "operations_catalog_get",
    "operation_chains_list",
    "operation_chain_get",
    "maintenance_status_get",
    "maintenance_audit_get",
    "github_issues",
    "github_issue",
    "git_log",
)

# ``chat`` runs the controller, which can act; ``service_status`` sits behind
# the write switch. Both stay out of the planner's reach.
WRITE_TOOLS: tuple[str, ...] = (
    "chat",
    "git_pull",
    "service_restart",
    "service_status",
    "run_tests",
    "experiment_start",
    "router_run",
    "shell",
)
