# Sends a message to your Telegram bot via the Telegram Bot API.
# Reads the bot token from ~/.claude/channels/telegram/.env (TELEGRAM_BOT_TOKEN=...)
# and the recipient chat ID from ~/.claude/channels/telegram/access.json (first numeric ID in allowFrom).
#
# Used by the morning-scan wrapper so we don't need the plugin's MCP tools
# (they don't load in `claude --print` mode anyway).
#
# USAGE
#   .\send-to-telegram.ps1 -Message "hello world"
#   .\send-to-telegram.ps1 -MessageFile "path\to\summary.txt"
#   .\send-to-telegram.ps1 -Message "*bold* test" -ParseMode "Markdown"

param(
    [string]$Message,
    [string]$MessageFile,
    [string]$ParseMode = "Markdown",
    [string]$EnvPath    = "$env:USERPROFILE\.claude\channels\telegram\.env",
    [string]$AccessPath = "$env:USERPROFILE\.claude\channels\telegram\access.json"
)

$ErrorActionPreference = "Stop"

# --- Resolve message text ---
if ($MessageFile) {
    if (-not (Test-Path -LiteralPath $MessageFile)) {
        Write-Error "MessageFile not found: $MessageFile"
        exit 1
    }
    # Force UTF-8 read. Windows PowerShell 5.1 defaults to the system codepage
    # (Windows-1252 in most locales), which mangles emoji and em-dashes.
    $Message = [System.IO.File]::ReadAllText($MessageFile, [System.Text.Encoding]::UTF8)
}
if (-not $Message -or $Message.Trim().Length -eq 0) {
    Write-Error "Message is empty. Pass -Message or -MessageFile."
    exit 1
}

# --- Resolve bot token from .env (or environment variable as fallback) ---
$token = $env:TELEGRAM_BOT_TOKEN
if (-not $token -and (Test-Path -LiteralPath $EnvPath)) {
    Get-Content -LiteralPath $EnvPath | ForEach-Object {
        if ($_ -match '^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+?)\s*$') {
            $token = $Matches[1].Trim('"').Trim("'")
        }
    }
}
if (-not $token) {
    Write-Error "TELEGRAM_BOT_TOKEN not found in env or $EnvPath. Run /telegram:configure <token> first."
    exit 1
}

# --- Resolve chat ID from access.json allowFrom[0] (or env var) ---
$chatId = $env:TELEGRAM_CHAT_ID
if (-not $chatId -and (Test-Path -LiteralPath $AccessPath)) {
    try {
        $access = Get-Content -LiteralPath $AccessPath -Raw | ConvertFrom-Json
        if ($access.allowFrom -and $access.allowFrom.Count -gt 0) {
            $chatId = "$($access.allowFrom[0])"
        }
    } catch {
        Write-Error "Failed to parse $AccessPath as JSON: $_"
        exit 1
    }
}
if (-not $chatId) {
    Write-Error "No chat ID found. Pair your account with /telegram:access pair <code> so allowFrom is populated, or set TELEGRAM_CHAT_ID env var."
    exit 1
}

# --- POST to Telegram ---
$url = "https://api.telegram.org/bot$token/sendMessage"
$body = @{
    chat_id    = $chatId
    text       = $Message
    parse_mode = $ParseMode
} | ConvertTo-Json -Compress -Depth 3

try {
    # Encode body as UTF-8 bytes manually. PS 5.1 Invoke-RestMethod -Body $string
    # can transcode through the system codepage, mangling emoji / em-dashes / etc.
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $response = Invoke-RestMethod -Uri $url -Method Post -Body $bodyBytes -ContentType "application/json; charset=utf-8"
    if ($response.ok) {
        Write-Output "Sent to chat_id $chatId (message_id: $($response.result.message_id))"
        exit 0
    } else {
        Write-Error "Telegram API responded with error: $($response | ConvertTo-Json -Depth 5)"
        exit 1
    }
} catch {
    Write-Error "Telegram POST failed: $_"
    exit 1
}
