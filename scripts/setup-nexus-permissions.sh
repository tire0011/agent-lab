#!/usr/bin/env bash
# setup-nexus-permissions.sh
# Lets the service user restart the Nexus units after a finalized task
# (task finalization runs `sudo -n systemctl --no-block restart nexus-mailbox agent-api`).
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
printf '%s ALL=(root) NOPASSWD: %s --no-block restart nexus-mailbox agent-api\n' "$SERVICE_USER" "$SYSTEMCTL" > "$tmp"
visudo -cq -f "$tmp"
install --mode=0440 "$tmp" "$SUDOERS_FILE"

echo "$LOG_PREFIX $SERVICE_USER may restart nexus-mailbox and agent-api ($SUDOERS_FILE)."
