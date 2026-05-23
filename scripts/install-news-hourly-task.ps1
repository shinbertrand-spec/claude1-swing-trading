# Registers the hourly news-snapshot job with Windows Task Scheduler.
# Fires every weekday at 10:00 local time and repeats hourly for 6 hours
# (defaults assume you ARE on US Eastern Time — adjust -LocalStartTime if not).
# Net effect: snapshots at 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00 ET.
#
# The slash command itself self-gates via the pre-flight "is the current hour
# in scope (09..16 ET)?" check, so over-firing is harmless — extra fires exit
# clean with NEWS_HOURLY_OUT_OF_HOURS and no file written.
#
# USAGE
#   .\install-news-hourly-task.ps1                              # defaults: 10:00 AM ET, repeat 6h
#   .\install-news-hourly-task.ps1 -LocalStartTime "7:00 AM"    # US Pacific
#   .\install-news-hourly-task.ps1 -LocalStartTime "10:00 PM"   # Malaysia
#
# AFTER INSTALL:
#   Get-ScheduledTask -TaskName ClaudeTradingNewsHourly | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeTradingNewsHourly        # test one fire now
#   Disable-ScheduledTask -TaskName ClaudeTradingNewsHourly      # pause (e.g. vacation)
#   Enable-ScheduledTask  -TaskName ClaudeTradingNewsHourly      # resume
#   Unregister-ScheduledTask -TaskName ClaudeTradingNewsHourly -Confirm:$false

param(
    [string]$TaskName        = "ClaudeTradingNewsHourly",
    [string]$ProjectRoot     = "C:\Users\User\Desktop\Claude1",
    [string]$LocalStartTime  = "10:00 AM",
    [int]$RepeatHours        = 6,
    [int]$IntervalMinutes    = 60
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate claude.exe — same logic as install-morning-task.ps1
$claudeExe = $null
$onPath = Get-Command claude -ErrorAction SilentlyContinue
if ($onPath) {
    $claudeExe = $onPath.Source
} else {
    $patterns = @(
        "$env:USERPROFILE\.antigravity\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe",
        "$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe",
        "$env:USERPROFILE\.cursor\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe"
    )
    foreach ($pattern in $patterns) {
        $match = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending | Select-Object -First 1
        if ($match) { $claudeExe = $match.FullName; break }
    }
}
if (-not $claudeExe -or -not (Test-Path -LiteralPath $claudeExe)) {
    Write-Error "Could not locate claude.exe. Searched system PATH and extension dirs under .antigravity, .vscode, .cursor."
    exit 1
}
Write-Output "Using claude.exe: $claudeExe"

# Build the inner command:
#   - cd to project so relative paths resolve correctly
#   - launch claude headless, run /news-hourly
#   - if a summary file was written for this hour, push it via the wrapper
$innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$claudeExe' --print --permission-mode bypassPermissions '/news-hourly'
& '$ProjectRoot\scripts\send-news-to-telegram.ps1'
"@

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""

# Trigger: weekly Mon-Fri at LocalStartTime, repeating hourly for $RepeatHours.
# We build a base weekly trigger then attach a repetition pattern to it.
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $LocalStartTime

$trigger.Repetition = (New-ScheduledTaskTrigger `
    -Once `
    -At $LocalStartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Hours $RepeatHours)).Repetition

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

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
    -Description "Claude Code hourly news snapshot -> Telegram push on material deltas" | Out-Null

Write-Output ""
Write-Output "Registered scheduled task: $TaskName"
Write-Output "  First fire:   $LocalStartTime, Mon-Fri (local time)"
Write-Output "  Repetition:   every $IntervalMinutes min for $RepeatHours hours"
Write-Output "  Total fires:  $([math]::Floor($RepeatHours * 60 / $IntervalMinutes) + 1) per weekday"
Write-Output "  Working dir:  $ProjectRoot"
Write-Output ""
Write-Output "IMPORTANT: $LocalStartTime is LOCAL time. If you are NOT on US Eastern Time, re-run with -LocalStartTime set to your local equivalent of 10:00 AM ET. Common offsets:"
Write-Output "  US Pacific  -> 7:00 AM"
Write-Output "  US Central  -> 9:00 AM"
Write-Output "  UK (BST)    -> 3:00 PM"
Write-Output "  Malaysia    -> 10:00 PM"
Write-Output "  Japan       -> 11:00 PM"
Write-Output ""
Write-Output "Test one fire now (without waiting):"
Write-Output "  Start-ScheduledTask -TaskName $TaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
