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
# Integrity: the file is accepted only if its sha256 matches. The expected value
# is DRAFT_MODEL_SHA256 if set (pin it once known); otherwise the LFS sha256
# that huggingface.co reports in the X-Linked-Etag header of the file. Without
# an expected value the download is refused. A mismatch deletes the download.
# An existing file is verified the same way instead of being downloaded again.
#
# Exit codes: 0 ok, 1 download/network error, 3 checksum missing or mismatch.

set -euo pipefail

LOG_PREFIX="[fetch-draft-model]"

MODEL_DIR="${LOCAL_MODEL_PATH:-/opt/local-models}"
HF_REPO="${DRAFT_HF_REPO:-Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF}"
DRAFT_FILE="${DRAFT_MODEL_FILE:-qwen2.5-coder-0.5b-instruct-q8_0.gguf}"
URL="${DRAFT_MODEL_URL:-https://huggingface.co/${HF_REPO}/resolve/main/${DRAFT_FILE}}"
EXPECTED="${DRAFT_MODEL_SHA256:-}"
DEST="${MODEL_DIR}/${DRAFT_FILE}"

if [[ ! "$DRAFT_FILE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.gguf$ ]]; then
    echo "$LOG_PREFIX ERROR: DRAFT_MODEL_FILE must be a plain .gguf file name." >&2
    exit 1
fi

if [[ -z "$EXPECTED" ]]; then
    # huggingface.co answers HEAD on a resolve URL of an LFS file with
    # X-Linked-Etag: "<sha256>" (before following the CDN redirect).
    EXPECTED="$(curl -fsI "$URL" 2>/dev/null | tr -d '\r' \
        | awk 'tolower($1)=="x-linked-etag:"{gsub(/"/, "", $2); print tolower($2)}' | tail -1 || true)"
fi
EXPECTED="$(printf '%s' "$EXPECTED" | tr 'A-F' 'a-f')"
if [[ ! "$EXPECTED" =~ ^[0-9a-f]{64}$ ]]; then
    echo "$LOG_PREFIX ERROR: no expected sha256 (set DRAFT_MODEL_SHA256)." >&2
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
