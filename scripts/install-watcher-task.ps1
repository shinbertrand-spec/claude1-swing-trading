<#
.SYNOPSIS
  Register the entry-trigger watcher as a Windows Task Scheduler job.

.DESCRIPTION
  Fires `uv run python -m tools.watcher.run` hourly across the US market session
  (through the close), so the watchlist's structured triggers are evaluated and
  Telegram alerts pushed without the operator prompting.

  The watcher is pure deterministic Python (no LLM/Claude in the loop) and
  self-gates: an empty watchlist or a name with no structured trigger is a clean
  no-op, so over-firing is harmless.

  TIMES ARE THIS MACHINE'S LOCAL WALL-CLOCK. This box runs on Singapore time
  (UTC+8), so the defaults are the SGT equivalents of the US session:
    US 10:00-16:00 ET (EDT, summer) == 22:00-04:00 SGT (next day).
  The hourly sweep starts 22:00 SGT (=10:00 ET) and repeats every 60 min for
  ~6h15m, so the last check lands at the 16:00 ET close (the "near-close" run).
  Mon-Fri SGT start == Mon-Fri ET session.

  DST CAVEAT: these are fixed local times tuned for EDT (summer). When the US
  falls back to EST, the ET session shifts +1h vs SGT — re-run with
  -OpenLocalTime "23:00" -CloseLocalTime "05:15". (Same fixed-offset limitation
  as the existing auto-paper tasks.)

.EXAMPLE
  .\scripts\install-watcher-task.ps1
  .\scripts\install-watcher-task.ps1 -OpenLocalTime "23:00" -CloseLocalTime "05:15"   # EST/winter
#>
[CmdletBinding()]
param(
    [string]$OpenLocalTime  = "22:00",   # 10:00 ET (EDT) in SGT
    [string]$CloseLocalTime = "04:15",   # 16:15 ET (EDT) in SGT, next day
    [string]$TaskName       = "ClaudeTradingEntryWatcher",
    [string]$RepoRoot       = "C:\Users\User\Desktop\Claude1"
)

$ErrorActionPreference = "Stop"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found on PATH or at $uv" }

$action = New-ScheduledTaskAction -Execute $uv `
    -Argument "run python -m tools.watcher.run" `
    -WorkingDirectory $RepoRoot

# Hourly sweep across the session. Weekly trigger (not Daily) so we can pin
# Mon-Fri via -DaysOfWeek. Cross-midnight duration is handled (close < open).
$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$open = [datetime]::ParseExact($OpenLocalTime, "HH:mm", $null)
$close = [datetime]::ParseExact($CloseLocalTime, "HH:mm", $null)
$durationMin = [int]($close - $open).TotalMinutes
if ($durationMin -le 0) { $durationMin += 1440 }   # close is next calendar day

$hourly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $open
$hourly.Repetition = (New-ScheduledTaskTrigger -Once -At $open `
    -RepetitionInterval (New-TimeSpan -Minutes 60) `
    -RepetitionDuration (New-TimeSpan -Minutes $durationMin)).Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $hourly -Settings $settings -Force `
    -Description "Entry-trigger watcher: evaluates watchlist triggers hourly across the US session, pushes Telegram on escalation. Advisory only." | Out-Null

Write-Host "Registered '$TaskName': hourly from $OpenLocalTime for $([int]($durationMin/60))h$($durationMin%60)m, Mon-Fri (SGT) = US 10:00-16:00 ET session."
Write-Host "Runs: $uv run python -m tools.watcher.run  (in $RepoRoot)"
