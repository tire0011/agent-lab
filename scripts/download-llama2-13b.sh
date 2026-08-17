#!/usr/bin/env bash
# download-llama2-13b.sh
# Downloads the Llama 2 13B GGUF model (Q4_K_M quantisation) in the background.
# Designed to survive an SSH session close on iPhone:
#
#   nohup bash scripts/download-llama2-13b.sh > /tmp/download-llama2-13b.log 2>&1 &
#   echo "Download started, PID=$!"
#
# Progress can be monitored:
#   tail -f /tmp/download-llama2-13b.log

set -euo pipefail

LOG_PREFIX="[download-llama2-13b]"

# ── Configuration ─────────────────────────────────────────────────────────────
MODEL_DIR="${LLAMA_MODEL_DIR:-/opt/llama-models}"
# Q4_K_M is the recommended quantisation for B2ms (8 GB RAM):
# ~7.4 GB on disk, ~7.8 GB in RAM  →  leaves ~200 MB headroom.
MODEL_FILE="${MODEL_FILE:-llama-2-13b-chat.Q4_K_M.gguf}"
# Hugging Face repository that hosts the GGUF file.
HF_REPO="${HF_REPO:-TheBloke/Llama-2-13B-chat-GGUF}"
HF_BASE="https://huggingface.co/${HF_REPO}/resolve/main"
MODEL_URL="${MODEL_URL:-${HF_BASE}/${MODEL_FILE}}"
DEST="${MODEL_DIR}/${MODEL_FILE}"

echo "$LOG_PREFIX Starting model download  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Source : $MODEL_URL"
echo "$LOG_PREFIX Destination: $DEST"

mkdir -p "$MODEL_DIR"

if [[ -f "$DEST" ]]; then
    echo "$LOG_PREFIX Model file already exists at $DEST – skipping download."
    exit 0
fi

# Use a temporary file so a partial download is never treated as complete.
TMP_DEST="${DEST}.downloading"

# Resume support: if a partial download exists, try to resume it.
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

# Verify the file has non-zero size.
FILE_SIZE=$(stat -c%s "$TMP_DEST" 2>/dev/null || stat -f%z "$TMP_DEST")
if [[ "$FILE_SIZE" -lt 1000000 ]]; then
    echo "$LOG_PREFIX ERROR: Downloaded file appears too small ($FILE_SIZE bytes). Aborting." >&2
    rm -f "$TMP_DEST"
    exit 1
fi

mv "$TMP_DEST" "$DEST"

echo "$LOG_PREFIX ✅ Model downloaded successfully  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX File: $DEST  ($(du -sh "$DEST" | cut -f1))"
echo "$LOG_PREFIX Next step: run scripts/setup-llama-systemd.sh"
