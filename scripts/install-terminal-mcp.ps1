[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8900,

    [ValidateRange(5, 300)]
    [int]$CommandTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$SourceRepo = 'https://github.com/iris-networks/terminal_mcp.git'
$InstallRoot = '/opt/nexus-terminal-mcp'
$SourceDir = "$InstallRoot/src"
$BinDir = "$InstallRoot/bin"
$Binary = "$BinDir/mcp-terminal-server"
$GoRoot = "$InstallRoot/go"
$ServiceName = 'nexus-terminal-mcp.service'
$ServiceFile = "/etc/systemd/system/$ServiceName"
$ServiceUser = 'agent'

function Write-Step([string]$Text) {
    Write-Host "`n=== $Text ===" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string]$FilePath,
        [Parameter(Mandatory = $true)] [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Get-GoExecutable {
    $existing = Get-Command go -ErrorAction SilentlyContinue
    if ($existing) {
        $versionText = (& $existing.Source version) -join ' '
        if ($versionText -match 'go(\d+)\.(\d+)(?:\.(\d+))?') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 1 -or ($major -eq 1 -and $minor -ge 23)) {
                Write-Host "Using existing $versionText" -ForegroundColor Green
                return $existing.Source
            }
        }
    }

    Write-Step 'Installing a private Go toolchain (1.23+)'
    $machine = (& uname -m).Trim()
    $goArch = switch ($machine) {
        'x86_64' { 'amd64' }
        'aarch64' { 'arm64' }
        'arm64' { 'arm64' }
        default { throw "Unsupported CPU architecture: $machine" }
    }

    $releases = Invoke-RestMethod -Uri 'https://go.dev/dl/?mode=json' -TimeoutSec 30
    $release = $releases | Where-Object { $_.stable -eq $true } | Select-Object -First 1
    if (-not $release) { throw 'Could not discover the current stable Go release.' }

    $archive = $release.files | Where-Object {
        $_.os -eq 'linux' -and $_.arch -eq $goArch -and $_.kind -eq 'archive'
    } | Select-Object -First 1
    if (-not $archive) { throw "No Linux/$goArch Go archive found." }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) $archive.filename
    try {
        Write-Host "Downloading $($archive.filename)"
        Invoke-WebRequest -Uri "https://go.dev/dl/$($archive.filename)" -OutFile $tmp -TimeoutSec 120
        $actualSha = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -ne $archive.sha256.ToLowerInvariant()) {
            throw 'Go archive checksum mismatch.'
        }

        Invoke-Checked 'sudo' @('rm', '-rf', $GoRoot)
        Invoke-Checked 'sudo' @('mkdir', '-p', $InstallRoot)
        Invoke-Checked 'sudo' @('tar', '-C', $InstallRoot, '-xzf', $tmp)
    }
    finally {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    }

    $go = "$GoRoot/bin/go"
    if (-not (Test-Path $go)) { throw "Go installation failed: $go not found." }
    return $go
}

Write-Step 'Preparing standalone Terminal MCP'
$go = Get-GoExecutable

Invoke-Checked 'sudo' @('mkdir', '-p', $InstallRoot, $BinDir)
Invoke-Checked 'sudo' @('chown', '-R', "${ServiceUser}:${ServiceUser}", $InstallRoot)

if (Test-Path "$SourceDir/.git") {
    Write-Host 'Updating existing Terminal MCP source checkout.'
    Invoke-Checked 'git' @('-C', $SourceDir, 'fetch', '--depth', '1', 'origin', 'main')
    Invoke-Checked 'git' @('-C', $SourceDir, 'reset', '--hard', 'origin/main')
} else {
    if (Test-Path $SourceDir) {
        Invoke-Checked 'sudo' @('rm', '-rf', $SourceDir)
    }
    Invoke-Checked 'git' @('clone', '--depth', '1', $SourceRepo, $SourceDir)
}

Write-Step 'Building Terminal MCP'
Push-Location $SourceDir
try {
    $env:CGO_ENABLED = '0'
    Invoke-Checked $go @('build', '-trimpath', '-o', $Binary, '.')
}
finally {
    Pop-Location
}
Invoke-Checked 'chmod' @('0755', $Binary)

Write-Step 'Registering independent systemd service'
$unit = @"
[Unit]
Description=Nexus standalone recovery Terminal MCP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$ServiceUser
WorkingDirectory=/agent
Environment=MCP_COMMAND_TIMEOUT=$CommandTimeoutSeconds
Environment=MCP_SHELL=/bin/bash
ExecStart=$Binary --http --host 127.0.0.1 --port $Port
Restart=always
RestartSec=3
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"@

$tmpUnit = Join-Path ([IO.Path]::GetTempPath()) 'nexus-terminal-mcp.service'
try {
    [IO.File]::WriteAllText($tmpUnit, $unit, [Text.UTF8Encoding]::new($false))
    Invoke-Checked 'sudo' @('install', '-m', '0644', $tmpUnit, $ServiceFile)
}
finally {
    Remove-Item -Force $tmpUnit -ErrorAction SilentlyContinue
}

Invoke-Checked 'sudo' @('systemctl', 'daemon-reload')
Invoke-Checked 'sudo' @('systemctl', 'enable', '--now', $ServiceName)
Invoke-Checked 'sudo' @('systemctl', 'restart', $ServiceName)
Start-Sleep -Seconds 2

Write-Step 'Checking service'
& systemctl --no-pager --full status $ServiceName
if ($LASTEXITCODE -ne 0) {
    & journalctl -u $ServiceName -n 80 --no-pager
    throw "$ServiceName did not start successfully."
}

$payload = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"nexus-installer","version":"1.0"}}}'
try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/mcp" -Method Post -ContentType 'application/json' -Body $payload -TimeoutSec 10
    Write-Host "MCP endpoint responded: HTTP $($response.StatusCode)" -ForegroundColor Green
}
catch {
    & journalctl -u $ServiceName -n 80 --no-pager
    throw "Service is running but MCP endpoint test failed: $($_.Exception.Message)"
}

Write-Host "`nTerminal MCP is installed and independent from agent-api." -ForegroundColor Green
Write-Host "Local MCP endpoint: http://127.0.0.1:$Port/mcp"
Write-Host "Service: $ServiceName"
Write-Host "Logs: journalctl -u $ServiceName -n 100 --no-pager"
Write-Host 'The MCP server binds only to 127.0.0.1. Add an authenticated reverse-proxy route before exposing it remotely.' -ForegroundColor Yellow
