# Minimal Agent Prototype

This repository includes a minimal command-line agent in `agent.py`.

## Requirements

- Python 3.10+
- Azure OpenAI deployment named `gpt-4o` (or set a different deployment in env)

## Environment variables

Set these before running:

- `AZURE_OPENAI_ENDPOINT` (for example: `https://<resource>.openai.azure.com`)
- `AZURE_OPENAI_API_KEY`
- Optional: `AZURE_OPENAI_DEPLOYMENT` (default: `gpt-4o`)
- Optional: `AZURE_OPENAI_API_VERSION` (default: `2024-06-01`)

Keep secrets out of source control.

## Usage

Run with a task prompt:

`python agent.py "Summarize the benefits of unit tests in 3 bullets."`

The response is printed to stdout.

Each run is logged as JSON under `state/runs/` with:

- timestamp
- model
- task
- response
- prompt tokens
- completion tokens
- total tokens
- runtime seconds
