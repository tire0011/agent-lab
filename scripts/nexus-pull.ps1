[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Write-Host "=== Pulling Nexus ===" -ForegroundColor Cyan

# The checkout is deployable state, not a workspace. This used to abort on any
# dirty worktree, which left the VM running nine-commit-old code because one
# generated outbox file sat untracked in /agent. modules.nexus_refresh now
# preserves local work on a rescue branch, archives runtime artifacts outside
# the repository, and only then fast-forwards main (issue #186).
$Python = if ($env:AGENT_VENV_PYTHON) { $env:AGENT_VENV_PYTHON } else { Join-Path $RepoRoot '.venv/bin/python' }
if (-not (Test-Path $Python)) {
    $Python = 'python3'
}

# Native command output goes to the pipeline, and this script returns the
# refresh report to its caller - so every command's output is sent to the host
# explicitly, leaving the report as the only pipeline value.
$ReportPath = Join-Path ([System.IO.Path]::GetTempPath()) "nexus-refresh-$PID.json"
Push-Location $RepoRoot
try {
    & $Python -m modules.nexus_refresh --repo-root $RepoRoot --json-out $ReportPath | Out-Host
    $RefreshExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

$Report = $null
if (Test-Path $ReportPath) {
    try {
        $Report = Get-Content -Raw -Path $ReportPath | ConvertFrom-Json
    }
    catch {
        Write-Host 'Could not parse the refresh report.' -ForegroundColor Yellow
    }
    Remove-Item -Path $ReportPath -Force -ErrorAction SilentlyContinue
}

if ($RefreshExitCode -ne 0) {
    throw 'Nexus pull failed. No merge, reset or force checkout was performed.'
}

$SyncScript = Join-Path $RepoRoot 'scripts/sync-api-deps.sh'
& /bin/bash $SyncScript | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus pull completed, but API dependency synchronization failed.'
}

git -C $RepoRoot log -1 --oneline | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'Could not display the deployed Nexus revision.'
}

return $Report
