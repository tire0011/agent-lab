#!/usr/bin/env bash
# setup-swap.sh
# Creates swap on the ephemeral Azure resource disk and recreates it after
# deallocation or a host move.
#
# Usage:
#   sudo bash scripts/setup-swap.sh
#
# Optional overrides:
#   NEXUS_SWAP_SIZE_GIB=8
#   NEXUS_SWAP_MOUNT_POINT=/mnt
#   NEXUS_SWAPPINESS=10

set -euo pipefail

LOG_PREFIX="[setup-swap]"
SWAP_SIZE_GIB="${NEXUS_SWAP_SIZE_GIB:-8}"
MOUNT_POINT="${NEXUS_SWAP_MOUNT_POINT:-/mnt}"
SWAPPINESS="${NEXUS_SWAPPINESS:-10}"
SWAP_FILE="${MOUNT_POINT%/}/swapfile"
PER_BOOT_SCRIPT="/var/lib/cloud/scripts/per-boot/nexus-swap"
SYSCTL_FILE="/etc/sysctl.d/99-nexus-swap.conf"

if [[ "${EUID}" -ne 0 ]]; then
    echo "$LOG_PREFIX ERROR: run this script as root (sudo)." >&2
    exit 1
fi

if [[ ! "$SWAP_SIZE_GIB" =~ ^[1-9][0-9]*$ ]]; then
    echo "$LOG_PREFIX ERROR: NEXUS_SWAP_SIZE_GIB must be a positive integer." >&2
    exit 1
fi

if [[ ! "$SWAPPINESS" =~ ^[0-9]+$ ]] || (( SWAPPINESS > 200 )); then
    echo "$LOG_PREFIX ERROR: NEXUS_SWAPPINESS must be between 0 and 200." >&2
    exit 1
fi

if ! findmnt --mountpoint "$MOUNT_POINT" >/dev/null; then
    echo "$LOG_PREFIX ERROR: $MOUNT_POINT is not a mounted filesystem." >&2
    exit 1
fi

root_source="$(findmnt --noheadings --output SOURCE /)"
swap_source="$(findmnt --noheadings --output SOURCE "$MOUNT_POINT")"
if [[ "$swap_source" == "$root_source" ]]; then
    echo "$LOG_PREFIX ERROR: refusing to place swap on the OS filesystem ($root_source)." >&2
    exit 1
fi

if [[ -f /etc/waagent.conf ]] &&
    grep -Eiq '^[[:space:]]*ResourceDisk\.EnableSwap[[:space:]]*=[[:space:]]*y' /etc/waagent.conf; then
    echo "$LOG_PREFIX ERROR: waagent swap management is enabled; use only one swap manager." >&2
    exit 1
fi

if [[ -L "$SWAP_FILE" ]]; then
    echo "$LOG_PREFIX ERROR: refusing to use symbolic link $SWAP_FILE." >&2
    exit 1
fi

required_bytes=$((SWAP_SIZE_GIB * 1024 * 1024 * 1024))
if [[ -e "$SWAP_FILE" ]]; then
    if [[ ! -f "$SWAP_FILE" ]]; then
        echo "$LOG_PREFIX ERROR: $SWAP_FILE exists but is not a regular file." >&2
        exit 1
    fi
    actual_bytes="$(stat --format='%s' "$SWAP_FILE")"
    if (( actual_bytes != required_bytes )); then
        echo "$LOG_PREFIX ERROR: existing $SWAP_FILE has an unexpected size." >&2
        echo "$LOG_PREFIX Expected ${SWAP_SIZE_GIB} GiB; remove it manually before resizing." >&2
        exit 1
    fi
    if [[ "$(blkid --probe --output value --match-tag TYPE "$SWAP_FILE" 2>/dev/null || true)" != "swap" ]]; then
        echo "$LOG_PREFIX ERROR: existing $SWAP_FILE has no swap signature." >&2
        exit 1
    fi
else
    available_bytes="$(df --block-size=1 --output=avail "$MOUNT_POINT" | tail -n 1 | tr -d ' ')"
    if (( available_bytes < required_bytes )); then
        echo "$LOG_PREFIX ERROR: insufficient free space on $MOUNT_POINT." >&2
        exit 1
    fi

    echo "$LOG_PREFIX Creating ${SWAP_SIZE_GIB} GiB swap file on $swap_source..."
    fallocate --length "${SWAP_SIZE_GIB}G" "$SWAP_FILE"
    chmod 0600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
fi

chmod 0600 "$SWAP_FILE"
if ! swapon --show=NAME --noheadings | grep -Fxq "$SWAP_FILE"; then
    swapon "$SWAP_FILE"
fi

printf 'vm.swappiness=%s\n' "$SWAPPINESS" > "$SYSCTL_FILE"
sysctl --write "vm.swappiness=$SWAPPINESS" >/dev/null

script_path="$(readlink -f "${BASH_SOURCE[0]}")"
if [[ "$script_path" != "$PER_BOOT_SCRIPT" ]]; then
    install -D --mode=0700 "$script_path" "$PER_BOOT_SCRIPT"
fi

echo "$LOG_PREFIX Swap configured and per-boot recreation installed."
swapon --show "$SWAP_FILE"
free -h
