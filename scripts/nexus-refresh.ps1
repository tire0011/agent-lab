[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "=== Refreshing Nexus ===" -ForegroundColor Cyan

& (Join-Path $PSScriptRoot 'nexus-pull.ps1')
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus refresh stopped because the pull failed.'
}

& (Join-Path $PSScriptRoot 'nexus-restart.ps1') @args
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus was updated, but the restart failed.'
}
