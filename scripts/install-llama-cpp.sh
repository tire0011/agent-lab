#!/usr/bin/env bash
# install-llama-cpp.sh
# Installs llama-cpp-python (CPU-only) and its Python dependencies.
# Designed to run in the background from an iPhone SSH session:
#
#   nohup bash scripts/install-llama-cpp.sh > /tmp/install-llama-cpp.log 2>&1 &
#   echo "Install started, PID=$!"
#
# Progress can be monitored:
#   tail -f /tmp/install-llama-cpp.log

set -euo pipefail

LOG_PREFIX="[install-llama-cpp]"
VENV_DIR="${LLAMA_VENV:-/opt/llama-venv}"
CMAKE_ARGS="${CMAKE_ARGS:-"-DLLAMA_CUBLAS=OFF -DLLAMA_METAL=OFF"}"
# Number of CPU threads for compilation – use all available cores
MAKE_JOBS="$(nproc 2>/dev/null || echo 2)"

echo "$LOG_PREFIX Starting llama-cpp-python installation  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX VENV_DIR=$VENV_DIR  MAKE_JOBS=$MAKE_JOBS"

# ── System packages ──────────────────────────────────────────────────────────
echo "$LOG_PREFIX Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    build-essential cmake git curl \
    libopenblas-dev \
    > /dev/null

# ── Python virtual environment ───────────────────────────────────────────────
echo "$LOG_PREFIX Creating Python virtual environment at $VENV_DIR…"
python3 -m venv "$VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "$LOG_PREFIX Upgrading pip…"
pip install --upgrade pip --quiet

# ── llama-cpp-python (CPU-only build) ────────────────────────────────────────
# CMAKE_ARGS controls the build; FORCE_CMAKE=1 ensures a source build.
echo "$LOG_PREFIX Installing llama-cpp-python (CPU-only, this may take 5-15 minutes)…"
CMAKE_ARGS="$CMAKE_ARGS" \
FORCE_CMAKE=1 \
    pip install llama-cpp-python --verbose 2>&1 | tail -n 20

# ── Additional Python dependencies ───────────────────────────────────────────
echo "$LOG_PREFIX Installing additional Python dependencies…"
pip install --quiet \
    httpx \
    fastapi \
    uvicorn[standard] \
    "openai>=1.0.0" \
    aiohttp

# ── Write activation helper ──────────────────────────────────────────────────
HELPER="/usr/local/bin/llama-activate"
cat > "$HELPER" <<'EOF'
#!/usr/bin/env bash
# Source this file to activate the llama virtual environment:
#   source /usr/local/bin/llama-activate
source /opt/llama-venv/bin/activate
EOF
chmod +x "$HELPER"

echo "$LOG_PREFIX ✅ llama-cpp-python installation complete  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$LOG_PREFIX Activate the venv with:  source $HELPER"
echo "$LOG_PREFIX Next step: run scripts/download-llama2-13b.sh"
