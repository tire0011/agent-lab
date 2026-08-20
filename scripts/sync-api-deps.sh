#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AGENT_REPO_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PIP="${AGENT_VENV_PIP:-$REPO_ROOT/.venv/bin/pip}"
REQUIREMENTS="${AGENT_REQUIREMENTS_FILE:-$REPO_ROOT/requirements-api.txt}"

if [[ ! -x "$PIP" ]]; then
    echo "dependency sync failed: project venv pip is not executable: $PIP" >&2
    exit 2
fi

if [[ ! -r "$REQUIREMENTS" ]]; then
    echo "dependency sync failed: requirements file is not readable: $REQUIREMENTS" >&2
    exit 2
fi

echo "syncing API dependencies from $REQUIREMENTS"
"$PIP" install --disable-pip-version-check -r "$REQUIREMENTS"
echo "API dependency sync complete"
