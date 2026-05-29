# Shadow-validation cron for /auto-paper-v2 (LLM/Python boundary refactor).
#
# Per [auto-paper LLM/Python boundary refactor 2026-05-28] §5.1 (migration):
# v2 ships as a parallel cron task running ALONGSIDE v1 for 3 trading days.
# Both fire at the same time; outcomes are compared day-by-day. After 3
# clean parallel runs, v1 retires and v2 is renamed in-place. Until then,
# v1 (ClaudeTradingAutoPaperEntry) MUST continue running unchanged — this
# script does NOT touch v1.
#
# Registers ONE Windows Task Scheduler job:
#
#   ClaudeTradingAutoPaperEntryV2  fires /auto-paper-v2 at the same local
#   time as v1's ClaudeTradingAutoPaperEntry (default 9:35 AM ET).
#
# /auto-paper-monitor and /auto-paper-reconcile are NOT duplicated — those
# loops operate on broker / ledger state and are agnostic to which entry
# command produced the position. A single Monitor + Reconcile pair serves
# both v1 and v2 placements.
#
# Pattern match to v1 (Fix 2 durable patch, 2026-05-28):
#   * S4U logon (no password storage, no interactive-logon dependency)
#   * Hidden window (powershell.exe) + CREATE_NO_WINDOW (claude.exe child,
#     enforced by tools/observability/run_and_push.py when -EnableDiscord
#     is set; otherwise -WindowStyle Hidden covers the parent)
#   * bypassPermissions for the slash command (cron is unattended)
#   * 60 min execution time limit (panel + skeptic + placement
#     comfortably finishes inside this envelope; raise if telemetry
#     shows otherwise)
#   * Battery-tolerant + wake-to-run + start-when-available
#
# DEFAULTS ASSUME YOU ARE ON US EASTERN TIME. Pass -EntryLocalTime to
# match v1's schedule on your zone. Common offsets:
#   US Pacific  -> 6:35 AM
#   UK (GMT)    -> 2:35 PM
#   Malaysia/Singapore (SGT, UTC+8) during EDT -> 9:35 PM same day
#
# USAGE
#   .\install-auto-paper-v2-shadow.ps1                       # install (defaults: ET)
#   .\install-auto-paper-v2-shadow.ps1 -EntryLocalTime "9:35 PM"  # SGT
#   .\install-auto-paper-v2-shadow.ps1 -Uninstall            # remove the v2 task
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperEntryV2 | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeTradingAutoPaperEntryV2   # smoke test now
#   Disable-ScheduledTask -TaskName ClaudeTradingAutoPaperEntryV2 # pause
#   Enable-ScheduledTask  -TaskName ClaudeTradingAutoPaperEntryV2 # resume
#
# RETIREMENT (post-shadow-validation, per spec §5.1.5):
#   1. .\install-auto-paper-v2-shadow.ps1 -Uninstall      # remove v2 task
#   2. Unregister-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry -Confirm:$false
#   3. Rename .claude/commands/auto-paper-v2.md -> auto-paper.md
#      (overwriting v1; v1 archived at _archive/auto-paper-v1-2026-05-28.md)
#   4. .\install-auto-paper-tasks.ps1   # re-register Entry (now firing v2)

param(
    [switch]$Uninstall,
    [string]$ProjectRoot          = "C:\Users\User\Desktop\Claude1",
    [string]$EntryLocalTime       = "9:35 AM",
    [int]   $EntryDayOffset       = 0,
    [string]$EntryTaskName        = "ClaudeTradingAutoPaperEntryV2",
    # --- Discord observability (opt-in; same shape as v1) ---
    # When set, wraps the slash command in tools.observability.run_and_push
    # so the cron's stdout (PHASE_INIT_OK / PHASE_POST_SKEPTIC_OK /
    # PHASE_POST_PANEL_OK markers + per-ticker PLACE lines) lands in
    # Discord. Pair with a v2-specific channel so v1 / v2 outputs don't
    # collide during the shadow window.
    [switch]$EnableDiscord,
    [string]$EntryDiscordChannel  = "paper-auto-entry-v2"
)

$ErrorActionPreference = "Stop"

# ---------------- Uninstall path -------------------------------------------

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $EntryTaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $EntryTaskName -Confirm:$false
        Write-Output "Uninstalled task: $EntryTaskName"
    } else {
        Write-Output "No task to uninstall (not found): $EntryTaskName"
    }
    Write-Output ""
    Write-Output "v1 task (ClaudeTradingAutoPaperEntry) — if present — is UNCHANGED by this script."
    Write-Output "  Get-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry | Get-ScheduledTaskInfo"
    exit 0
}

# ---------------- Install path ---------------------------------------------

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate claude.exe (same logic as install-auto-paper-tasks.ps1).
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

# Locate uv.exe when -EnableDiscord is set (needed by run_and_push wrapper).
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

# ---------------- Action / Principal / Settings builders -------------------

if ($EnableDiscord) {
    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$uvExe' run python -m tools.observability.run_and_push --claude-exe '$claudeExe' --slash-command '/auto-paper-v2' --discord-channel '$EntryDiscordChannel' --project-root '$ProjectRoot'
"@
} else {
    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$claudeExe' --print --permission-mode bypassPermissions '/auto-paper-v2'
"@
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""

# S4U logon — Fix 2 durable patch (2026-05-28). No password storage, no
# interactive-logon dependency, no console flicker. HTTPS calls (Tiger API,
# Anthropic API, yfinance, Discord webhook) all work fine under S4U's
# restricted token; we only need anonymous-TLS.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Limited

# 60-min envelope mirrors v1's Entry task (raised from 20 min on 2026-05-27
# for the Phase 3 critic panel; v2 ships with the same panel + extra Python
# orchestration, so the same envelope applies).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60)

# ---------------- Trigger --------------------------------------------------

# DayOffset: shifts the Mon-Fri weekly trigger forward by N days. Same
# semantics as install-auto-paper-tasks.ps1 — use when local time is east
# of ET and a US trading day's run fires in your overnight or next morning.
$allDays  = @("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")
$baseIdx  = 1,2,3,4,5
$daysList = $baseIdx | ForEach-Object { $allDays[($_ + $EntryDayOffset) % 7] }

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $daysList `
    -At $EntryLocalTime

# ---------------- Idempotent re-register -----------------------------------

$existing = Get-ScheduledTask -TaskName $EntryTaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $EntryTaskName -Confirm:$false
    Write-Output "Removed existing task: $EntryTaskName"
}

Register-ScheduledTask `
    -TaskName $EntryTaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Claude Code v2 SHADOW: /auto-paper-v2 (LLM/Python boundary refactor 2026-05-28). Runs PARALLEL to v1 ClaudeTradingAutoPaperEntry for 3 trading days; retire v1 after outcomes confirmed identical." | Out-Null

$dayMsg = ($daysList -join ",")
Write-Output ""
Write-Output "Registered: $EntryTaskName @ $EntryLocalTime ($dayMsg)"
Write-Output "  Slash command: /auto-paper-v2"
Write-Output "  Project root:  $ProjectRoot"
$discordStatus = if ($EnableDiscord) { "ENABLED ($EntryDiscordChannel)" } else { "disabled" }
Write-Output "  Discord push:  $discordStatus"
Write-Output ""
Write-Output "v1 task (ClaudeTradingAutoPaperEntry) is UNCHANGED — both will fire in parallel."
Write-Output ""
Write-Output 'Smoke test now (fires immediately, returns exit code only after full run):'
Write-Output "  Start-ScheduledTask -TaskName $EntryTaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $EntryTaskName | Get-ScheduledTaskInfo"
Write-Output ""
Write-Output 'Compare outcomes across the shadow window:'
Write-Output '  - v1 placements -> ledgers/paper-auto/<TICKER>.yml (existing flow)'
Write-Output '  - v2 placements -> ledgers/paper-auto/<TICKER>.yml (same target)'
Write-Output '  - v2 run artifacts -> ledgers/_auto_paper_runs/<run_id>/'
Write-Output ""
Write-Output 'Retire v1 after 3 clean parallel days - see RETIREMENT section in script header.'
