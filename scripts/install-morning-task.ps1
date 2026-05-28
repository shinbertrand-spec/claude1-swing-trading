# Registers the morning swing-trade candidate scan with Windows Task Scheduler.
# Fires every weekday at the time you specify (local time — see -LocalTime).
#
# USAGE
#   .\install-morning-task.ps1                              # uses defaults (9:45 AM local)
#   .\install-morning-task.ps1 -LocalTime "6:45 AM"         # if you're on US Pacific Time
#   .\install-morning-task.ps1 -LocalTime "9:45 PM"         # if you're on MYT (Malaysia)
#
# AFTER INSTALL — useful management commands:
#   Get-ScheduledTask -TaskName ClaudeTradingMorningScan | Get-ScheduledTaskInfo   # see next run time
#   Start-ScheduledTask -TaskName ClaudeTradingMorningScan                          # test now
#   Disable-ScheduledTask -TaskName ClaudeTradingMorningScan                        # pause (e.g. holidays)
#   Enable-ScheduledTask  -TaskName ClaudeTradingMorningScan                        # resume
#   Unregister-ScheduledTask -TaskName ClaudeTradingMorningScan -Confirm:$false     # remove

param(
    [string]$TaskName    = "ClaudeTradingMorningScan",
    [string]$ProjectRoot = "C:\Users\User\Desktop\Claude1",
    [string]$LocalTime   = "9:45 AM"   # Default assumes you ARE on US Eastern Time.
                                       # Adjust to your local equivalent of 9:45 AM ET if not.
)

$ErrorActionPreference = "Stop"

# Verify the project root exists
if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    Write-Error "Project root not found: $ProjectRoot"
    exit 1
}

# Locate claude.exe. Prefer system PATH; fall back to bundled extension binaries
# (Antigravity / VSCode / Cursor). Picks the highest-version extension dir match.
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
    Write-Error "Could not locate claude.exe. Searched system PATH and extension dirs under .antigravity, .vscode, .cursor. Install Claude Code or pass the binary path explicitly."
    exit 1
}
Write-Output "Using claude.exe: $claudeExe"

# Build the inner command:
#   - cd to project so relative paths resolve correctly
#   - launch claude in headless print mode with the telegram channel enabled
#   - invoke the morning-scan-telegram slash command
$innerCommand = @"
Set-Location -LiteralPath '$ProjectRoot'
`$today = (Get-Date).ToString('yyyy-MM-dd')
& '$claudeExe' --print --permission-mode bypassPermissions '/morning-scan-telegram'
`$summary = Join-Path '$ProjectRoot\journal\candidates' (`$today + '-summary.txt')
if (Test-Path -LiteralPath `$summary) {
    & '$ProjectRoot\scripts\send-to-telegram.ps1' -MessageFile `$summary
} else {
    Write-Error ('Morning scan did not produce summary file: ' + `$summary)
    exit 1
}
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
    -LogonType S4U `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 60)

# Remove any prior task with the same name (idempotent re-install)
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
    -Description "Claude Code morning swing-trade candidate scan -> Telegram push" | Out-Null

Write-Output ""
Write-Output "Registered scheduled task: $TaskName"
Write-Output "  Fires:       $LocalTime, Mon-Fri (local time)"
Write-Output "  Action:      claude --print `"/morning-scan-telegram`" --channels plugin:telegram@claude-plugins-official"
Write-Output "  Working dir: $ProjectRoot"
Write-Output ""
Write-Output "IMPORTANT: Local time, not ET. If you are NOT on US Eastern Time, re-run with -LocalTime set to your local equivalent of 9:45 AM ET. Common offsets:"
Write-Output "  US Pacific  -> 6:45 AM"
Write-Output "  US Central  -> 8:45 AM"
Write-Output "  UK (BST)    -> 2:45 PM"
Write-Output "  Malaysia    -> 9:45 PM"
Write-Output "  Japan       -> 10:45 PM"
Write-Output ""
Write-Output "Test now (without waiting for the next fire):"
Write-Output "  Start-ScheduledTask -TaskName $TaskName"
Write-Output ""
Write-Output "Inspect:"
Write-Output "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
