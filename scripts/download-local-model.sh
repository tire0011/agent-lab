#!/usr/bin/env bash
# download-local-model.sh
# Downloads a GGUF local model (default: Qwen2.5-Coder-7B-Instruct Q4_K_M).
# Designed to survive an SSH session close:
#
#   nohup bash scripts/download-local-model.sh > /tmp/download-local-model.log 2>&1 &
#   echo "Download started, PID=$!"
#
# Progress can be monitored:
#   tail -f /tmp/download-local-model.log
#
# All settings are configurable via environment variables; no code changes
# needed to switch to a different model.

set -euo pipefail

LOG_PREFIX="[download-local-model]"

# ── Configuration ─────────────────────────────────────────────────────────────
# Directory where the model file will be stored.
MODEL_DIR="${LOCAL_MODEL_PATH:-/opt/local-models}"

# Hugging Face repository that hosts the GGUF file.
# Default: Qwen2.5-Coder-7B-Instruct Q4_K_M
#   Disk: ~4.7 GB   RAM: ~5.5 GB  — fits comfortably on a B2ms (8 GB RAM).
HF_REPO="${HF_REPO:-Qwen/Qwen2.5-Coder-7B-Instruct-GGUF}"
MODEL_FILE="${MODEL_FILE:-qwen2.5-coder-7b-instruct-q4_k_m.gguf}"
HF_BASE="https://huggingface.co/${HF_REPO}/resolve/main"
MODEL_URL="${MODEL_URL:-${HF_BASE}/${MODEL_FILE}}"
DEST="${MODEL_DIR}/${MODEL_FILE}"

echo "$LOG_PREFIX Starting model download  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Model  : ${HF_REPO}/${MODEL_FILE}"
echo "$LOG_PREFIX Source : $MODEL_URL"
echo "$LOG_PREFIX Dest   : $DEST"

mkdir -p "$MODEL_DIR"

if [[ -f "$DEST" ]]; then
    echo "$LOG_PREFIX Model file already exists at $DEST – skipping download."
    exit 0
fi

# Use a temporary file so a partial download is never mistaken for a complete one.
TMP_DEST="${DEST}.downloading"

if command -v curl &>/dev/null; then
    echo "$LOG_PREFIX Using curl with resume support…"
    curl \
        --location \
        --continue-at - \
        --retry 5 \
        --retry-delay 10 \
        --output "$TMP_DEST" \
        --progress-bar \
        "$MODEL_URL"
elif command -v wget &>/dev/null; then
    echo "$LOG_PREFIX Using wget with resume support…"
    wget \
        --continue \
        --retry-connrefused \
        --waitretry=10 \
        --tries=5 \
        --output-document="$TMP_DEST" \
        "$MODEL_URL"
else
    echo "$LOG_PREFIX ERROR: Neither curl nor wget is available." >&2
    exit 1
fi

# Verify the file has non-trivial size (a GGUF must be at least a few MB).
FILE_SIZE=$(stat -c%s "$TMP_DEST" 2>/dev/null || stat -f%z "$TMP_DEST")
if [[ "$FILE_SIZE" -lt 1000000 ]]; then
    echo "$LOG_PREFIX ERROR: Downloaded file is suspiciously small ($FILE_SIZE bytes). Aborting." >&2
    rm -f "$TMP_DEST"
    exit 1
fi

mv "$TMP_DEST" "$DEST"

echo "$LOG_PREFIX ✅ Model downloaded successfully  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX File: $DEST  ($(du -sh "$DEST" | cut -f1))"
echo "$LOG_PREFIX Next step: run scripts/setup-llama-systemd.sh"

