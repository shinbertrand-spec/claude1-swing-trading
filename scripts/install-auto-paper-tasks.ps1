# Registers THREE Windows Task Scheduler jobs for the paper-auto track:
#
#   1. ClaudeTradingAutoPaperEntry      fires /auto-paper at 9:35 AM ET
#      (5 min after open; lets the 9:30 morning-scan-telegram task finish
#      writing today's candidates file at journal/candidates/YYYY-MM-DD.md)
#
#   2. ClaudeTradingAutoPaperMonitor    fires /auto-paper-monitor every
#      30 min from 10:00 AM to 3:30 PM ET (Session 3 — per-bar
#      sell-decision composer auto-exit for starter-state positions)
#
#   3. ClaudeTradingAutoPaperReconcile  fires /auto-paper-reconcile at
#      4:30 PM ET (30 min after close; lets late-printing fills settle;
#      transitions submitted -> starter + auto-places broker-side stops)
#
# All three run Mon-Fri only. Each self-gates inside the slash command:
#   - /auto-paper exits clean if no candidates file for today
#   - /auto-paper-monitor exits clean if no starter-state positions
#   - /auto-paper-reconcile exits clean if nothing in `submitted` state
# so over-firing on holidays / non-trading days is harmless.
#
# DEFAULTS ASSUME YOU ARE ON US EASTERN TIME. If not, override the
# -*LocalTime parameters to your local equivalents. Common offsets:
#   US Pacific  -> 6:35 AM / 7:00 AM / 1:30 PM
#   UK (GMT)    -> 2:35 PM / 3:00 PM / 9:30 PM
#   Malaysia    -> 10:35 PM / 11:00 PM / 5:30 AM (next day) — careful!
#
# USAGE
#   .\install-auto-paper-tasks.ps1                          # defaults: ET times
#   .\install-auto-paper-tasks.ps1 -EntryLocalTime "6:35 AM" -ReconcileLocalTime "1:30 PM"
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry | Get-ScheduledTaskInfo
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperMonitor | Get-ScheduledTaskInfo
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperReconcile | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry         # test now
#   Disable-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry       # pause
#   Enable-ScheduledTask  -TaskName ClaudeTradingAutoPaperEntry       # resume
#   Unregister-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry    # remove

param(
    [string]$ProjectRoot          = "C:\Users\User\Desktop\Claude1",
    [string]$EntryLocalTime       = "9:35 AM",
    [string]$MonitorStartLocalTime= "10:00 AM",
    [string]$MonitorEndLocalTime  = "3:30 PM",
    [int]   $MonitorRepeatMinutes = 30,
    [string]$ReconcileLocalTime   = "4:30 PM",
    [string]$EntryTaskName        = "ClaudeTradingAutoPaperEntry",
    [string]$MonitorTaskName      = "ClaudeTradingAutoPaperMonitor",
    [string]$ReconcileTaskName    = "ClaudeTradingAutoPaperReconcile"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate claude.exe (same logic as install-news-hourly-task.ps1)
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

# ---------------- helpers ----------------------------------------------------

function New-AutoPaperAction {
    param([string]$SlashCommand)

    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$claudeExe' --print --permission-mode bypassPermissions '$SlashCommand'
"@

    return New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""
}

function New-AutoPaperPrincipal {
    return New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
}

function New-AutoPaperSettings {
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
}

function Register-AutoPaperTaskOnce {
    # Once-per-weekday at a specific local time.
    param(
        [string]$Name,
        [string]$SlashCommand,
        [string]$LocalTime,
        [string]$Description
    )

    $action = New-AutoPaperAction -SlashCommand $SlashCommand
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $LocalTime

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Output "Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal (New-AutoPaperPrincipal) `
        -Settings (New-AutoPaperSettings) `
        -Description $Description | Out-Null

    Write-Output "  Registered: $Name @ $LocalTime (Mon-Fri)"
}

function Register-AutoPaperTaskRepeating {
    # Repeat every N minutes between StartLocalTime and EndLocalTime on weekdays.
    # Implemented as: a Weekly trigger at StartLocalTime, configured to repeat
    # every $RepeatMinutes for the duration (EndLocalTime - StartLocalTime).
    param(
        [string]$Name,
        [string]$SlashCommand,
        [string]$StartLocalTime,
        [string]$EndLocalTime,
        [int]   $RepeatMinutes,
        [string]$Description
    )

    $startDt = [DateTime]::Parse($StartLocalTime)
    $endDt   = [DateTime]::Parse($EndLocalTime)
    if ($endDt -le $startDt) {
        throw "End time ($EndLocalTime) must be after start time ($StartLocalTime)"
    }
    $duration = $endDt - $startDt

    $action = New-AutoPaperAction -SlashCommand $SlashCommand

    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $StartLocalTime
    $trigger.Repetition = (New-ScheduledTaskTrigger `
        -Once -At $StartLocalTime `
        -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) `
        -RepetitionDuration $duration).Repetition

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Output "Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal (New-AutoPaperPrincipal) `
        -Settings (New-AutoPaperSettings) `
        -Description $Description | Out-Null

    Write-Output "  Registered: $Name @ $StartLocalTime, every ${RepeatMinutes}min until $EndLocalTime (Mon-Fri)"
}

# ---------------- install all three ------------------------------------------

Write-Output ""
Write-Output "Installing paper-auto Task Scheduler jobs..."
Write-Output ""

Register-AutoPaperTaskOnce `
    -Name $EntryTaskName `
    -SlashCommand "/auto-paper" `
    -LocalTime $EntryLocalTime `
    -Description "Claude Code autonomous paper-trade entry: picks from morning-scan candidates, places via Tiger paper API"

Register-AutoPaperTaskRepeating `
    -Name $MonitorTaskName `
    -SlashCommand "/auto-paper-monitor" `
    -StartLocalTime $MonitorStartLocalTime `
    -EndLocalTime $MonitorEndLocalTime `
    -RepeatMinutes $MonitorRepeatMinutes `
    -Description "Claude Code paper-auto intraday monitor: per-bar sell-decision composer auto-exit for starter-state positions"

Register-AutoPaperTaskOnce `
    -Name $ReconcileTaskName `
    -SlashCommand "/auto-paper-reconcile" `
    -LocalTime $ReconcileLocalTime `
    -Description "Claude Code paper-auto EOD reconcile: pulls filled orders, updates submitted-state ledgers to starter/closed, auto-places broker-side stop on starter transition"

Write-Output ""
Write-Output "Done. Working dir for all three jobs: $ProjectRoot"
Write-Output ""
Write-Output "IMPORTANT: $EntryLocalTime, $MonitorStartLocalTime/$MonitorEndLocalTime, and $ReconcileLocalTime are LOCAL time."
Write-Output "Defaults assume US Eastern. Adjust the -*LocalTime parameters if you're on a different zone."
Write-Output ""
Write-Output "Test entry now:"
Write-Output "  Start-ScheduledTask -TaskName $EntryTaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $EntryTaskName | Get-ScheduledTaskInfo"
Write-Output "  Get-ScheduledTask -TaskName $MonitorTaskName | Get-ScheduledTaskInfo"
Write-Output "  Get-ScheduledTask -TaskName $ReconcileTaskName | Get-ScheduledTaskInfo"
