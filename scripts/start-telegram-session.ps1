# Starts a persistent Claude Code session with the telegram MCP channel enabled.
# Keep this terminal window open for the bot to receive your DMs.
#
# USAGE
#   .\scripts\start-telegram-session.ps1
#
# WHAT IT DOES
#   - Discovers claude.exe (system PATH, then Antigravity/VSCode/Cursor extension dirs)
#   - cd's into the project root
#   - Launches `claude --channels plugin:telegram@claude-plugins-official` interactively
#
# Once started, you can DM your Telegram bot from your phone. Messages flow
# into the session here, and replies via the `reply` MCP tool go back to your
# Telegram chat. To stop the session: press Ctrl+C in this window, or close it.
#
# To run automatically at Windows logon, see the "auto-start" note at the
# bottom of this file.

param(
    [string]$ProjectRoot = "C:\Users\User\Desktop\Claude1"
)

$ErrorActionPreference = "Stop"

# ---- Locate claude.exe (same logic as install-morning-task.ps1) ----
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
    Write-Error "Could not locate claude.exe. Searched PATH and extension dirs under .antigravity, .vscode, .cursor."
    exit 1
}

# ---- Start interactive session with telegram channel enabled ----
Set-Location -LiteralPath $ProjectRoot
Write-Output ""
Write-Output "Starting persistent Claude session with telegram channel..."
Write-Output "  claude.exe:    $claudeExe"
Write-Output "  project root:  $ProjectRoot"
Write-Output ""
Write-Output "DM your Telegram bot to talk to it. Press Ctrl+C here to stop."
Write-Output "Tip: /morning-deep-dive JCI GOOGL  (sent from Telegram) will deep-dive those tickers."
Write-Output ""

& $claudeExe --channels "plugin:telegram@claude-plugins-official"

# ---- Auto-start note ----
# To start this script automatically when you log in to Windows:
#
# 1. Create a shortcut in your Startup folder pointing to:
#       powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\User\Desktop\Claude1\scripts\start-telegram-session.ps1"
#
#    Startup folder location: press Win+R, paste:  shell:startup
#
# 2. Or register a logon-triggered scheduled task. Example:
#       $action = New-ScheduledTaskAction -Execute "powershell.exe" `
#           -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\User\Desktop\Claude1\scripts\start-telegram-session.ps1`""
#       $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
#       Register-ScheduledTask -TaskName "ClaudeTelegramSession" -Action $action -Trigger $trigger
#
# Either way: the session runs in a console window — keep it open (minimize is fine).
