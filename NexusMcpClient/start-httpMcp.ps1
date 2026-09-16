param(
  [switch]$update_key
)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repoRoot
try {
  if ($update_key -or [string]::IsNullOrWhiteSpace($env:AGENT_API_KEY)) {
    $secureApiKey = Read-Host "Enter AGENT_API_KEY" -AsSecureString
    $apiKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
    try {
      $env:AGENT_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPointer)
    }
    finally {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPointer)
    }
  }

  if ([string]::IsNullOrWhiteSpace($env:AGENT_API_KEY)) {
    throw "AGENT_API_KEY is required."
  }

  $env:NEXUS_MCP_TRANSPORT = "streamable-http"
  python -m NexusMcpClient
}
finally {
  Pop-Location
}