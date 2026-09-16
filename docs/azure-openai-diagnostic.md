# Azure OpenAI Nexus diagnostic

Use `scripts/test_azure_openai.py` to exercise the exact Azure OpenAI delegation path used by Nexus. The script prints endpoint/deployment information, whether a credential is configured, HTTP status, and bounded sanitized Azure error details. It never prints the credential value.

On the Azure VM, load the same environment file used by the Nexus services and run:

```bash
cd /agent
set -a
. /etc/agent-lab/agent.env
set +a
.venv/bin/python scripts/test_azure_openai.py
```

From Windows PowerShell over SSH:

```powershell
ssh agent-lab-vm "cd /agent && set -a && . /etc/agent-lab/agent.env && set +a && .venv/bin/python scripts/test_azure_openai.py"
```

The diagnostic calls:

```python
await service.delegate(
    "azure_openai",
    "Reply with exactly OK.",
    context="manual diagnostic",
)
```

so it does not duplicate Azure HTTP request logic in a separate test harness.
