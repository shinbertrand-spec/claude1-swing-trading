# Registers TWO Windows Task Scheduler jobs for the paper-auto track:
#
#   1. ClaudeTradingAutoPaperEntry      — fires /auto-paper at 9:35 AM ET
#      (5 min after open; lets the 9:30 morning-scan-telegram task finish
#      writing today's candidates file at journal/candidates/YYYY-MM-DD.md)
#
#   2. ClaudeTradingAutoPaperReconcile  — fires /auto-paper-reconcile at
#      4:30 PM ET (30 min after close; lets late-printing fills settle)
#
# Both run Mon-Fri only. Both self-gate inside the slash command:
#   - /auto-paper exits clean if no candidates file for today
#   - /auto-paper-reconcile exits clean if nothing in `submitted` state
# so over-firing on holidays / non-trading days is harmless.
#
# DEFAULTS ASSUME YOU ARE ON US EASTERN TIME. If not, override -EntryLocalTime
# and -ReconcileLocalTime to your local equivalents. Common offsets:
#   US Pacific  -> 6:35 AM / 1:30 PM
#   UK (GMT)    -> 2:35 PM / 9:30 PM
#   Malaysia    -> 10:35 PM / 5:30 AM (next day) — careful!
#
# USAGE
#   .\install-auto-paper-tasks.ps1                          # defaults: ET times
#   .\install-auto-paper-tasks.ps1 -EntryLocalTime "6:35 AM" -ReconcileLocalTime "1:30 PM"
#
# AFTER INSTALL
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry | Get-ScheduledTaskInfo
#   Get-ScheduledTask -TaskName ClaudeTradingAutoPaperReconcile | Get-ScheduledTaskInfo
#   Start-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry         # test now
#   Disable-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry       # pause
#   Enable-ScheduledTask  -TaskName ClaudeTradingAutoPaperEntry       # resume
#   Unregister-ScheduledTask -TaskName ClaudeTradingAutoPaperEntry    # remove

param(
    [string]$ProjectRoot         = "C:\Users\User\Desktop\Claude1",
    [string]$EntryLocalTime      = "9:35 AM",
    [string]$ReconcileLocalTime  = "4:30 PM",
    [string]$EntryTaskName       = "ClaudeTradingAutoPaperEntry",
    [string]$ReconcileTaskName   = "ClaudeTradingAutoPaperReconcile"
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

# ---------------- helper -----------------------------------------------------

function Register-AutoPaperTask {
    param(
        [string]$Name,
        [string]$SlashCommand,
        [string]$LocalTime,
        [string]$Description
    )

    $innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
& '$claudeExe' --print --permission-mode bypassPermissions '$SlashCommand'
"@

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$innerCommand`""

    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $LocalTime

    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Output "Removed existing task: $Name"
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description $Description | Out-Null

    Write-Output "  Registered: $Name @ $LocalTime (Mon-Fri)"
}

# ---------------- install both -----------------------------------------------

Write-Output ""
Write-Output "Installing paper-auto Task Scheduler jobs..."
Write-Output ""

Register-AutoPaperTask `
    -Name $EntryTaskName `
    -SlashCommand "/auto-paper" `
    -LocalTime $EntryLocalTime `
    -Description "Claude Code autonomous paper-trade entry — picks from morning-scan candidates, places via Tiger paper API"

Register-AutoPaperTask `
    -Name $ReconcileTaskName `
    -SlashCommand "/auto-paper-reconcile" `
    -LocalTime $ReconcileLocalTime `
    -Description "Claude Code paper-auto EOD reconcile — pulls filled orders, updates submitted-state ledgers to starter / closed"

Write-Output ""
Write-Output "Done. Working dir for both jobs: $ProjectRoot"
Write-Output ""
Write-Output "IMPORTANT: $EntryLocalTime and $ReconcileLocalTime are LOCAL time."
Write-Output "Defaults assume US Eastern. Adjust with -EntryLocalTime / -ReconcileLocalTime"
Write-Output "if you're on a different zone."
Write-Output ""
Write-Output "Test entry now:"
Write-Output "  Start-ScheduledTask -TaskName $EntryTaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $EntryTaskName | Get-ScheduledTaskInfo"
Write-Output "  Get-ScheduledTask -TaskName $ReconcileTaskName | Get-ScheduledTaskInfo"
