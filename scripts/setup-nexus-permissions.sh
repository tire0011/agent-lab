#!/usr/bin/env bash
# setup-nexus-permissions.sh
# Lets the service user hand the agent-api restart to a transient systemd unit
# after a finalized task (task finalization runs
# `sudo -n systemd-run --collect --on-active=2 systemctl restart agent-api`),
# and stop/start the llama-server unit for scripts/model-benchmark.sh (#302).
# and restart nexus-mailbox and llama-server for the controller's service_restart
# (#315; modules/service_operations.py runs `sudo -n systemctl restart <unit>`),
# and schedule a VM reboot for the controller's machine_reboot (#319; runs
# `sudo -n systemd-run --collect --on-active=60 systemctl reboot`),
# and install one Ubuntu package for the controller's install_tool (#589; runs
# `sudo -n /usr/local/sbin/nexus-install-tool <package>`, refused by the
# denylist in /etc/agent-lab/tool-denylist).
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_TOOL="/usr/local/sbin/nexus-install-tool"
install --owner=root --group=root --mode=0755 "$SCRIPT_DIR/nexus-install-tool.sh" "$INSTALL_TOOL"
# Local model management (#673): download from Hugging Face, switch with rollback.
LLAMA_MODEL="/usr/local/sbin/nexus-llama-model"
install --owner=root --group=root --mode=0755 "$SCRIPT_DIR/nexus-llama-model.sh" "$LLAMA_MODEL"
install --owner=root --group=root --mode=0755 "$SCRIPT_DIR/nexus-llama-start.sh" /usr/local/sbin/nexus-llama-start
if [[ ! -e /etc/agent-lab/tool-denylist ]]; then
    install -d --mode=0755 /etc/agent-lab
    install --owner=root --group=root --mode=0644 "$SCRIPT_DIR/../deploy/tool-denylist" /etc/agent-lab/tool-denylist
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s ALL=(root) NOPASSWD: %s --collect --on-active=2 systemctl restart agent-api\n' "$SERVICE_USER" "$SYSTEMD_RUN" > "$tmp"
printf '%s ALL=(root) NOPASSWD: %s stop llama-server, %s start llama-server\n' "$SERVICE_USER" "$SYSTEMCTL" "$SYSTEMCTL" >> "$tmp"
printf '%s ALL=(root) NOPASSWD: %s restart nexus-mailbox, %s restart llama-server\n' "$SERVICE_USER" "$SYSTEMCTL" "$SYSTEMCTL" >> "$tmp"
printf '%s ALL=(root) NOPASSWD: %s --collect --on-active=60 systemctl reboot\n' "$SERVICE_USER" "$SYSTEMD_RUN" >> "$tmp"
printf '%s ALL=(root) NOPASSWD: %s\n' "$SERVICE_USER" "$INSTALL_TOOL" >> "$tmp"
printf '%s ALL=(root) NOPASSWD: %s\n' "$SERVICE_USER" "$LLAMA_MODEL" >> "$tmp"
visudo -cq -f "$tmp"
install --mode=0440 "$tmp" "$SUDOERS_FILE"

echo "$LOG_PREFIX $SERVICE_USER may schedule an agent-api restart, stop/start llama-server restart nexus-mailbox/llama-server, schedule a reboot, install tools and manage the local model ($SUDOERS_FILE)."
