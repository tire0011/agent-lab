[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "=== Refreshing Nexus ===" -ForegroundColor Cyan

& (Join-Path $PSScriptRoot 'nexus-pull.ps1')
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus refresh stopped because the pull failed.'
}

# Bootstrap phase: expose the MCP write tools to remote clients.
# systemd services do not inherit this PowerShell process environment, so set
# the variable in the system manager environment before restarting agent-api.
sudo systemctl set-environment MCP_WRITE_ENABLED=true
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus was updated, but MCP write mode could not be enabled.'
}

& (Join-Path $PSScriptRoot 'nexus-restart.ps1') @args
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus was updated, but the restart failed.'
}
