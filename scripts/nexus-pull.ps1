[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Write-Host "=== Pulling Nexus ===" -ForegroundColor Cyan

$Status = @(git -C $RepoRoot status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw 'Could not read the Nexus git status.'
}
if ($Status.Count -gt 0) {
    Write-Host 'Nexus working tree is dirty; pull aborted.' -ForegroundColor Red
    $Status | ForEach-Object { Write-Host $_ }
    throw 'Commit, stash, or remove the local changes before refreshing Nexus.'
}

git -C $RepoRoot pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus pull failed. No merge or reset was performed.'
}

$SyncScript = Join-Path $RepoRoot 'scripts/sync-api-deps.sh'
& /bin/bash $SyncScript
if ($LASTEXITCODE -ne 0) {
    throw 'Nexus pull completed, but API dependency synchronization failed.'
}

git -C $RepoRoot log -1 --oneline
if ($LASTEXITCODE -ne 0) {
    throw 'Could not display the deployed Nexus revision.'
}
