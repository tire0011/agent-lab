"""Reusable building blocks for the V0/V1 agent runner (agent.py).

Each module here is intentionally small and focused (serialization, Azure
OpenAI/MCP client construction, run result extraction, run logging, CLI
output, cost monitoring, and structured run persistence) so agent.py stays
a thin orchestration script.
"""
