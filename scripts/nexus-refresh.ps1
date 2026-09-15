[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RestArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Services = @('agent-api.service', 'nexus-mailbox.service')

Write-Host "=== Refreshing Nexus ===" -ForegroundColor Cyan

$Report = & (Join-Path $PSScriptRoot 'nexus-pull.ps1')
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

& (Join-Path $PSScriptRoot 'nexus-restart.ps1') @RestArgs
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus was updated, but the restart failed.'
}

# One closing report: what was rescued, what was archived, which commit is live
# now, and how the services came back (issue #186).
Write-Host ""
Write-Host "=== Refresh summary ===" -ForegroundColor Cyan
if ($null -ne $Report) {
    $Previous = if ($Report.previous_head) { $Report.previous_head.Substring(0, 12) } else { '?' }
    $Current = if ($Report.new_head) { $Report.new_head.Substring(0, 12) } else { '?' }
    Write-Host "branch: $($Report.branch)"
    Write-Host "head: $Previous -> $Current"
    if ($null -ne $Report.rescue -and $Report.rescue.performed) {
        if ($Report.rescue.rescue_branch) {
            Write-Host "rescued work: $($Report.rescue.rescue_branch) @ $($Report.rescue.rescue_commit)"
            foreach ($Path in $Report.rescue.rescued_paths) {
                Write-Host "  preserved: $Path"
            }
        }
        foreach ($Archived in $Report.rescue.archived) {
            Write-Host "  archived: $($Archived.repo_path) -> $($Archived.archived_path)"
        }
    }
    else {
        Write-Host 'rescued work: none (checkout was clean)'
    }
    if ($Report.diverged_branch) {
        Write-Host "local commits preserved on: $($Report.diverged_branch)"
    }
}
else {
    Write-Host 'refresh report unavailable' -ForegroundColor Yellow
}

foreach ($Service in $Services) {
    $State = (systemctl is-active $Service)
    Write-Host "service ${Service}: $State"
}
