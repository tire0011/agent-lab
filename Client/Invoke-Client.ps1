param(
  [ValidateSet("health", "status", "problems", "ideas", "chat")]
  [string]$Command = "health",

  [string]$Message,
  [switch]$Interactive,
  [string]$ApiKey,
  [string]$ChatPath
)

if (-not $env:AGENT_API_BASE_URL) {
  throw "Set AGENT_API_BASE_URL to the Nexus host, e.g. https://<vm-host>"
}

$clientArgs = @($Command)

if ($Command -eq "status" -and $ApiKey) {
  $clientArgs += @("--api-key", $ApiKey)
}

if ($Command -eq "chat") {
  if ($Interactive) {
    $clientArgs += "--interactive"
  }
  if ($ChatPath) {
    $clientArgs += @("--chat-path", $ChatPath)
  }
  if ($Message) {
    $clientArgs += $Message
  }
}

Write-Host "AGENT_API_BASE_URL set to: $env:AGENT_API_BASE_URL"
Write-Host "Starting client: python -m client $($clientArgs -join ' ')"

python -m client @clientArgs
exit $LASTEXITCODE