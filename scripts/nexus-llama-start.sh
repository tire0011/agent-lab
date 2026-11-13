#!/usr/bin/env bash
# nexus-llama-start.sh - ExecStart of llama-server (#673).
# Reads /etc/agent-lab/llama.env (written only by nexus-llama-model, as root)
# as plain KEY=VALUE lines - never sourced - and starts llama-server with it.

set -euo pipefail

CONF="${NEXUS_LLAMA_CONF:-/etc/agent-lab/llama.env}"
MODEL_DIR="/opt/local-models"
MODEL="qwen2.5-coder-7b-instruct-q4_k_m.gguf"
DRAFT_MODEL=""
DRAFT_MAX="16"
CTX="8192"
THREADS="2"
PARALLEL="1"
PORT="8080"

if [[ -r "$CONF" ]]; then
    while IFS='=' read -r key value; do
        case "$key" in
            MODEL_DIR) MODEL_DIR="$value" ;;
            MODEL) MODEL="$value" ;;
            DRAFT_MODEL) DRAFT_MODEL="$value" ;;
            DRAFT_MAX) DRAFT_MAX="$value" ;;
            CTX) CTX="$value" ;;
            THREADS) THREADS="$value" ;;
            PARALLEL) PARALLEL="$value" ;;
            PORT) PORT="$value" ;;
        esac
    done < <(grep -E '^[A-Z_]+=' "$CONF")
fi

args=(--model "$MODEL_DIR/$MODEL" --host 127.0.0.1 --port "$PORT"
      --threads "$THREADS" --ctx-size "$CTX" --parallel "$PARALLEL")
if [[ -n "$DRAFT_MODEL" ]]; then
    args+=(--model-draft "$MODEL_DIR/$DRAFT_MODEL" --draft-max "$DRAFT_MAX")
fi
exec "${LLAMA_SERVER_BIN:-$(command -v llama-server)}" "${args[@]}"
