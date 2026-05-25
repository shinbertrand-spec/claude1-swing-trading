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
    [int]   $EntryDayOffset       = 0,
    [string]$MonitorStartLocalTime= "10:00 AM",
    [string]$MonitorEndLocalTime  = "3:30 PM",
    [int]   $MonitorRepeatMinutes = 30,
    [int]   $MonitorDayOffset     = 0,
    [string]$ReconcileLocalTime   = "4:30 PM",
    [int]   $ReconcileDayOffset   = 0,
    [string]$EntryTaskName        = "ClaudeTradingAutoPaperEntry",
    [string]$MonitorTaskName      = "ClaudeTradingAutoPaperMonitor",
    [string]$ReconcileTaskName    = "ClaudeTradingAutoPaperReconcile",
    # --- Discord observability (opt-in) ---
    # When -EnableDiscord is set, each task wraps its slash command in
    # tools.observability.run_and_push, which captures stdout and POSTs it
    # to the named Discord webhook (~/.claude/channels/discord/.env).
    # Webhook lookup is best-effort: missing webhook URL -> task still runs,
    # just skips the push. Lets you install the cron BEFORE finishing Discord
    # channel setup.
    [switch]$EnableDiscord,
    [string]$EntryDiscordChannel     = "paper-auto-entry",
    [string]$MonitorDiscordChannel   = "paper-auto-monitor",
    [string]$ReconcileDiscordChannel = "paper-auto-reconcile"
)

# DayOffset: shifts the Mon-Fri trigger forward by N days. Use when your local
# time is east of ET and a US trading day's slash command fires in your
# overnight or next-morning. Example for Singapore (SGT, UTC+8) during EDT
# (May-Nov, UTC-4 — 12 hours behind SGT):
#   - Entry: 9:35 AM ET = 9:35 PM SGT same day. EntryDayOffset = 0.
#   - Monitor: 10:00 AM ET = 10:00 PM SGT same-day start. MonitorDayOffset = 0.
#       (Monitor's repetitions DO cross midnight; script handles that below.)
#   - Reconcile: 4:30 PM ET = 4:30 AM SGT *next* day → fires Tue-Sat SGT for
#       ET Mon-Fri. ReconcileDayOffset = 1.

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

# Locate uv.exe when -EnableDiscord is set (required by tools.observability.run_and_push).
$uvExe = $null
if ($EnableDiscord) {
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
        Write-Error "Could not locate uv.exe (needed when -EnableDiscord). Install uv or pass without -EnableDiscord."
        exit 1
    }
    Write-Output "Using uv.exe:     $uvExe"
}

# ---------------- helpers ----------------------------------------------------

function New-AutoPaperAction {
    param(
        [string]$SlashCommand,
        [string]$DiscordChannel = ""
    )

    if ($EnableDiscord -and $DiscordChannel) {
        $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$uvExe' run python -m tools.observability.run_and_push --claude-exe '$claudeExe' --slash-command '$SlashCommand' --discord-channel '$DiscordChannel' --project-root '$ProjectRoot'
"@
    } else {
        $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$claudeExe' --print --permission-mode bypassPermissions '$SlashCommand'
"@
    }

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

function Get-WeekdayNamesShifted {
    # Return Mon-Fri shifted forward by $Offset days. Used to derive the
    # correct local trigger days when the US trading day's slash command
    # fires in the local next-day overnight (e.g. SGT reconcile).
    param([int]$Offset = 0)

    $allDays = @("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")
    # Mon=1..Fri=5 in $allDays indexing.
    $baseIdx = 1,2,3,4,5
    return $baseIdx | ForEach-Object { $allDays[($_ + $Offset) % 7] }
}

function Register-AutoPaperTaskOnce {
    # Once-per-weekday at a specific local time.
    param(
        [string]$Name,
        [string]$SlashCommand,
        [string]$LocalTime,
        [string]$Description,
        [int]   $DayOffset = 0,
        [string]$DiscordChannel = ""
    )

    $days = Get-WeekdayNamesShifted -Offset $DayOffset

    $action = New-AutoPaperAction -SlashCommand $SlashCommand -DiscordChannel $DiscordChannel
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek $days `
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

    $dayList = ($days -join ",")
    Write-Output "  Registered: $Name @ $LocalTime ($dayList)"
}

function Register-AutoPaperTaskRepeating {
    # Repeat every N minutes between StartLocalTime and EndLocalTime on weekdays.
    # Implemented as: a Weekly trigger at StartLocalTime, configured to repeat
    # every $RepeatMinutes for the duration (EndLocalTime - StartLocalTime).
    # When EndLocalTime <= StartLocalTime, the window is assumed to cross
    # midnight (end is on the next calendar day) — needed for SGT/HKT etc.
    # where the US session falls in local overnight.
    param(
        [string]$Name,
        [string]$SlashCommand,
        [string]$StartLocalTime,
        [string]$EndLocalTime,
        [int]   $RepeatMinutes,
        [string]$Description,
        [int]   $DayOffset = 0,
        [string]$DiscordChannel = ""
    )

    $startDt = [DateTime]::Parse($StartLocalTime)
    $endDt   = [DateTime]::Parse($EndLocalTime)
    if ($endDt -le $startDt) {
        # Cross-midnight window — interpret end as next calendar day.
        $endDt = $endDt.AddDays(1)
    }
    $duration = $endDt - $startDt

    $days = Get-WeekdayNamesShifted -Offset $DayOffset

    $action = New-AutoPaperAction -SlashCommand $SlashCommand -DiscordChannel $DiscordChannel

    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek $days `
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

    $dayList = ($days -join ",")
    $durMsg = if ($endDt.Date -gt $startDt.Date) { "until $EndLocalTime next day" } else { "until $EndLocalTime" }
    Write-Output "  Registered: $Name @ $StartLocalTime, every ${RepeatMinutes}min $durMsg ($dayList)"
}

# ---------------- install all three ------------------------------------------

Write-Output ""
Write-Output "Installing paper-auto Task Scheduler jobs..."
Write-Output ""

Register-AutoPaperTaskOnce `
    -Name $EntryTaskName `
    -SlashCommand "/auto-paper" `
    -LocalTime $EntryLocalTime `
    -DayOffset $EntryDayOffset `
    -DiscordChannel $EntryDiscordChannel `
    -Description "Claude Code autonomous paper-trade entry: picks from morning-scan candidates, places via Tiger paper API"

Register-AutoPaperTaskRepeating `
    -Name $MonitorTaskName `
    -SlashCommand "/auto-paper-monitor" `
    -StartLocalTime $MonitorStartLocalTime `
    -EndLocalTime $MonitorEndLocalTime `
    -RepeatMinutes $MonitorRepeatMinutes `
    -DayOffset $MonitorDayOffset `
    -DiscordChannel $MonitorDiscordChannel `
    -Description "Claude Code paper-auto intraday monitor: per-bar sell-decision composer auto-exit for starter-state positions"

Register-AutoPaperTaskOnce `
    -Name $ReconcileTaskName `
    -SlashCommand "/auto-paper-reconcile" `
    -LocalTime $ReconcileLocalTime `
    -DayOffset $ReconcileDayOffset `
    -DiscordChannel $ReconcileDiscordChannel `
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
