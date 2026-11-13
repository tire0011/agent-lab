#!/usr/bin/env bash
# fetch-draft-model.sh - download the speculative-decoding draft model (#669).
#
# Default: Qwen2.5-Coder-0.5B-Instruct Q8_0 (~0.5 GB), same tokenizer as the
# Qwen2.5-Coder-7B main model, into the local models directory.
#
#   bash scripts/fetch-draft-model.sh
#   sudo LLAMA_DRAFT_MODEL_FILE=qwen2.5-coder-0.5b-instruct-q8_0.gguf \
#        bash scripts/setup-llama-systemd.sh
#
# Integrity: the default official artifact is checked against its reviewed,
# repository-pinned sha256. Custom URL/repository/file selections must provide
# DRAFT_MODEL_SHA256 explicitly; a hash is never learned from the download
# source itself. A mismatch deletes the partial download.
# An existing file is verified the same way instead of being downloaded again.
#
# Exit codes: 0 ok, 1 download/network error, 3 checksum missing or mismatch.

set -euo pipefail

LOG_PREFIX="[fetch-draft-model]"

MODEL_DIR="${LOCAL_MODEL_PATH:-/opt/local-models}"
DEFAULT_REPO="Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF"
DEFAULT_FILE="qwen2.5-coder-0.5b-instruct-q8_0.gguf"
# Official Qwen artifact at repository commit ebb2015119c907b064c512bf053e945850b5875f.
DEFAULT_SHA256="e1a77721fa97d412f121878223eec81fb4ae6f271e18f922d746711f67b344d1"
HF_REPO="${DRAFT_HF_REPO:-$DEFAULT_REPO}"
DRAFT_FILE="${DRAFT_MODEL_FILE:-$DEFAULT_FILE}"
URL="${DRAFT_MODEL_URL:-https://huggingface.co/${HF_REPO}/resolve/main/${DRAFT_FILE}}"
EXPECTED="${DRAFT_MODEL_SHA256:-}"
DEST="${MODEL_DIR}/${DRAFT_FILE}"

if [[ ! "$DRAFT_FILE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.gguf$ ]]; then
    echo "$LOG_PREFIX ERROR: DRAFT_MODEL_FILE must be a plain .gguf file name." >&2
    exit 1
fi

if [[ -z "$EXPECTED" && "$HF_REPO" == "$DEFAULT_REPO" && "$DRAFT_FILE" == "$DEFAULT_FILE" \
      && -z "${DRAFT_MODEL_URL+x}" ]]; then
    EXPECTED="$DEFAULT_SHA256"
fi
EXPECTED="$(printf '%s' "$EXPECTED" | tr 'A-F' 'a-f')"
if [[ ! "$EXPECTED" =~ ^[0-9a-f]{64}$ ]]; then
    echo "$LOG_PREFIX ERROR: custom draft artifacts require DRAFT_MODEL_SHA256." >&2
    exit 3
fi
echo "$LOG_PREFIX Model    : $HF_REPO/$DRAFT_FILE"
echo "$LOG_PREFIX Dest     : $DEST"
echo "$LOG_PREFIX sha256   : $EXPECTED"

verify() {
    local actual
    actual="$(sha256sum "$1" | awk '{print $1}')"
    if [[ "$actual" != "$EXPECTED" ]]; then
        echo "$LOG_PREFIX ERROR: sha256 mismatch for $1 (got $actual)." >&2
        return 1
    fi
}

mkdir -p "$MODEL_DIR"
if [[ -f "$DEST" ]]; then
    if verify "$DEST"; then
        echo "$LOG_PREFIX Already present and verified: $DEST"
        exit 0
    fi
    exit 3
fi

TMP="${MODEL_DIR}/.${DRAFT_FILE}.part"
if ! curl -fL --retry 3 --retry-delay 10 --continue-at - -o "$TMP" "$URL"; then
    echo "$LOG_PREFIX ERROR: download failed: $URL" >&2
    exit 1
fi
if ! verify "$TMP"; then
    rm -f "$TMP"
    exit 3
fi
chmod 0644 "$TMP"
mv -f "$TMP" "$DEST"
echo "$LOG_PREFIX Downloaded and verified: $DEST ($(du -sh "$DEST" | cut -f1))"
echo "$LOG_PREFIX Next: LLAMA_DRAFT_MODEL_FILE=$DRAFT_FILE bash scripts/setup-llama-systemd.sh"
echo "$LOG_PREFIX   or: nexus-llama-model switch DRAFT_MODEL=$DRAFT_FILE DRAFT_MAX=16 DRAFT_MIN=4"
