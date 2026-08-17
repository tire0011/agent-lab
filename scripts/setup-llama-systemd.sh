#!/usr/bin/env bash
# setup-llama-systemd.sh
# Creates and enables a systemd service that runs a llama-cpp-python HTTP
# server (OpenAI-compatible REST API on port 8080) automatically on boot.
#
# Usage:
#   sudo bash scripts/setup-llama-systemd.sh
#
# After setup:
#   systemctl status llama-cpp
#   journalctl -u llama-cpp -f

set -euo pipefail

LOG_PREFIX="[setup-llama-systemd]"

# ── Configuration ─────────────────────────────────────────────────────────────
# PREREQUISITE: This script requires a Standard_B2ms VM (4 vCPU, 8 GB RAM).
# The Llama 2 13B Q4_K_M model requires ~7.8 GB RAM.
# Do NOT run this on a Standard_B1s (1 vCPU, 1 GB RAM) — the service will
# be killed immediately by the OOM killer.
# Upgrade the VM in the Azure portal before running this script.
SERVICE_NAME="llama-cpp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="${LLAMA_VENV:-/opt/llama-venv}"
MODEL_DIR="${LLAMA_MODEL_DIR:-/opt/llama-models}"
MODEL_FILE="${MODEL_FILE:-llama-2-13b-chat.Q4_K_M.gguf}"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
SERVICE_USER="${LLAMA_SERVICE_USER:-ubuntu}"

# B2ms has 4 vCPU – use 4 threads for optimal throughput.
N_THREADS="${LLAMA_N_THREADS:-4}"
# Context window (tokens); 4096 is safe for 8 GB RAM with Q4_K_M.
CTX_SIZE="${LLAMA_CTX_SIZE:-4096}"
# llama-cpp-python server port (OpenAI-compatible).
SERVER_PORT="${LLAMA_SERVER_PORT:-8080}"
# Parallel request slots (keep low on CPU-only to avoid thrashing).
N_PARALLEL="${LLAMA_N_PARALLEL:-1}"

echo "$LOG_PREFIX Configuring systemd service '$SERVICE_NAME'  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Model path : $MODEL_PATH"
echo "$LOG_PREFIX Port       : $SERVER_PORT"
echo "$LOG_PREFIX Threads    : $N_THREADS"

if [[ ! -f "$MODEL_PATH" ]]; then
    echo "$LOG_PREFIX WARNING: Model file not found at $MODEL_PATH." >&2
    echo "$LOG_PREFIX Run scripts/download-llama2-13b.sh first, or set MODEL_FILE accordingly." >&2
    echo "$LOG_PREFIX Continuing with service installation anyway…" >&2
fi

# ── Create systemd service unit ───────────────────────────────────────────────
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=llama-cpp-python OpenAI-compatible HTTP Server (Llama 2 13B)
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${MODEL_DIR}
ExecStart=${VENV_DIR}/bin/python -m llama_cpp.server \\
    --model ${MODEL_PATH} \\
    --host 127.0.0.1 \\
    --port ${SERVER_PORT} \\
    --n_threads ${N_THREADS} \\
    --n_ctx ${CTX_SIZE} \\
    --n_parallel ${N_PARALLEL} \\
    --chat_format llama-2
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
# Prevent the service from acquiring too much memory; kill it before OOM.
MemoryMax=7G

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
