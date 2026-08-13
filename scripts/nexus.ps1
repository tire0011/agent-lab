[CmdletBinding()]
param(
    [ValidateSet('state', 'logs', 'restart', 'mailbox')]
    [string]$Action = 'state',

    [ValidateRange(1, 1000)]
    [int]$Lines = 100,

    [switch]$Follow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ApiBaseUrl = 'http://127.0.0.1:8000'
$Services = @(
    'agent-api.service',
    'nexus-mailbox.service',
    'nexus-mailbox.timer'
)

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Get-NexusState {
    Write-Section 'Services'
    systemctl --no-pager --full status @Services

    Write-Section 'Timers'
    systemctl list-timers nexus-mailbox.timer --no-pager

    Write-Section 'API'
    foreach ($Path in @('/health', '/status', '/activity/public', '/problems')) {
        try {
            $Response = Invoke-RestMethod -Uri "$ApiBaseUrl$Path" -TimeoutSec 10
            Write-Host "$Path OK" -ForegroundColor Green
            $Response | ConvertTo-Json -Depth 8
        }
        catch {
            Write-Host "$Path FAILED: $($_.Exception.Message)" -ForegroundColor Red
        }
    }

    Write-Section 'Git'
    git status --short --branch
}

function Get-NexusLogs {
    $Arguments = @(
        '-u', 'agent-api.service',
        '-u', 'nexus-mailbox.service',
        '-n', $Lines.ToString(),
        '--no-pager',
        '--output=short-iso'
    )
    if ($Follow) { $Arguments += '-f' }
    journalctl @Arguments
}

function Invoke-NexusMailbox {
    sudo systemctl start nexus-mailbox.service
    systemctl status nexus-mailbox.service --no-pager --full
    journalctl -u nexus-mailbox.service -n $Lines --no-pager --output=short-iso
}

function Restart-Nexus {
    Write-Section 'Restarting Nexus'
    sudo systemctl daemon-reload
    sudo systemctl enable --now nexus-mailbox.timer
    sudo systemctl restart agent-api.service
    sudo systemctl start nexus-mailbox.service
    Start-Sleep -Seconds 2
    Get-NexusState
}

switch ($Action) {
    'state'   { Get-NexusState }
    'logs'    { Get-NexusLogs }
    'restart' { Restart-Nexus }
    'mailbox' { Invoke-NexusMailbox }
}
