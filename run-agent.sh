#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

legacy_env_file="$project_dir/.env"
if [[ -e "$legacy_env_file" ]]; then
    echo "Refusing to start: $legacy_env_file is visible to the MCP filesystem." >&2
    echo "Move runtime secrets to /etc/agent-lab/agent.env and remove the repo copy." >&2
    exit 2
fi

agent_env_file="/etc/agent-lab/agent.env"
if [[ -f "$agent_env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$agent_env_file"
    set +a
fi

node_home="${AGENT_NODE_HOME:-$HOME/.local/node-v22.23.2}"
if [[ -d "$node_home/bin" ]]; then
    export PATH="$node_home/bin:$PATH"
fi

export AZURE_OPENAI_ENDPOINT="https://openai-tire0011.openai.azure.com/"
export AGENT_FS_ROOT="/agent"

case "${AZURE_OPENAI_AUTH:-azure_cli}" in
    azure_cli)
        command -v az >/dev/null || {
            echo "Azure CLI is required for AZURE_OPENAI_AUTH=azure_cli." >&2
            exit 2
        }
        AZURE_OPENAI_API_KEY="$(az account get-access-token \
            --resource https://cognitiveservices.azure.com \
            --query accessToken \
            --output tsv)"
        export AZURE_OPENAI_API_KEY
        ;;
    api_key)
        : "${AZURE_OPENAI_API_KEY:?AZURE_OPENAI_API_KEY is required for api_key auth}"
        ;;
    *)
        echo "Unsupported AZURE_OPENAI_AUTH. Use azure_cli or api_key." >&2
        exit 2
        ;;
esac

python_bin="${AGENT_PYTHON:-$project_dir/.venv-agent/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    echo "Agent Python not found at $python_bin." >&2
    echo "Create it with: python3 -m venv .venv-agent && .venv-agent/bin/pip install -r requirements-agent.txt" >&2
    exit 2
fi

exec "$python_bin" agent.py "$@"
