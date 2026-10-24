#!/usr/bin/env bash
# setup-reboot-check.sh
# Installs and enables the timer that checks every 2 hours whether the VM needs
# a reboot (#332; deploy/nexus-reboot-check.{service,timer} run
# nexus_reboot_check.py). The reboot itself goes through the sudoers rule from
# setup-nexus-permissions.sh.
# Idempotent: nexus-refresh.ps1 runs it on every refresh, so a new machine
# gets the timer with its first refresh.
#
# Usage:
#   sudo bash scripts/setup-reboot-check.sh

set -euo pipefail

LOG_PREFIX="[setup-reboot-check]"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
UNITS=(nexus-reboot-check.service nexus-reboot-check.timer)

if [[ "${EUID}" -ne 0 ]]; then
    echo "$LOG_PREFIX ERROR: run this script as root (sudo)." >&2
    exit 1
fi

for unit in "${UNITS[@]}"; do
    install --mode=0644 "$REPO_ROOT/deploy/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now nexus-reboot-check.timer

echo "$LOG_PREFIX nexus-reboot-check.timer installed and enabled (every 2 hours)."
