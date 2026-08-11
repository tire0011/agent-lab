param(
  [ValidateSet("health", "status", "chat")]
  [string]$Command = "health",

  [string]$Message,
  [switch]$Interactive,
  [string]$ApiKey,
  [string]$ChatPath
)

$env:AGENT_API_BASE_URL = "https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com"

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