[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ServiceName = 'nexus-terminal-mcp.service'
$ServiceFile = "/etc/systemd/system/$ServiceName"
$InstallRoot = '/opt/nexus-terminal-mcp'

function Write-Step([string]$Text) {
    Write-Host "`n=== $Text ===" -ForegroundColor Cyan
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)] [string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$IgnoreFailure
    )

    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if (-not $IgnoreFailure -and $code -ne 0) {
        throw "$FilePath failed with exit code $code"
    }
    return $code
}

Write-Step 'Stopping and disabling Terminal MCP'
Invoke-NativeChecked 'sudo' @('systemctl', 'disable', '--now', $ServiceName) -IgnoreFailure | Out-Null

Write-Step 'Removing systemd unit'
if (Test-Path $ServiceFile) {
    Invoke-NativeChecked 'sudo' @('rm', '-f', $ServiceFile) | Out-Null
}
Invoke-NativeChecked 'sudo' @('systemctl', 'daemon-reload') | Out-Null
Invoke-NativeChecked 'sudo' @('systemctl', 'reset-failed') -IgnoreFailure | Out-Null

Write-Step 'Removing Terminal MCP installation'
if (Test-Path $InstallRoot) {
    Invoke-NativeChecked 'sudo' @('rm', '-rf', $InstallRoot) | Out-Null
}

Write-Step 'Verifying removal'
& systemctl status $ServiceName --no-pager 2>$null
if ($LASTEXITCODE -eq 0) {
    throw "$ServiceName still appears to be active."
}

Write-Host "`nTerminal MCP was removed." -ForegroundColor Green
Write-Host "Removed service: $ServiceName"
Write-Host "Removed install directory: $InstallRoot"
