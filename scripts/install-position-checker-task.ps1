# Registers the position-checker scheduled task.
# Fires every 30 minutes, 24/7. The script self-exits when US market is closed,
# so we don't need complex schedule triggers.
#
# USAGE
#   .\install-position-checker-task.ps1
#
# DEFAULTS
#   Task name:      ClaudeTradingPositionChecker
#   Interval:       every 30 minutes
#   Duration:       indefinitely
#
# MANAGEMENT
#   Test now:   Start-ScheduledTask -TaskName ClaudeTradingPositionChecker
#   Pause:      Disable-ScheduledTask -TaskName ClaudeTradingPositionChecker
#   Resume:     Enable-ScheduledTask  -TaskName ClaudeTradingPositionChecker
#   Remove:     Unregister-ScheduledTask -TaskName ClaudeTradingPositionChecker -Confirm:$false

param(
    [string]$TaskName    = "ClaudeTradingPositionChecker",
    [string]$ProjectRoot = "C:\Users\User\Desktop\Claude1"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

$scriptPath = Join-Path $ProjectRoot "scripts\check-positions.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    Write-Error "Checker script not found: $scriptPath"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""

# Run every 30 min starting now, repeating indefinitely
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Idempotent re-install
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed existing task: $TaskName"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Checks open swing-trade positions every 30 min; posts Telegram alerts for stop proximity, catalysts, and profit milestones." | Out-Null

Write-Output ""
Write-Output "Registered scheduled task: $TaskName"
Write-Output "  Interval:    every 30 minutes (24/7)"
Write-Output "  Script:      $scriptPath"
Write-Output "  Market gate: script self-skips outside US 9:30-16:00 ET, M-F"
Write-Output ""
Write-Output "Test now:"
Write-Output "  Start-ScheduledTask -TaskName $TaskName"
Write-Output ""
Write-Output "Or run the script directly with -Force to bypass the market-hours gate:"
Write-Output "  powershell -ExecutionPolicy Bypass -File `"$scriptPath`" -Force"
