# Registers TWO Windows Task Scheduler jobs for the thematic-portfolio
# X-timeline ingest (Aschenbrenner / SA-LP co-PMs / ensemble principals):
#
#   1. ClaudeThematicXIngestTier1    fires every hour, 24/7
#      Polls Tier 1 accounts only: @leopoldasch + @CarlShulman.
#      Primary signal source — hourly cadence ensures fresh corpus on
#      Loop 1 firings.
#
#   2. ClaudeThematicXIngestTier23   fires every 4 hours, 24/7
#      Polls Tier 2 + Tier 3 accounts: philip_trammell / AvitalBalwit /
#      sholtodouglas / bradgerstner / plaffont / TimWeiss_LSC.
#      Secondary signals — 4-hourly is the documented cadence per
#      [[swing-thematic-portfolio-x-ingest-decision]] § Polling cadence.
#
# Both run 7 days a week (X content is not market-hour-restricted —
# Aschenbrenner et al. tweet whenever). Per-account dedup via the
# state file at ledgers/thematic/corpus/x/_state/last_seen.json
# makes empty polls effectively free; total monthly cost is
# ~$0.30-$1 per the design spec § Pricing.
#
# UNLIKE the kill-switch installer, this runs uv directly (not via
# claude.exe) — the ingester is pure Python. No LLM tokens burned per
# poll. The downstream classifier (substantive-artifact-classifier
# subagent + x-scanner classifier subagent) consumes the on-disk
# YAMLs separately when Loop 1 fires.
#
# USAGE
#   .\install-x-ingest-tasks.ps1                     # defaults: hourly Tier 1, 4-hourly Tier 2/3
#   .\install-x-ingest-tasks.ps1 -DryRun             # install but write --dry-run on the actions
#   .\install-x-ingest-tasks.ps1 -Tier1Only          # skip Tier 2/3 registration
#   .\install-x-ingest-tasks.ps1 -Tier23Only         # skip Tier 1 registration
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeThematicXIngestTier1   | Get-ScheduledTaskInfo
#   Get-ScheduledTask -TaskName ClaudeThematicXIngestTier23  | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeThematicXIngestTier1     # test now (1 poll)
#   Disable-ScheduledTask -TaskName ClaudeThematicXIngestTier1   # pause (e.g. account suspension)
#   Unregister-ScheduledTask -TaskName ClaudeThematicXIngestTier1 -Confirm:$false

param(
    [string]$ProjectRoot      = "C:\Users\User\Desktop\Claude1",
    [string]$Tier1TaskName    = "ClaudeThematicXIngestTier1",
    [string]$Tier23TaskName   = "ClaudeThematicXIngestTier23",
    # Cadence params. Default Tier 1 = every 60 min; Tier 2/3 = every 240 min.
    [int]   $Tier1IntervalMinutes  = 60,
    [int]   $Tier23IntervalMinutes = 240,
    # Pagination depth per account per fire. Default 1 page = up to 20 tweets;
    # at hourly cadence on accounts that post a few times a week, 1 page is
    # plenty. Increase for backfill / catch-up scenarios.
    [int]   $MaxPagesPerAccount    = 1,
    [switch]$Tier1Only,
    [switch]$Tier23Only,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate uv.exe — same pattern as install-kill-switch-tasks.ps1
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

# Pre-flight: verify the twitterapi.io credential is set. If absent, the
# tasks will install fine but every fire will exit with TwitterAPIAuthError
# — flag this loudly.
$credPath = "$env:USERPROFILE\.claude\channels\twitterapi\.env"
if (-not (Test-Path -LiteralPath $credPath)) {
    Write-Warning "twitterapi.io credential NOT FOUND at $credPath"
    Write-Warning "  The cron will fire but every poll will fail with TwitterAPIAuthError."
    Write-Warning "  Create the file with TWITTERAPI_IO_API_KEY=... or the tasks won't work."
    Write-Warning ""
}

# ---------------- helpers ----------------------------------------------------

function New-XIngestAction {
    param(
        [string]$TierArgs,           # e.g. "--tier 1" or "--tier 2 --tier 3"
        [int]   $MaxPages
    )

    $dryRunFlag = ""
    if ($DryRun) { $dryRunFlag = " --dry-run" }

    # uv run python -m <module> <tier> --max-pages-per-account N [--dry-run]
    # Stdout/stderr land in the Task Scheduler "Last Run Result" log.
    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$uvExe' run python -m tools.thematic_portfolio.corpus.x_ingest $TierArgs --max-pages-per-account $MaxPages$dryRunFlag
"@

    return New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""
}

function New-XIngestPrincipal {
    return New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
}

function New-XIngestSettings {
    # ExecutionTimeLimit set to 10 min — each fire makes <=8 advanced_search
    # calls (one per Tier 2/3 account, since Tier 1 fires alone). Each call
    # is sub-second; 10 min is generous headroom for network hiccups +
    # retries on 429. (The client itself caps at 3 retries with exponential
    # backoff.)
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
}

function Register-XIngestRepeatingTask {
    # Repeat every $IntervalMinutes for 24 hours starting at midnight,
    # 7 days a week. Net effect: hourly (or 4-hourly) polling 24/7.
    param(
        [string]$Name,
        [string]$TierArgs,
        [int]   $IntervalMinutes,
        [string]$Description
    )

    $startTime = "12:00 AM"

    $action = New-XIngestAction -TierArgs $TierArgs -MaxPages $MaxPagesPerAccount

    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Saturday `
        -At $startTime
    $trigger.Repetition = (New-ScheduledTaskTrigger `
        -Once -At $startTime `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Output "Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal (New-XIngestPrincipal) `
        -Settings (New-XIngestSettings) `
        -Description $Description | Out-Null

    $totalFires = [math]::Floor(24 * 60 / $IntervalMinutes)
    Write-Output "Registered: $Name ($totalFires fires/day, every $IntervalMinutes min, 24/7)"
}

# ---------------- registration ----------------------------------------------

if (-not $Tier23Only) {
    Register-XIngestRepeatingTask `
        -Name $Tier1TaskName `
        -TierArgs "--tier 1" `
        -IntervalMinutes $Tier1IntervalMinutes `
        -Description "Claude1 thematic-portfolio X-ingest Tier 1 (Aschenbrenner + Shulman), hourly 24/7"
}

if (-not $Tier1Only) {
    Register-XIngestRepeatingTask `
        -Name $Tier23TaskName `
        -TierArgs "--tier 2 --tier 3" `
        -IntervalMinutes $Tier23IntervalMinutes `
        -Description "Claude1 thematic-portfolio X-ingest Tier 2/3 (Trammell + Avital + Sholto + Gerstner + Laffont + Weiss), every 4h 24/7"
}

Write-Output ""
Write-Output "==== Install summary ===="
Write-Output "  Tier 1 (hourly):     leopoldasch + CarlShulman"
Write-Output "  Tier 2/3 (4hrly):    philip_trammell + AvitalBalwit + sholtodouglas + bradgerstner + plaffont + TimWeiss_LSC"
Write-Output "  Max pages / account: $MaxPagesPerAccount (= up to $(20 * $MaxPagesPerAccount) tweets per fire per account)"
if ($DryRun) {
    Write-Output "  Mode:                --dry-run (artifacts NOT written; state NOT persisted)"
} else {
    Write-Output "  Mode:                production (writes ledgers/thematic/corpus/x/<date>/<id>.yml + state)"
}
Write-Output ""
Write-Output "Test one fire now (without waiting):"
Write-Output "  Start-ScheduledTask -TaskName $Tier1TaskName"
Write-Output "  Start-ScheduledTask -TaskName $Tier23TaskName"
Write-Output ""
Write-Output "Inspect last run:"
Write-Output "  Get-ScheduledTask -TaskName $Tier1TaskName | Get-ScheduledTaskInfo"
Write-Output ""
Write-Output "Pause / resume:"
Write-Output "  Disable-ScheduledTask -TaskName $Tier1TaskName"
Write-Output "  Enable-ScheduledTask  -TaskName $Tier1TaskName"
Write-Output ""
Write-Output "Remove entirely:"
Write-Output "  Unregister-ScheduledTask -TaskName $Tier1TaskName  -Confirm:`$false"
Write-Output "  Unregister-ScheduledTask -TaskName $Tier23TaskName -Confirm:`$false"
