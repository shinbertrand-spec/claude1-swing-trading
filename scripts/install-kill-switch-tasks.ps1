# Registers TWO Windows Task Scheduler jobs for the thematic-portfolio kill-switch:
#
#   1. ClaudeThematicKillSwitchMonitor  fires Process B's cycle every 5 min
#      from 9:30 AM to 4:00 PM ET on weekdays. Each tick:
#        - Reads Tiger paper positions
#        - Intersects with thematic-portfolio index
#        - Updates rolling peak + reads Aschenbrenner-kill-event flag
#        - Runs the 3-tier CPPI ladder
#        - On non-hold + --no-dry-run: cancel-then-place per thematic
#          position (proportional sell_fraction, floor-rounded shares)
#        - Appends events.jsonl + writes heartbeat.json
#        - Pushes Telegram alert on tier-1/2/3 fires
#
#   2. ClaudeThematicKillSwitchWatchdog  fires Process C every 5 min from
#      9:30 AM to 4:00 PM ET on weekdays. Each tick:
#        - Reads B's heartbeat.json
#        - Verdict: fresh / silent / missing (15 min RTH threshold)
#        - On fresh -> silent transition: writes
#          kill_switch_unavailable.json + Telegram alert (Loop 1 then
#          refuses to fire until B recovers)
#        - On silent -> fresh recovery: clears flag + recovery alert
#
# Both run Mon-Fri only, RTH only. Per the design:
#   [[swing-thematic-portfolio-kill-switch-architecture]] § Heartbeat
# Off-hours: the kill-switch is idle (no thematic positions to sell when
# the broker is closed). The orchestrator's pre-flight check still reads
# the flag, so a kill-event-flag set off-hours blocks Loop 1 at next
# invocation regardless of cadence.
#
# DEFAULTS ASSUME US EASTERN TIME. Override the -*LocalTime params if
# you're elsewhere:
#   US Pacific  -> 6:30 AM / 1:00 PM
#   UK (GMT)    -> 2:30 PM / 9:00 PM
#   Singapore   -> 9:30 PM / 4:00 AM next day (set MonitorDayOffset)
#
# USAGE
#   .\install-kill-switch-tasks.ps1
#   .\install-kill-switch-tasks.ps1 -MonitorStartLocalTime "9:30 PM" -MonitorEndLocalTime "4:00 AM" -MonitorDayOffset 0
#   .\install-kill-switch-tasks.ps1 -DryRun  # install but write --dry-run on the Monitor
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeThematicKillSwitchMonitor  | Get-ScheduledTaskInfo
#   Get-ScheduledTask -TaskName ClaudeThematicKillSwitchWatchdog | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeThematicKillSwitchMonitor   # test now
#   Disable-ScheduledTask -TaskName ClaudeThematicKillSwitchMonitor # pause
#   Unregister-ScheduledTask -TaskName ClaudeThematicKillSwitchMonitor

param(
    [string]$ProjectRoot          = "C:\Users\User\Desktop\Claude1",
    [string]$MonitorStartLocalTime= "9:30 AM",
    [string]$MonitorEndLocalTime  = "4:00 PM",
    [int]   $RepeatMinutes        = 5,
    [int]   $MonitorDayOffset     = 0,
    [string]$MonitorTaskName      = "ClaudeThematicKillSwitchMonitor",
    [string]$WatchdogTaskName     = "ClaudeThematicKillSwitchWatchdog",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate uv.exe — kill-switch processes are Python (not claude.exe like auto-paper)
$uvExe = $null
$onPath = Get-Command uv -ErrorAction SilentlyContinue
if ($onPath) {
    $uvExe = $onPath.Source
} else {
    $patterns = @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python*\Scripts\uv.exe",
        "$env:LOCALAPPDATA\uv\uv.exe"
    )
    foreach ($pattern in $patterns) {
        $match = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending | Select-Object -First 1
        if ($match) { $uvExe = $match.FullName; break }
    }
}
if (-not $uvExe -or -not (Test-Path -LiteralPath $uvExe)) {
    Write-Error "Could not locate uv.exe. Install uv (https://docs.astral.sh/uv) or pass its path explicitly."
    exit 1
}
Write-Output "Using uv.exe: $uvExe"

# ---------------- helpers ----------------------------------------------------

function New-KillSwitchAction {
    # Build the Task Scheduler action that invokes a Python module via uv.
    # Working directory is set to the project root so relative state-file
    # paths (ledgers/thematic/kill_switch/_state/) resolve correctly.
    param(
        [string]$Module,
        [string]$ExtraArgs = ""
    )

    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$uvExe' run python -m $Module $ExtraArgs
"@

    return New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""
}

function New-KillSwitchPrincipal {
    return New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
}

function New-KillSwitchSettings {
    # ExecutionTimeLimit set to 4 min — at 5-min cadence, a single tick
    # taking longer than 4 min indicates a hung Tiger call. The watchdog
    # picks up the resulting heartbeat staleness on the next pass.
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
}

function Get-WeekdayNamesShifted {
    param([int]$Offset = 0)
    $allDays = @("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")
    $baseIdx = 1,2,3,4,5
    return $baseIdx | ForEach-Object { $allDays[($_ + $Offset) % 7] }
}

function Register-KillSwitchRepeatingTask {
    # Repeat every $RepeatMinutes between StartLocalTime and EndLocalTime
    # on weekdays. Mirrors Register-AutoPaperTaskRepeating semantics.
    param(
        [string]$Name,
        [string]$Module,
        [string]$ExtraArgs,
        [string]$StartLocalTime,
        [string]$EndLocalTime,
        [int]   $RepeatMinutes,
        [string]$Description,
        [int]   $DayOffset = 0
    )

    $startDt = [DateTime]::Parse($StartLocalTime)
    $endDt   = [DateTime]::Parse($EndLocalTime)
    if ($endDt -le $startDt) {
        # Cross-midnight window — interpret end as next calendar day.
        $endDt = $endDt.AddDays(1)
    }
    $duration = $endDt - $startDt

    $days = Get-WeekdayNamesShifted -Offset $DayOffset

    $action = New-KillSwitchAction -Module $Module -ExtraArgs $ExtraArgs

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
        -Principal (New-KillSwitchPrincipal) `
        -Settings (New-KillSwitchSettings) `
        -Description $Description | Out-Null

    $dayList = ($days -join ",")
    $durMsg = if ($endDt.Date -gt $startDt.Date) { "until $EndLocalTime next day" } else { "until $EndLocalTime" }
    Write-Output "  Registered: $Name @ $StartLocalTime, every ${RepeatMinutes}min $durMsg ($dayList)"
}

# ---------------- install both -----------------------------------------------

$monitorMode = if ($DryRun) { "--once --dry-run" } else { "--once --no-dry-run" }

Write-Output ""
Write-Output "Installing thematic-portfolio kill-switch Task Scheduler jobs..."
Write-Output "Monitor mode: $monitorMode"
Write-Output ""

Register-KillSwitchRepeatingTask `
    -Name $MonitorTaskName `
    -Module "tools.thematic_portfolio.kill_switch.monitor" `
    -ExtraArgs $monitorMode `
    -StartLocalTime $MonitorStartLocalTime `
    -EndLocalTime $MonitorEndLocalTime `
    -RepeatMinutes $RepeatMinutes `
    -DayOffset $MonitorDayOffset `
    -Description "Thematic-portfolio kill-switch Process B: 3-tier CPPI ladder monitor, places paper sells on tier-1/2/3 fires"

Register-KillSwitchRepeatingTask `
    -Name $WatchdogTaskName `
    -Module "tools.thematic_portfolio.kill_switch.watchdog" `
    -ExtraArgs "" `
    -StartLocalTime $MonitorStartLocalTime `
    -EndLocalTime $MonitorEndLocalTime `
    -RepeatMinutes $RepeatMinutes `
    -DayOffset $MonitorDayOffset `
    -Description "Thematic-portfolio kill-switch Process C: heartbeat watchdog. Sets/clears kill_switch_unavailable.json + Telegram alerts on B silence transitions"

Write-Output ""
Write-Output "Done. Working dir for both jobs: $ProjectRoot"
Write-Output ""
Write-Output "IMPORTANT: $MonitorStartLocalTime / $MonitorEndLocalTime are LOCAL time."
Write-Output "Defaults assume US Eastern. Adjust the -Monitor*LocalTime parameters for your zone."
Write-Output ""
if ($DryRun) {
    Write-Output "DRY-RUN MODE: Monitor will skip order placement. Re-run without -DryRun to enable live (paper) selling."
    Write-Output ""
}
Write-Output "Test now:"
Write-Output "  Start-ScheduledTask -TaskName $MonitorTaskName"
Write-Output "  Start-ScheduledTask -TaskName $WatchdogTaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $MonitorTaskName  | Get-ScheduledTaskInfo"
Write-Output "  Get-ScheduledTask -TaskName $WatchdogTaskName | Get-ScheduledTaskInfo"
Write-Output ""
Write-Output "State files (gitignored, regenerable):"
Write-Output "  ledgers/thematic/kill_switch/_state/peak.json"
Write-Output "  ledgers/thematic/kill_switch/_state/heartbeat.json"
Write-Output "  ledgers/thematic/kill_switch/_state/events.jsonl"
Write-Output "  ledgers/thematic/kill_switch/_state/aschenbrenner_kill_event.json"
Write-Output "  ledgers/thematic/kill_switch/_state/kill_switch_unavailable.json   <- watchdog flag (orchestrator reads)"
