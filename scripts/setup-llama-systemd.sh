#!/usr/bin/env bash
# setup-llama-systemd.sh
# Creates and enables a systemd service that runs llama-server
# (llama.cpp native server, OpenAI-compatible REST API on port 8080).
#
# Usage:
#   sudo bash scripts/setup-llama-systemd.sh
#
# After setup:
#   systemctl status llama-server
#   journalctl -u llama-server -f

set -euo pipefail

LOG_PREFIX="[setup-llama-systemd]"

# ── Configuration ─────────────────────────────────────────────────────────────
# Override any of these with environment variables before running the script.
SERVICE_NAME="llama-server"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Model file path.  Set LOCAL_MODEL_PATH to the directory and MODEL_FILE to
# the filename, or set MODEL_FULL_PATH directly to skip the join.
MODEL_DIR="${LOCAL_MODEL_PATH:-/opt/local-models}"
MODEL_FILE="${MODEL_FILE:-qwen2.5-coder-7b-instruct-q4_k_m.gguf}"
MODEL_FULL_PATH="${MODEL_FULL_PATH:-${MODEL_DIR}/${MODEL_FILE}}"

SERVICE_USER="${LOCAL_MODEL_SERVICE_USER:-ubuntu}"

# B2ms has 4 vCPU — use all of them.
N_THREADS="${LOCAL_MODEL_THREADS:-4}"
# Context window (tokens).  8192 is conservative for a 7B Q4_K_M on 8 GB RAM.
CTX_SIZE="${LOCAL_MODEL_CONTEXT:-8192}"
# Server port (OpenAI-compatible API).
SERVER_PORT="${LOCAL_MODEL_PORT:-8080}"
# One slot = one concurrent request; avoids memory pressure on CPU-only.
N_PARALLEL="${LOCAL_MODEL_PARALLEL:-1}"

echo "$LOG_PREFIX Configuring systemd service '$SERVICE_NAME'  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Model path   : $MODEL_FULL_PATH"
echo "$LOG_PREFIX Port         : 127.0.0.1:$SERVER_PORT"
echo "$LOG_PREFIX Context      : $CTX_SIZE tokens"
echo "$LOG_PREFIX Threads      : $N_THREADS"
echo "$LOG_PREFIX Parallel slots: $N_PARALLEL"

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

# ── Create systemd service unit ───────────────────────────────────────────────
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=llama-server — local OpenAI-compatible inference (${MODEL_FILE})
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${MODEL_DIR}
ExecStart=${LLAMA_SERVER_BIN} \\
    --model ${MODEL_FULL_PATH} \\
    --host 127.0.0.1 \\
    --port ${SERVER_PORT} \\
    --threads ${N_THREADS} \\
    --ctx-size ${CTX_SIZE} \\
    --parallel ${N_PARALLEL} \\
    --no-mmap
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

