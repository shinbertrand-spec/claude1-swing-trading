# Wrapper for the hourly news-snapshot Telegram push. Locates today's
# HH-summary.txt under ledgers/news/YYYY-MM-DD/ (based on current ET hour)
# and delegates to the generic send-to-telegram.ps1.
#
# If the summary file doesn't exist for this hour, exits 0 silently — the
# news-hourly slash command only writes a summary when material_deltas is
# non-empty, so "no file" means "no push needed", not "something broke".
#
# USAGE
#   .\send-news-to-telegram.ps1                  # uses today's date + current ET hour
#   .\send-news-to-telegram.ps1 -Date 2026-05-23 -Hour 14   # specific snapshot

param(
    [string]$ProjectRoot = "C:\Users\User\Desktop\Claude1",
    [string]$Date        = "",
    [string]$Hour        = ""
)

$ErrorActionPreference = "Stop"

# --- Resolve ET date + hour ---
if (-not $Date -or -not $Hour) {
    $etNow = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Eastern Standard Time")
    if (-not $Date) { $Date = $etNow.ToString("yyyy-MM-dd") }
    if (-not $Hour) { $Hour = $etNow.ToString("HH") }
}

# Pad hour to 2 digits in case it was passed as "9"
$Hour = $Hour.PadLeft(2, "0")

$summaryPath = Join-Path $ProjectRoot "ledgers\news\$Date\$Hour-summary.txt"

if (-not (Test-Path -LiteralPath $summaryPath)) {
    Write-Output "No summary file for $Date $Hour:00 ET — nothing to push (this is normal when no material deltas fired)."
    exit 0
}

$content = [System.IO.File]::ReadAllText($summaryPath, [System.Text.Encoding]::UTF8)
if (-not $content -or $content.Trim().Length -eq 0) {
    Write-Output "Summary file is empty: $summaryPath — skipping push."
    exit 0
}

# Delegate. The news summary is plain text (not Markdown) — see ledgers/news/README.md
# § "Telegram push format". Pass ParseMode=None to keep things literal.
$sender = Join-Path $ProjectRoot "scripts\send-to-telegram.ps1"
if (-not (Test-Path -LiteralPath $sender)) {
    Write-Error "send-to-telegram.ps1 not found at: $sender"
    exit 1
}

& $sender -MessageFile $summaryPath -ParseMode "None"
exit $LASTEXITCODE
