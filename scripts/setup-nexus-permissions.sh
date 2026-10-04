#!/usr/bin/env bash
# setup-nexus-permissions.sh
# Lets the service user hand the agent-api restart to a transient systemd unit
# after a finalized task (task finalization runs
# `sudo -n systemd-run --collect --on-active=2 systemctl restart agent-api`),
# and stop/start the llama-server unit for scripts/model-benchmark.sh (#302).
# Idempotent: nexus-refresh.ps1 runs it on every refresh, so a new machine
# gets the rule with its first refresh.
#
# Usage:
#   sudo bash scripts/setup-nexus-permissions.sh
#
# Optional overrides:
#   NEXUS_SERVICE_USER=agent

set -euo pipefail

LOG_PREFIX="[setup-nexus-permissions]"
SERVICE_USER="${NEXUS_SERVICE_USER:-agent}"
SUDOERS_FILE="/etc/sudoers.d/nexus-restart"
SYSTEMD_RUN="$(command -v systemd-run)"
SYSTEMCTL="$(command -v systemctl)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "$LOG_PREFIX ERROR: run this script as root (sudo)." >&2
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "$LOG_PREFIX ERROR: user $SERVICE_USER does not exist." >&2
    exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s ALL=(root) NOPASSWD: %s --collect --on-active=2 systemctl restart agent-api\n' "$SERVICE_USER" "$SYSTEMD_RUN" > "$tmp"
printf '%s ALL=(root) NOPASSWD: %s stop llama-server, %s start llama-server\n' "$SERVICE_USER" "$SYSTEMCTL" "$SYSTEMCTL" >> "$tmp"
visudo -cq -f "$tmp"
install --mode=0440 "$tmp" "$SUDOERS_FILE"

echo "$LOG_PREFIX $SERVICE_USER may schedule an agent-api restart and stop/start llama-server ($SUDOERS_FILE)."
