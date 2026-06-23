<#
.SYNOPSIS
  Register the entry-trigger watcher as a Windows Task Scheduler job.

.DESCRIPTION
  Fires `uv run python -m tools.watcher.run` hourly during US market hours plus a
  near-close run, so the watchlist's structured triggers are evaluated and
  Telegram alerts pushed without the operator prompting.

  The watcher is pure deterministic Python (no LLM/Claude in the loop) and
  self-gates: an empty watchlist or a name with no structured trigger is a
  clean no-op, so over-firing on holidays is harmless.

  Cadence (operator-selected 2026-06-24): HOURLY + near-close.
    - ClaudeTradingEntryWatcher : 10:00 AM ET, repeating every 60 min until
      4:00 PM ET, Mon-Fri  (the hourly sweep)
    - plus a near-close fire at 3:45 PM ET (caught by the 15:45 repetition edge)

  Times default to US Eastern wall-clock on this machine. Override with
  -OpenLocalTime / -CloseLocalTime / -NearCloseLocalTime for other zones.

.EXAMPLE
  .\scripts\install-watcher-task.ps1
#>
[CmdletBinding()]
param(
    [string]$OpenLocalTime     = "10:00",
    [string]$CloseLocalTime    = "16:00",
    [string]$NearCloseLocalTime = "15:45",
    [string]$TaskName          = "ClaudeTradingEntryWatcher",
    [string]$RepoRoot          = "C:\Users\User\Desktop\Claude1"
)

$ErrorActionPreference = "Stop"

# Resolve `uv` (same approach as the auto-paper installer).
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found on PATH or at $uv" }

$action = New-ScheduledTaskAction -Execute $uv `
    -Argument "run python -m tools.watcher.run" `
    -WorkingDirectory $RepoRoot

# Hourly sweep: daily at OpenLocalTime, repeat every 60 min for the trading day.
$open = [datetime]::ParseExact($OpenLocalTime, "HH:mm", $null)
$close = [datetime]::ParseExact($CloseLocalTime, "HH:mm", $null)
$durationMin = [int]($close - $open).TotalMinutes

$hourly = New-ScheduledTaskTrigger -Daily -At $open
$hourly.Repetition = (New-ScheduledTaskTrigger -Once -At $open `
    -RepetitionInterval (New-TimeSpan -Minutes 60) `
    -RepetitionDuration (New-TimeSpan -Minutes $durationMin)).Repetition

# Near-close fire (3:45 PM ET by default) — a dedicated daily trigger so the
# last actionable check lands just before the bell even if it misses an hourly edge.
$nearClose = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($NearCloseLocalTime, "HH:mm", $null))

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

# Mon-Fri only: gate via the trigger's DaysOfWeek by re-registering as weekly.
$hourly.DaysOfWeek = 62      # Mon-Fri bitmask (2+4+8+16+32)
$nearClose.DaysOfWeek = 62

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($hourly, $nearClose) -Settings $settings -Force `
    -Description "Entry-trigger watcher: evaluates watchlist triggers hourly + near-close, pushes Telegram on escalation. Advisory only." | Out-Null

Write-Host "Registered '$TaskName': hourly $OpenLocalTime-$CloseLocalTime + near-close $NearCloseLocalTime ET, Mon-Fri."
Write-Host "Runs: $uv run python -m tools.watcher.run  (in $RepoRoot)"
