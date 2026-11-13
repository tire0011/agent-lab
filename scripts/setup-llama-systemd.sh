#!/usr/bin/env bash
# setup-llama-systemd.sh
# Creates and enables a systemd service that runs llama-server
# (llama.cpp native server, OpenAI-compatible REST API on port 8080).
#
# Usage:
#   sudo bash scripts/setup-llama-systemd.sh
#
# Speculative decoding (#669, optional):
#   sudo LLAMA_DRAFT_MODEL_FILE=qwen2.5-coder-0.5b-instruct-q8_0.gguf \
#        bash scripts/setup-llama-systemd.sh
# A bare name is looked up in the model directory; a path must point into it.
# If the file exists, DRAFT_MODEL/DRAFT_MAX=16/DRAFT_MIN=4 go into
# /etc/agent-lab/llama.env and nexus-llama-start adds
# --model-draft <file> --draft-max 16 --draft-min 4. If the variable is not
# supplied, an existing configuration is left alone. If it is explicitly empty
# or names an unusable file, persisted DRAFT_* keys are removed. Fetch the file
# with scripts/fetch-draft-model.sh.
#
# After setup:
#   systemctl status llama-server
#   journalctl -u llama-server -f

set -euo pipefail

LOG_PREFIX="[setup-llama-systemd]"

# ── Configuration ─────────────────────────────────────────────────────────────
# Override any of these with environment variables before running the script.
SERVICE_NAME="llama-server"
SERVICE_FILE="${LLAMA_SERVICE_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"

# Model file path.  Set LOCAL_MODEL_PATH to the directory and MODEL_FILE to
# the filename, or set MODEL_FULL_PATH directly to skip the join.
MODEL_DIR="${LOCAL_MODEL_PATH:-/opt/local-models}"
MODEL_FILE="${MODEL_FILE:-qwen2.5-coder-7b-instruct-q4_k_m.gguf}"
MODEL_FULL_PATH="${MODEL_FULL_PATH:-${MODEL_DIR}/${MODEL_FILE}}"

# Nexus services on the VM run as the dedicated agent user by default.
SERVICE_USER="${LOCAL_MODEL_SERVICE_USER:-agent}"

# Nexus VM has 2 vCPUs; use a conservative default of 2 threads.
N_THREADS="${LOCAL_MODEL_THREADS:-2}"
# Context window (tokens).  8192 is conservative for a 7B Q4_K_M on 8 GB RAM.
CTX_SIZE="${LOCAL_MODEL_CONTEXT:-8192}"
# Server port (OpenAI-compatible API).
SERVER_PORT="${LOCAL_MODEL_PORT:-8080}"
# One slot = one concurrent request; avoids memory pressure on CPU-only.
N_PARALLEL="${LOCAL_MODEL_PARALLEL:-1}"
# Optional draft model for speculative decoding (#669); empty = none.
DRAFT_SETTING_SUPPLIED=0
[[ ${LLAMA_DRAFT_MODEL_FILE+x} ]] && DRAFT_SETTING_SUPPLIED=1
DRAFT_MODEL_INPUT="${LLAMA_DRAFT_MODEL_FILE:-}"
DRAFT_MAX="${LLAMA_DRAFT_MAX:-16}"
DRAFT_MIN="${LLAMA_DRAFT_MIN:-4}"

echo "$LOG_PREFIX Configuring systemd service '$SERVICE_NAME'  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Model path   : $MODEL_FULL_PATH"
echo "$LOG_PREFIX Port         : 127.0.0.1:$SERVER_PORT"
echo "$LOG_PREFIX Context      : $CTX_SIZE tokens"
echo "$LOG_PREFIX Threads      : $N_THREADS"
echo "$LOG_PREFIX Parallel slots: $N_PARALLEL"

# nexus-llama-start resolves the draft as MODEL_DIR/DRAFT_MODEL, so only a file
# that exists in the model directory is accepted.
DRAFT_MODEL_NAME=""
if [[ -n "$DRAFT_MODEL_INPUT" ]]; then
    if [[ "$DRAFT_MODEL_INPUT" == */* ]]; then
        draft_path="$DRAFT_MODEL_INPUT"
    else
        draft_path="$MODEL_DIR/$DRAFT_MODEL_INPUT"
    fi
    if [[ ! -f "$draft_path" ]]; then
        echo "$LOG_PREFIX WARNING: draft model $draft_path not found - no speculative decoding." >&2
        echo "$LOG_PREFIX Run scripts/fetch-draft-model.sh first." >&2
    elif [[ "$(cd "$(dirname "$draft_path")" && pwd -P)" != "$(cd "$MODEL_DIR" 2>/dev/null && pwd -P)" ]]; then
        echo "$LOG_PREFIX WARNING: draft model must be in $MODEL_DIR - no speculative decoding." >&2
    elif ! [[ "$DRAFT_MAX" =~ ^[0-9]+$ && "$DRAFT_MIN" =~ ^[0-9]+$ ]]; then
        echo "$LOG_PREFIX WARNING: LLAMA_DRAFT_MAX/LLAMA_DRAFT_MIN must be integers - no speculative decoding." >&2
    else
        DRAFT_MODEL_NAME="$(basename "$draft_path")"
        echo "$LOG_PREFIX Draft model  : $draft_path (draft-max $DRAFT_MAX, draft-min $DRAFT_MIN)"
    fi
fi

# Warn if the model file does not exist yet (download can happen later).
if [[ ! -f "$MODEL_FULL_PATH" ]]; then
    echo "$LOG_PREFIX WARNING: Model file not found at $MODEL_FULL_PATH." >&2
    echo "$LOG_PREFIX Run scripts/download-local-model.sh first, or set MODEL_FULL_PATH." >&2
    echo "$LOG_PREFIX Continuing with service installation anyway…" >&2
fi

# Verify llama-server is installed.
if ! command -v llama-server &>/dev/null; then
    echo "$LOG_PREFIX ERROR: llama-server not found in PATH." >&2
    echo "$LOG_PREFIX Run scripts/install-llama-cpp.sh first." >&2
    exit 1
fi

LLAMA_SERVER_BIN="$(command -v llama-server)"

# ── Model configuration Nexus may change (#673) ──────────────────────────────
# The unit starts llama-server through nexus-llama-start, which reads
# /etc/agent-lab/llama.env. nexus-llama-model (sudoers) rewrites that file and
# rolls back a switch that leaves llama-server unhealthy. An existing file is
# kept; a new one keeps the context size of the unit it replaces.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_CONF="${NEXUS_LLAMA_CONF:-/etc/agent-lab/llama.env}"
START_BIN="${NEXUS_LLAMA_START_BIN:-/usr/local/sbin/nexus-llama-start}"
install --owner=root --group=root --mode=0755 "$SCRIPT_DIR/nexus-llama-start.sh" "$START_BIN"
draft_lines=()
if [[ -n "$DRAFT_MODEL_NAME" ]]; then
    draft_lines=("DRAFT_MODEL=${DRAFT_MODEL_NAME}" "DRAFT_MAX=${DRAFT_MAX}" "DRAFT_MIN=${DRAFT_MIN}")
fi
if [[ -f "$LLAMA_CONF" && $DRAFT_SETTING_SUPPLIED -eq 1 ]]; then
    # LLAMA_DRAFT_MODEL_FILE is authoritative when explicitly supplied. Rewrite
    # the file atomically so an empty, missing, outside-model-dir, or otherwise
    # invalid draft cannot leave stale speculative-decoding flags behind.
    tmp_conf="$(mktemp "${LLAMA_CONF}.tmp.XXXXXX")"
    trap 'rm -f "$tmp_conf"' EXIT
    grep -vE '^DRAFT_(MODEL|MAX|MIN)=' "$LLAMA_CONF" > "$tmp_conf" || true
    if [[ ${#draft_lines[@]} -gt 0 ]]; then
        printf '%s\n' "${draft_lines[@]}" >> "$tmp_conf"
    fi
    chmod 0644 "$tmp_conf"
    mv -f "$tmp_conf" "$LLAMA_CONF"
    trap - EXIT
    echo "$LOG_PREFIX Updated draft settings in $LLAMA_CONF"
elif [[ ! -f "$LLAMA_CONF" ]]; then
    if [[ -z "${LOCAL_MODEL_CONTEXT:-}" && -f "$SERVICE_FILE" ]]; then
        old_ctx="$(grep -oE -- '--ctx-size [0-9]+' "$SERVICE_FILE" | awk '{print $2}' | head -1 || true)"
        CTX_SIZE="${old_ctx:-$CTX_SIZE}"
    fi
    install -d --mode=0755 "$(dirname "$LLAMA_CONF")"
    tmp_conf="$(mktemp "${LLAMA_CONF}.tmp.XXXXXX")"
    trap 'rm -f "$tmp_conf"' EXIT
    printf '%s\n' "MODEL_DIR=${MODEL_DIR}" "MODEL=$(basename "$MODEL_FULL_PATH")" \
        "CTX=${CTX_SIZE}" "THREADS=${N_THREADS}" "PARALLEL=${N_PARALLEL}" "PORT=${SERVER_PORT}" \
        ${draft_lines[@]+"${draft_lines[@]}"} > "$tmp_conf"
    chmod 0644 "$tmp_conf"
    mv -f "$tmp_conf" "$LLAMA_CONF"
    trap - EXIT
    echo "$LOG_PREFIX Wrote $LLAMA_CONF (context $CTX_SIZE)"
fi

# ── Create systemd service unit ───────────────────────────────────────────────
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=llama-server — local OpenAI-compatible inference (model in ${LLAMA_CONF})
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${MODEL_DIR}
Environment=LLAMA_SERVER_BIN=${LLAMA_SERVER_BIN}
ExecStart=${START_BIN}
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
# Conservative memory cap: prevents the OOM killer from taking other services.
MemoryMax=6G

[Install]
WantedBy=multi-user.target
EOF

echo "$LOG_PREFIX Created $SERVICE_FILE"

# ── Enable and start the service ──────────────────────────────────────────────
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "$LOG_PREFIX ✅ Systemd service '$SERVICE_NAME' installed and started  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Check status : systemctl status $SERVICE_NAME"
echo "$LOG_PREFIX Follow logs  : journalctl -u $SERVICE_NAME -f"
echo "$LOG_PREFIX API base URL : http://127.0.0.1:${SERVER_PORT}/v1"
