#!/usr/bin/env bash
# nexus-llama-model.sh - lets Nexus try other local models (#673). Runs as root
# through one sudoers rule (copied to /usr/local/sbin/nexus-llama-model).
#
#   nexus-llama-model list
#   nexus-llama-model download <hf-owner/repo> <file.gguf>
#   nexus-llama-model switch KEY=VALUE ...   (MODEL DRAFT_MODEL DRAFT_MAX DRAFT_MIN CTX THREADS PARALLEL)
#
# Downloads come only from huggingface.co, only .gguf, within a size limit and
# with disk space to spare. A switch that leaves llama-server unhealthy is
# rolled back to the previous configuration.

set -euo pipefail

CONF="${NEXUS_LLAMA_CONF:-/etc/agent-lab/llama.env}"
MODEL_DIR="${NEXUS_LLAMA_MODEL_DIR:-/opt/local-models}"
MAX_BYTES="${NEXUS_LLAMA_MAX_BYTES:-6442450944}"   # 6 GiB
SPARE_BYTES="${NEXUS_LLAMA_SPARE_BYTES:-2147483648}" # keep 2 GiB free
HEALTH_WAIT="${NEXUS_LLAMA_HEALTH_WAIT:-180}"
NAME_RE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$'

log() { logger -t nexus-llama-model "$*" 2>/dev/null || true; }
die() { echo "refused: $*" >&2; exit 2; }

conf_get() { grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

case "${1:-}" in
list)
    echo "models in $MODEL_DIR:"
    ls -1 "$MODEL_DIR" 2>/dev/null | grep -E '\.gguf$' | sed 's/^/  /' || true
    echo "current configuration:"
    sed 's/^/  /' "$CONF" 2>/dev/null || echo "  (none)"
    ;;
download)
    [[ $# -eq 3 ]] || die "usage: download <owner/repo> <file.gguf>"
    repo="$2"; file="$3"
    [[ "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "bad repository name"
    [[ "$file" =~ $NAME_RE && "$file" == *.gguf ]] || die "only a plain .gguf file name is allowed"
    url="https://huggingface.co/$repo/resolve/main/$file"
    size="$(curl -fsIL "$url" | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}')"
    [[ "$size" -gt 0 ]] || { echo "not found: $url" >&2; exit 1; }
    [[ "$size" -le "$MAX_BYTES" ]] || die "file is $size bytes, limit is $MAX_BYTES"
    free="$(df -B1 --output=avail "$MODEL_DIR" | tail -1 | tr -d ' ')"
    [[ "$free" -ge $((size + SPARE_BYTES)) ]] || die "not enough disk space ($free free, need $((size + SPARE_BYTES)))"
    tmp="$MODEL_DIR/.$file.part"
    curl -fL --retry 3 -o "$tmp" "$url"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$MODEL_DIR/$file"
    log "downloaded $repo/$file ($size bytes)"
    echo "downloaded: $file ($size bytes)"
    ;;
switch)
    shift
    [[ $# -ge 1 ]] || die "usage: switch KEY=VALUE ..."
    declare -A new
    for key in MODEL_DIR MODEL DRAFT_MODEL DRAFT_MAX DRAFT_MIN CTX THREADS PARALLEL PORT; do
        new[$key]="$(conf_get "$key")"
    done
    new[MODEL_DIR]="${new[MODEL_DIR]:-$MODEL_DIR}"
    for pair in "$@"; do
        key="${pair%%=*}"; value="${pair#*=}"
        case "$key" in
            MODEL) [[ "$value" =~ $NAME_RE && -f "$MODEL_DIR/$value" ]] || die "MODEL must be a downloaded .gguf in $MODEL_DIR" ;;
            DRAFT_MODEL) [[ -z "$value" || ( "$value" =~ $NAME_RE && -f "$MODEL_DIR/$value" ) ]] || die "DRAFT_MODEL must be empty or a downloaded .gguf" ;;
            DRAFT_MAX) [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 && "$value" -le 64 ]] || die "DRAFT_MAX must be 1-64" ;;
            DRAFT_MIN) [[ "$value" =~ ^[0-9]+$ && "$value" -le 64 ]] || die "DRAFT_MIN must be 0-64" ;;
            CTX) [[ "$value" =~ ^[0-9]+$ && "$value" -ge 512 && "$value" -le 65536 ]] || die "CTX must be 512-65536" ;;
            THREADS) [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 && "$value" -le "$(nproc)" ]] || die "THREADS must be 1-$(nproc)" ;;
            PARALLEL) [[ "$value" =~ ^[0-9]+$ && "$value" -ge 1 && "$value" -le 4 ]] || die "PARALLEL must be 1-4" ;;
            *) die "unknown key $key" ;;
        esac
        new[$key]="$value"
    done
    backup="$CONF.previous"
    [[ -f "$CONF" ]] && cp -p "$CONF" "$backup"
    tmpconf="$(mktemp)"
    for key in MODEL_DIR MODEL DRAFT_MODEL DRAFT_MAX DRAFT_MIN CTX THREADS PARALLEL PORT; do
        [[ -n "${new[$key]}" ]] && echo "$key=${new[$key]}"
    done > "$tmpconf"
    install -m 0644 "$tmpconf" "$CONF"; rm -f "$tmpconf"
    port="${new[PORT]:-8080}"
    systemctl restart llama-server
    for _ in $(seq 1 "$HEALTH_WAIT"); do
        if curl -fs "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            log "switched: $*"
            echo "switched: $*"
            exit 0
        fi
        sleep 1
    done
    if [[ -f "$backup" ]]; then
        cp -p "$backup" "$CONF"
        systemctl restart llama-server
    fi
    log "switch rolled back, llama-server not healthy: $*"
    echo "rolled back: llama-server did not become healthy with: $*" >&2
    exit 4
    ;;
*)
    die "usage: list | download <owner/repo> <file.gguf> | switch KEY=VALUE ..."
    ;;
esac
