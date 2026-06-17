# Registers ONE Windows Task Scheduler job for the self-observability cockpit:
#
#   ClaudeTradingHealthCheck  fires  tools.observability.health_check  at
#   10:20 AM ET (after the 9:35 entry task's 60-min window), then repeats
#   hourly until 4:20 PM ET. Mon-Fri.
#
# WHAT IT DOES (observe-only): reads the auto-paper run artifacts, computes the
# Health & Anomalies snapshot (silent-failure detector + cron overdue + feed
# probes), writes journal/observability/health_snapshot.json + health.html, and
# pushes edge-triggered Telegram alerts for the two page-level conditions
# (silent-failure; errors / feed-down / entry-overdue). It NEVER places, sizes,
# or mutates a trade — it lives entirely outside the placement path and always
# exits 0, so a health-check failure can neither look like nor cause a trading
# failure. Over-firing on holidays is harmless (no scheduled entry session that
# day → no alarm).
#
# The 10:20 ET first fire is the post-entry silent-failure catch: the 9:35 entry
# run completes within its 60-min window, so by 10:20 the placement results are
# on disk and the detector can read them. Hourly thereafter is the feed/overdue
# heartbeat.
#
# DEFAULTS ASSUME US EASTERN. Override -*LocalTime for other zones. Singapore
# (SGT, UTC+8) during EDT: 10:20 AM ET = 10:20 PM SGT same day → DayOffset 0.
#
# USAGE (must run elevated — Task Scheduler registration needs admin):
#   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-File','C:\Users\User\Desktop\Claude1\scripts\install-health-check-task.ps1'
#   .\install-health-check-task.ps1 -StartLocalTime "10:20 AM" -EndLocalTime "4:20 PM"
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeTradingHealthCheck | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeTradingHealthCheck      # test now
#   Unregister-ScheduledTask -TaskName ClaudeTradingHealthCheck # remove

param(
    [string]$ProjectRoot     = "C:\Users\User\Desktop\Claude1",
    [string]$StartLocalTime  = "10:20 AM",
    [string]$EndLocalTime    = "4:20 PM",
    [int]   $RepeatMinutes   = 60,
    [int]   $DayOffset       = 0,
    [string]$TaskName        = "ClaudeTradingHealthCheck",
    [int]   $MinutesLimit    = 10
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate uv.exe (the health check runs as a Python module, not a slash command).
$uvExe = $null
$onPath = Get-Command uv -ErrorAction SilentlyContinue
if ($onPath) {
    $uvExe = $onPath.Source
} else {
    $uvPatterns = @(
        "$env:USERPROFILE\AppData\Local\Programs\uv\uv.exe",
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($p in $uvPatterns) {
        if (Test-Path -LiteralPath $p) { $uvExe = $p; break }
    }
}
if (-not $uvExe) {
    Write-Error "Could not locate uv.exe. Install uv or add it to PATH."
    exit 1
}
Write-Output "Using uv.exe: $uvExe"

function Get-WeekdayNamesShifted {
    param([int]$Offset = 0)
    $allDays = @("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")
    $baseIdx = 1,2,3,4,5
    return $baseIdx | ForEach-Object { $allDays[($_ + $Offset) % 7] }
}

$innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$uvExe' run python -m tools.observability.health_check
"@

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""

$startDt = [DateTime]::Parse($StartLocalTime)
$endDt   = [DateTime]::Parse($EndLocalTime)
if ($endDt -le $startDt) { $endDt = $endDt.AddDays(1) }  # cross-midnight (e.g. SGT)
$duration = $endDt - $startDt

$days = Get-WeekdayNamesShifted -Offset $DayOffset

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $StartLocalTime
$trigger.Repetition = (New-ScheduledTaskTrigger `
    -Once -At $StartLocalTime `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) `
    -RepetitionDuration $duration).Repetition

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $MinutesLimit)

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
    -Description "Claude Code self-observability cockpit: observe-only health & anomalies snapshot + silent-failure Telegram alert. Reads run artifacts; never trades; always exits 0." | Out-Null

$dayList = ($days -join ",")
$durMsg = if ($endDt.Date -gt $startDt.Date) { "until $EndLocalTime next day" } else { "until $EndLocalTime" }
Write-Output "  Registered: $TaskName @ $StartLocalTime, every ${RepeatMinutes}min $durMsg ($dayList)"
Write-Output ""
Write-Output "Done. Working dir: $ProjectRoot"
Write-Output "Test now:  Start-ScheduledTask -TaskName $TaskName"
Write-Output "Inspect:   Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
