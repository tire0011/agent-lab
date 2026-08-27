param(
    [string]$ApiKey = $env:AGENT_API_KEY,
    [string]$BaseUrl = $(if ($env:AGENT_API_BASE_URL) { $env:AGENT_API_BASE_URL } else { "http://127.0.0.1:8000" })
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not $ApiKey) {
    $secure = Read-Host "Nexus API key" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

if (-not $ApiKey) {
    throw "AGENT_API_KEY is required."
}

$env:AGENT_API_KEY = $ApiKey
$env:AGENT_API_BASE_URL = $BaseUrl

Set-Location $repoRoot
& python "Client/nexus_chat.py"
exit $LASTEXITCODE
