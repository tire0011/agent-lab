#!/usr/bin/env bash
# Compare small local models on the Nexus controller (#302).
#
#   /agent/scripts/model-benchmark.sh <branch> [name...]   # run all or the named candidates
#   /agent/scripts/model-benchmark.sh --check               # only check the download URLs
#
# Runs as the service user (e.g. from a mailbox command): stopping and starting
# the llama-server unit uses the sudo rules from setup-nexus-permissions.sh.
# Run as root it works too. One candidate per call keeps a run short.
#
# Run it while Nexus is idle: for each candidate the llama-server unit is
# stopped and the candidate is served on the same port with the same flags,
# so a task arriving meanwhile would run on the candidate. For each model:
#   1. use it from MODEL_DIR, or download it to CANDIDATE_DIR (download-local-model.sh)
#   2. start it, wait for /v1/models
#   3. run live_tests/test_model_benchmark_controller.py via nexus-live-check.sh
#      as the service user
# The llama-server unit is always started again at the end, also on failure.
# Results: $OUT_DIR/<timestamp>/<model>.log and summary.txt.
#
# Candidates: CANDIDATES (newline separated "name|hf_repo|file") overrides the
# list below. Downloaded candidates are deleted after their run (the disk has
# room for one or two, not all); KEEP_MODELS=1 keeps them. Models already in
# MODEL_DIR, like the one the llama-server unit serves, are never deleted.

set -u

REPO=${AGENT_REPO:-/agent}
MODEL_DIR=${LOCAL_MODEL_PATH:-/opt/local-models}
PORT=${LOCAL_MODEL_PORT:-8080}
THREADS=${LOCAL_MODEL_THREADS:-2}
CTX=${LOCAL_MODEL_CONTEXT:-8192}
SERVICE_USER=${LOCAL_MODEL_SERVICE_USER:-agent}
OUT_DIR=${OUT_DIR:-/var/tmp/model-benchmark}
CANDIDATE_DIR=${CANDIDATE_DIR:-$OUT_DIR/models}
TEST=live_tests/test_model_benchmark_controller.py
KEEP_MODELS=${KEEP_MODELS:-0}

DEFAULT_CANDIDATES="baseline-qwen2.5-coder-7b|Qwen/Qwen2.5-Coder-7B-Instruct-GGUF|qwen2.5-coder-7b-instruct-q4_k_m.gguf
gemma-3-4b|ggml-org/gemma-3-4b-it-GGUF|gemma-3-4b-it-Q4_K_M.gguf
qwen3-4b-2507|unsloth/Qwen3-4B-Instruct-2507-GGUF|Qwen3-4B-Instruct-2507-Q4_K_M.gguf
llama-3.1-8b|bartowski/Meta-Llama-3.1-8B-Instruct-GGUF|Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
phi-4-mini|bartowski/microsoft_Phi-4-mini-instruct-GGUF|microsoft_Phi-4-mini-instruct-Q4_K_M.gguf
qwen3-8b|Qwen/Qwen3-8B-GGUF|Qwen3-8B-Q4_K_M.gguf
granite-3.3-8b|ibm-granite/granite-3.3-8b-instruct-GGUF|granite-3.3-8b-instruct-Q4_K_M.gguf
ministral-8b|bartowski/Ministral-8B-Instruct-2410-GGUF|Ministral-8B-Instruct-2410-Q4_K_M.gguf"
CANDIDATES=${CANDIDATES:-$DEFAULT_CANDIDATES}

url_of() { echo "https://huggingface.co/$1/resolve/main/$2"; }

if [ "${1:-}" = "--check" ]; then
    while IFS='|' read -r name repo file; do
        [ -n "$name" ] || continue
        code=$(curl -s -o /dev/null -w '%{http_code}' -I -L --max-time 30 "$(url_of "$repo" "$file")")
        size=$(curl -s -I -L --max-time 30 "$(url_of "$repo" "$file")" | awk 'tolower($1)=="content-length:" {v=$2} END {printf "%d", v/1048576}')
        echo "$code ${size}MiB $name $repo/$file"
    done <<< "$CANDIDATES"
    df -h "$MODEL_DIR" | tail -1
    exit 0
fi

if [ $# -lt 1 ]; then
    echo "usage: $0 <branch> [name...]   |   $0 --check" >&2
    exit 2
fi
BRANCH=$1
shift
ONLY=" $* "

if [ "$(id -u)" -eq 0 ]; then
    unit() { systemctl "$1" llama-server; }
    as_service_user() { runuser -u "$SERVICE_USER" -- "$@"; }
else
    # Granted by scripts/setup-nexus-permissions.sh.
    unit() { sudo -n systemctl "$1" llama-server; }
    as_service_user() { "$@"; }
fi

RUN_DIR="$OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR" "$CANDIDATE_DIR"
chmod 755 "$OUT_DIR" "$RUN_DIR" "$CANDIDATE_DIR"
SUMMARY="$RUN_DIR/summary.txt"
SERVER_PID=""

restore() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    unit start
    echo "llama-server unit: $(systemctl is-active llama-server)" | tee -a "$SUMMARY"
}

echo "llama-server: $(llama-server --version 2>&1 | head -1)" | tee "$SUMMARY"
echo "branch: $BRANCH" | tee -a "$SUMMARY"
if ! unit stop; then
    echo "ERROR: cannot stop llama-server (missing sudo rule? run setup-nexus-permissions.sh)" | tee -a "$SUMMARY" >&2
    exit 2
fi
trap restore EXIT
# A timeout or Ctrl-C must still restore the unit.
trap 'exit 143' TERM INT

while IFS='|' read -r name repo file; do
    [ -n "$name" ] || continue
    if [ "$ONLY" != "  " ] && [ "${ONLY#* $name }" = "$ONLY" ]; then continue; fi
    log="$RUN_DIR/$name.log"
    echo "=== $name ($file)" | tee -a "$SUMMARY"

    downloaded=""
    model="$MODEL_DIR/$file"
    if [ ! -f "$model" ]; then
        model="$CANDIDATE_DIR/$file"
        [ -f "$model" ] || downloaded=1
        if ! HF_REPO="$repo" MODEL_FILE="$file" LOCAL_MODEL_PATH="$CANDIDATE_DIR" \
                bash "$REPO/scripts/download-local-model.sh" >"$log" 2>&1; then
            echo "  download failed (see $log)" | tee -a "$SUMMARY"
            continue
        fi
    fi

    as_service_user llama-server --model "$model" \
        --host 127.0.0.1 --port "$PORT" --threads "$THREADS" --ctx-size "$CTX" --parallel 1 \
        >>"$log" 2>&1 &
    SERVER_PID=$!
    ready=""
    for _ in $(seq 1 90); do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
        if curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null; then ready=1; break; fi
        sleep 2
    done
    if [ -z "$ready" ]; then
        echo "  model did not load (see $log)" | tee -a "$SUMMARY"
    else
        as_service_user env AGENT_DB_PATH="${AGENT_DB_PATH:-/var/lib/nexus/agent.db}" \
            "$REPO/scripts/nexus-live-check.sh" "$BRANCH" "$TEST" >>"$log" 2>&1
        # One JSON line per scenario: scenario, action, verdict, seconds.
        sed -n '/live results (json)/,$p' "$log" | grep '^{' | python3 -c '
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    print("  %-20s %-6s %-22s %6.1fs" % (r.get("scenario"), "PASS" if r.get("verdict") else "FAIL",
          r.get("action"), r.get("llm_seconds") or r.get("seconds") or 0))
' | tee -a "$SUMMARY"
    fi

    kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""
    if [ -n "$downloaded" ] && [ "$KEEP_MODELS" != 1 ]; then
        rm -f "$model"
    fi
done <<< "$CANDIDATES"

chmod 644 "$RUN_DIR"/*
echo "results: $RUN_DIR" | tee -a "$SUMMARY"
