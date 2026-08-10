param(
    [Parameter(Mandatory)]
    [string]$Prompt
)

$endpoint = az cognitiveservices account show `
    --name openAI-tire0011 `
    --resource-group DefaultResourceGroup-DEWC `
    --query properties.endpoint -o tsv

$key = az cognitiveservices account keys list `
    --name openAI-tire0011 `
    --resource-group DefaultResourceGroup-DEWC `
    --query key1 -o tsv

$headers = @{
    "api-key"      = $key
    "Content-Type" = "application/json"
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