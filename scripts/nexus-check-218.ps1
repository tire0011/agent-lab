[CmdletBinding()]
param(
    # The branch carrying the #218 fix. Pass an empty string or -SkipCheckout
    # to test whatever is already checked out.
    [string]$Branch = 'claude/wizardly-hopper-7icxf3',

    [switch]$SkipCheckout,

    # The task text that produced the failing session.
    [string]$Task = 'fix github issue #196',

    [ValidateRange(1, 10)]
    [int]$MaxSteps = 4,

    # Also dump the raw observations.
    [switch]$Json
)

# Runs the #218 manual check end to end on the VM: verifies llama-server is up,
# puts the fix on disk, and drives the real local controller over the task text
# from session 1ca2d781836a481d8152744f5c1ff94d.
#
# It deliberately does NOT source /etc/agent-lab/agent.env. That file is
# root-owned by design (see nexus.ps1) and the check does not need it: the only
# thing it talks to is llama-server on 127.0.0.1:8080, and session state goes to
# a scratch database.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PowerShell 7.4 turns a non-zero native exit code into a terminating error by
# default. `systemctl is-active` returns 3 for an inactive unit and the python
# check returns 1 or 2 by design, so every native exit code here is inspected
# explicitly instead - otherwise the script dies where it means to report.
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LlamaService = 'llama-server'
$LocalModelUrl = if ($env:LOCAL_MODEL_URL) { $env:LOCAL_MODEL_URL } else { 'http://127.0.0.1:8080/v1' }

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

Write-Host "=== Nexus #218 check ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- local model
Write-Section 'Local model'
$LlamaState = (systemctl is-active $LlamaService 2>$null | Select-Object -First 1)
Write-Host "$LlamaService : $LlamaState"
if ($LlamaState -ne 'active') {
    Write-Host "$LlamaService is not active. Start it and re-run:" -ForegroundColor Red
    Write-Host "  sudo systemctl start $LlamaService"
    Write-Host "  journalctl -u $LlamaService -n 50 --no-pager"
    exit 2
}

# Active is not the same as answering. A model still loading returns nothing,
# which the check would otherwise report as a failed controller turn.
try {
    $Models = Invoke-RestMethod -Uri "$LocalModelUrl/models" -TimeoutSec 30
    # Strict mode makes a missing property a terminating error, and this is the
    # one place the shape is not ours to rely on.
    $ModelName = '?'
    if ($Models -and $Models.PSObject.Properties.Name -contains 'data') {
        $Entries = @($Models.data)
        if ($Entries.Count -gt 0) { $ModelName = $Entries[0].id }
    }
    Write-Host "endpoint  : $LocalModelUrl answering (model: $ModelName)" -ForegroundColor Green
}
catch {
    Write-Host "endpoint  : $LocalModelUrl NOT answering - $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The service is up but the model may still be loading. Watch it with:"
    Write-Host "  journalctl -u $LlamaService -f"
    exit 2
}

# --------------------------------------------------------------------- source
Write-Section 'Source'
Push-Location $RepoRoot
try {
    if ($SkipCheckout -or [string]::IsNullOrWhiteSpace($Branch)) {
        Write-Host "checkout: left as is (-SkipCheckout)"
    }
    else {
        git fetch origin $Branch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "could not fetch origin/$Branch" }
        git checkout $Branch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "could not check out $Branch (is the worktree dirty? run scripts/nexus-pull.ps1)" }
        git pull --ff-only origin $Branch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "could not fast-forward $Branch" }
    }
    git --no-pager log --oneline -1 | Out-Host
    git status --short --branch | Out-Host

    # ------------------------------------------------------------------- check
    Write-Section 'Check'
    $Python = if ($env:AGENT_VENV_PYTHON) { $env:AGENT_VENV_PYTHON } else { Join-Path $RepoRoot '.venv/bin/python' }
    if (-not (Test-Path $Python)) {
        $Python = 'python3'
    }
    Write-Host "python: $Python"
    Write-Host ""

    $Arguments = @(
        (Join-Path $RepoRoot 'scripts/check_issue_218_find_tool.py'),
        '--task', $Task,
        '--repo-root', $RepoRoot,
        '--max-steps', $MaxSteps.ToString()
    )
    if ($Json) { $Arguments += '--json' }

    & $Python @Arguments | Out-Host
    $CheckExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

# -------------------------------------------------------------------- verdict
Write-Section 'Verdict'
switch ($CheckExitCode) {
    0 {
        Write-Host "PASS - the #218 failure signature did not reappear." -ForegroundColor Green
    }
    1 {
        Write-Host "FAIL - the #218 failure signature is back. See the decision trace above." -ForegroundColor Red
    }
    2 {
        Write-Host "INCONCLUSIVE - nothing was verified. See the reason above." -ForegroundColor Yellow
    }
    default {
        Write-Host "The check exited with $CheckExitCode." -ForegroundColor Red
    }
}
Write-Host "A single run is one sample of a small local model; re-run it a few times before concluding."

exit $CheckExitCode
