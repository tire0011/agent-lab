#!/usr/bin/env bash
# Compare small local models on the Nexus controller (#302).
#
#   sudo /agent/scripts/model-benchmark.sh <branch>           # run the benchmark
#   /agent/scripts/model-benchmark.sh --check                 # only check the download URLs
#
# Run it while Nexus is idle: for each candidate the llama-server unit is
# stopped and the candidate is served on the same port with the same flags,
# so a task arriving meanwhile would run on the candidate. For each model:
#   1. download it to MODEL_DIR if missing (scripts/download-local-model.sh)
#   2. start it, wait for /v1/models
#   3. run live_tests/test_model_benchmark_controller.py via nexus-live-check.sh
#      as the service user
# The llama-server unit is always started again at the end, also on failure.
# Results: $OUT_DIR/<timestamp>/<model>.log and summary.txt.
#
# Candidates: CANDIDATES (newline separated "name|hf_repo|file") overrides the
# list below. Downloaded candidates are deleted after their run (the disk has
# room for one or two, not all); KEEP_MODELS=1 keeps them. The model the
# llama-server unit serves is never deleted.

set -u

REPO=${AGENT_REPO:-/agent}
MODEL_DIR=${LOCAL_MODEL_PATH:-/opt/local-models}
PORT=${LOCAL_MODEL_PORT:-8080}
THREADS=${LOCAL_MODEL_THREADS:-2}
CTX=${LOCAL_MODEL_CONTEXT:-8192}
SERVICE_USER=${LOCAL_MODEL_SERVICE_USER:-agent}
OUT_DIR=${OUT_DIR:-/var/tmp/model-benchmark}
TEST=live_tests/test_model_benchmark_controller.py
KEEP_MODELS=${KEEP_MODELS:-0}
UNIT_MODEL=$(systemctl cat llama-server 2>/dev/null | sed -n 's/.*--model \([^ ]*\).*/\1/p' | head -1)

DEFAULT_CANDIDATES="baseline-qwen2.5-coder-7b|Qwen/Qwen2.5-Coder-7B-Instruct-GGUF|qwen2.5-coder-7b-instruct-q4_k_m.gguf
gemma-3-4b|ggml-org/gemma-3-4b-it-GGUF|gemma-3-4b-it-Q4_K_M.gguf
qwen3-4b-2507|unsloth/Qwen3-4B-Instruct-2507-GGUF|Qwen3-4B-Instruct-2507-Q4_K_M.gguf
llama-3.1-8b|bartowski/Meta-Llama-3.1-8B-Instruct-GGUF|Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
phi-4-mini|bartowski/microsoft_Phi-4-mini-instruct-GGUF|microsoft_Phi-4-mini-instruct-Q4_K_M.gguf"
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
    echo "usage: sudo $0 <branch>   |   $0 --check" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: needs root to stop and start llama-server (run with sudo)" >&2
    exit 2
fi
BRANCH=$1
RUN_DIR="$OUT_DIR/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"
chmod 755 "$OUT_DIR" "$RUN_DIR"
SUMMARY="$RUN_DIR/summary.txt"
SERVER_PID=""

restore() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    systemctl start llama-server
    echo "llama-server unit: $(systemctl is-active llama-server)" | tee -a "$SUMMARY"
}
trap restore EXIT

echo "llama-server: $(llama-server --version 2>&1 | head -1)" | tee "$SUMMARY"
echo "branch: $BRANCH" | tee -a "$SUMMARY"
systemctl stop llama-server

while IFS='|' read -r name repo file; do
    [ -n "$name" ] || continue
    log="$RUN_DIR/$name.log"
    echo "=== $name ($file)" | tee -a "$SUMMARY"

    if ! HF_REPO="$repo" MODEL_FILE="$file" LOCAL_MODEL_PATH="$MODEL_DIR" \
            bash "$REPO/scripts/download-local-model.sh" >"$log" 2>&1; then
        echo "  download failed (see $log)" | tee -a "$SUMMARY"
        continue
    fi

    runuser -u "$SERVICE_USER" -- llama-server --model "$MODEL_DIR/$file" \
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
        kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""
        continue
    fi

    runuser -u "$SERVICE_USER" -- env AGENT_DB_PATH="${AGENT_DB_PATH:-/var/lib/nexus/agent.db}" \
        "$REPO/scripts/nexus-live-check.sh" "$BRANCH" "$TEST" >>"$log" 2>&1
    # One JSON line per scenario: scenario, action, verdict, seconds.
    sed -n '/live results (json)/,$p' "$log" | grep '^{' | python3 -c '
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    print("  %-20s %-6s %-22s %6.1fs" % (r.get("scenario"), "PASS" if r.get("verdict") else "FAIL",
          r.get("action"), r.get("llm_seconds") or r.get("seconds") or 0))
' | tee -a "$SUMMARY"

    kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""
    if [ "$KEEP_MODELS" != 1 ] && [ "$MODEL_DIR/$file" != "$UNIT_MODEL" ]; then
        rm -f "$MODEL_DIR/$file"
    fi
done <<< "$CANDIDATES"

chmod 644 "$RUN_DIR"/*
echo "results: $RUN_DIR" | tee -a "$SUMMARY"
