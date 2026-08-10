param(
    [Parameter(Mandatory)]
    [string]$Prompt
)

$endpoint = "https://openai-tire0011.openai.azure.com"

$accessToken = az account get-access-token `
    --resource https://cognitiveservices.azure.com `
    --query accessToken `
    --output tsv

if (-not $accessToken) {
    throw "Could not obtain Azure access token."
}

$headers = @{
    "Authorization" = "Bearer $accessToken"
    "Content-Type"  = "application/json"
}
$body = @{
    model = "gpt-4o"
    messages = @(
        @{
            role    = "user"
            content = $Prompt
        }
    )
} | ConvertTo-Json -Depth 5

$result = Invoke-RestMethod `
    -Uri "$endpoint/openai/v1/chat/completions" `
    -Method Post `
    -Headers $headers `
    -Body $body

$result.choices[0].message.content

"`nTokens:"
$result.usage | Format-List