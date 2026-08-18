#!/usr/bin/env bash
# install-llama-cpp.sh
# Builds llama.cpp (CPU-only) and installs the llama-server binary.
# Designed to run in the background from an SSH session:
#
#   nohup bash scripts/install-llama-cpp.sh > /tmp/install-llama-cpp.log 2>&1 &
#   echo "Install started, PID=$!"
#
# Progress can be monitored:
#   tail -f /tmp/install-llama-cpp.log
#
# Idempotent: re-running the script rebuilds llama.cpp but does not remove
# any existing model files or service configuration.

set -euo pipefail

LOG_PREFIX="[install-llama-cpp]"
LLAMA_DIR="${LLAMA_DIR:-/opt/llama.cpp}"
INSTALL_DIR="${LLAMA_INSTALL_DIR:-/usr/local/bin}"
MAKE_JOBS="$(nproc 2>/dev/null || echo 2)"

echo "$LOG_PREFIX Starting llama.cpp build  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Source dir : $LLAMA_DIR"
echo "$LOG_PREFIX Install dir: $INSTALL_DIR"
echo "$LOG_PREFIX Build jobs : $MAKE_JOBS"

# ── System packages ──────────────────────────────────────────────────────────
echo "$LOG_PREFIX Installing system build dependencies…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    curl \
    libopenblas-dev \
    > /dev/null

# ── Clone or update llama.cpp ────────────────────────────────────────────────
if [[ -d "$LLAMA_DIR/.git" ]]; then
    echo "$LOG_PREFIX Updating existing llama.cpp checkout at $LLAMA_DIR…"
    git -C "$LLAMA_DIR" fetch --depth=1 origin
    git -C "$LLAMA_DIR" reset --hard FETCH_HEAD
else
    echo "$LOG_PREFIX Cloning llama.cpp into $LLAMA_DIR…"
    git clone --depth=1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
fi

# ── Build (CPU-only, OpenBLAS for throughput) ─────────────────────────────────
echo "$LOG_PREFIX Building llama.cpp (CPU-only, OpenBLAS) — this may take 5–15 minutes…"
cmake \
    -S "$LLAMA_DIR" \
    -B "$LLAMA_DIR/build" \
    -DGGML_NATIVE=OFF \
    -DGGML_CUDA=OFF \
    -DGGML_METAL=OFF \
    -DGGML_BLAS=ON \
    -DGGML_BLAS_VENDOR=OpenBLAS \
    -DLLAMA_BUILD_SERVER=ON \
    -DCMAKE_BUILD_TYPE=Release \
    > /dev/null

cmake --build "$LLAMA_DIR/build" --config Release -j "$MAKE_JOBS"

# ── Install binaries ──────────────────────────────────────────────────────────
echo "$LOG_PREFIX Installing llama-server and llama-cli to $INSTALL_DIR…"
for binary in llama-server llama-cli; do
    BIN_PATH="$LLAMA_DIR/build/bin/$binary"
    if [[ -f "$BIN_PATH" ]]; then
        install -m 0755 "$BIN_PATH" "$INSTALL_DIR/$binary"
        echo "$LOG_PREFIX   Installed $binary"
    else
        echo "$LOG_PREFIX   WARNING: $binary not found in build output — skipping" >&2
    fi
done

# Verify llama-server is executable.
if ! command -v llama-server &>/dev/null; then
    echo "$LOG_PREFIX ERROR: llama-server not found in PATH after installation." >&2
    exit 1
fi

echo "$LOG_PREFIX ✅ llama.cpp build complete  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX llama-server: $(llama-server --version 2>&1 | head -1 || true)"
echo "$LOG_PREFIX Next step: run scripts/download-local-model.sh"
