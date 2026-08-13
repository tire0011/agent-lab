[CmdletBinding()]
param(
    [ValidateSet('state', 'logs', 'restart', 'mailbox')]
    [string]$Action = 'state',

    [ValidateRange(1, 1000)]
    [int]$Lines = 100,

    [switch]$Follow,

    # When supplied on the 'logs' action, prompts for AGENT_API_KEY (never
    # echoed, never written to disk) and additionally fetches authenticated
    # diagnostic detail (phase, matched policy rule, test command/exit
    # code, changed files, bounded test output) for the newest experiments
    # auto-discovered from /activity/public.
    [switch]$Details,

    [ValidateRange(1, 20)]
    [int]$ExperimentCount = 5
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
    Write-Section 'Journal'
    $Arguments = @(
        '-u', 'agent-api.service',
        '-u', 'nexus-mailbox.service',
        '-n', $Lines.ToString(),
        '--no-pager',
        '--output=short-iso'
    )
    if ($Follow) { $Arguments += '-f' }
    journalctl @Arguments

    if ($Follow) {
        # -f never returns; everything below (diagnostics, authenticated
        # details) only makes sense for a single snapshot.
        return
    }

    # Single public, strictly allowlisted endpoint: service/mailbox status,
    # git revision/deployment time, latest experiments (full ids, lifecycle
    # phases+durations, sanitized selection/rejection reason, matched policy
    # rule, test command/exit code, changed files, bounded sanitized
    # diagnostic messages, token usage/cost), and current problems. Never
    # includes secrets, prompts, env data, diffs, or raw command output.
    Write-Section 'Diagnostics (/diagnostics/public)'
    $Diagnostics = $null
    try {
        $Diagnostics = Invoke-RestMethod -Uri "$ApiBaseUrl/diagnostics/public?limit=$ExperimentCount" -TimeoutSec 10
        $Diagnostics | ConvertTo-Json -Depth 8
    }
    catch {
        Write-Host "GET /diagnostics/public FAILED: $($_.Exception.Message)" -ForegroundColor Red
    }

    # Auto-discover the newest experiment ids from the diagnostics response
    # (already newest-first). These are full experiment ids, but only an
    # 8+ char prefix is ever required by either the public or authenticated
    # /experiments/{id} endpoint, so a prefix is all we need downstream.
    $ExperimentIds = @()
    if ($Diagnostics -and $Diagnostics.experiments) {
        $ExperimentIds = @(
            $Diagnostics.experiments |
                Select-Object -ExpandProperty id -Unique |
                Select-Object -First $ExperimentCount
        )
    }

    if ($ExperimentIds.Count -eq 0) {
        Write-Host "No experiments discovered from /diagnostics/public." -ForegroundColor Yellow
    }

    if (-not $Details) {
        return
    }

    # --- Authenticated diagnostic detail -------------------------------
    # Prompt for AGENT_API_KEY only now, only in-memory, never echoed and
    # never written to disk/env/log. The plaintext copy is cleared as soon
    # as the authenticated calls below are done.
    Write-Section 'Authenticated diagnostics (-Details)'
    $SecureApiKey = Read-Host -Prompt 'AGENT_API_KEY' -AsSecureString
    $PlainApiKey = [System.Net.NetworkCredential]::new('', $SecureApiKey).Password
    try {
        if ([string]::IsNullOrEmpty($PlainApiKey)) {
            Write-Host 'No API key entered; skipping authenticated diagnostics.' -ForegroundColor Yellow
            return
        }
        $AuthHeaders = @{ 'x-api-key' = $PlainApiKey }

        if ($ExperimentIds.Count -eq 0) {
            Write-Host 'No experiments to inspect.' -ForegroundColor Yellow
            return
        }

        foreach ($ExperimentId in $ExperimentIds) {
            Write-Section "Experiment $ExperimentId"
            try {
                # /experiments/{id} accepts an unambiguous 8+ char prefix,
                # matching the public endpoint, so the short id from
                # /activity/public resolves to the full experiment id here.
                $Full = Invoke-RestMethod -Uri "$ApiBaseUrl/experiments/$ExperimentId" -Headers $AuthHeaders -TimeoutSec 10
                [pscustomobject]@{
                    id                    = $Full.id
                    status                = $Full.status
                    policy_rule           = $Full.policy_rule
                    test_command          = $Full.test_command
                    test_exit_code        = $Full.test_exit_code
                    created_at            = $Full.created_at
                    ended_at              = $Full.ended_at
                } | ConvertTo-Json -Depth 4
            }
            catch {
                Write-Host "GET /experiments/$ExperimentId FAILED: $($_.Exception.Message)" -ForegroundColor Red
                continue
            }

            try {
                $Diagnostics = Invoke-RestMethod -Uri "$ApiBaseUrl/activity?experiment_id=$ExperimentId&limit=10" -Headers $AuthHeaders -TimeoutSec 10
                foreach ($Entry in $Diagnostics.activity) {
                    [pscustomobject]@{
                        timestamp         = $Entry.timestamp
                        phase             = $Entry.phase
                        from_status       = $Entry.from_status
                        to_status         = $Entry.to_status
                        policy_rule       = $Entry.policy_rule
                        rejection_reason  = $Entry.rejection_reason
                        test_command      = $Entry.test_command
                        test_exit_code    = $Entry.test_exit_code
                        changed_files     = $Entry.changed_files
                        test_output_lines = $Entry.test_output_lines
                    } | ConvertTo-Json -Depth 4
                }
            }
            catch {
                Write-Host "GET /activity FAILED: $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
    finally {
        # Best-effort in-memory cleanup; never persisted to disk or env.
        $PlainApiKey = $null
        $AuthHeaders = $null
        Remove-Variable -Name PlainApiKey, SecureApiKey, AuthHeaders -ErrorAction SilentlyContinue
        [System.GC]::Collect()
    }
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
