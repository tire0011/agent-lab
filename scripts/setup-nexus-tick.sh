#!/usr/bin/env bash
# setup-nexus-tick.sh
# Installs and enables the initiative tick timer (#356; deploy/nexus-tick.
# {service,timer} run nexus_tick.py every 15 minutes). The tick starts at most
# one piece of work at a time, within its daily budget.
# Idempotent: nexus-refresh.ps1 runs it on every refresh.
#
# Usage:
#   sudo bash scripts/setup-nexus-tick.sh

set -euo pipefail

LOG_PREFIX="[setup-nexus-tick]"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="/etc/systemd/system"
UNITS=(nexus-tick.service nexus-tick.timer)

if [[ "${EUID}" -ne 0 ]]; then
    echo "$LOG_PREFIX ERROR: run this script as root (sudo)." >&2
    exit 1
fi

for unit in "${UNITS[@]}"; do
    install --mode=0644 "$REPO_ROOT/deploy/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now nexus-tick.timer

echo "$LOG_PREFIX nexus-tick.timer installed and enabled (every 15 minutes)."
