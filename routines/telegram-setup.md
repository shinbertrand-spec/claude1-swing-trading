# Telegram Pipeline — One-Time Setup

The morning candidate scan auto-posts to your Telegram via the `telegram@claude-plugins-official` plugin. Plugin is already installed; what follows is the one-time setup you do to make it actually deliver messages.

## Prerequisites

- A Telegram account on your phone.
- This Windows machine (the morning scan runs from here via Task Scheduler).

## Step 1 — Install Bun

The plugin's MCP server runs on Bun (a TypeScript runtime). Install it once:

```powershell
powershell -c "irm bun.sh/install.ps1 | iex"
```

Restart PowerShell after install. Verify:

```powershell
bun --version
```

If `bun --version` errors, the install didn't put it on PATH — check https://bun.sh/docs/installation/windows for manual instructions.

## Step 2 — Create your Telegram bot

1. On Telegram, search for **@BotFather** and open the chat.
2. Send `/newbot`.
3. Reply with a display name (anything — e.g., "My Trading Bot").
4. Reply with a username — must end in `bot`. Example: `bertrand_swing_trade_bot`.
5. BotFather replies with a token that looks like `1234567890:AAHfiqksKZ8...`. **Copy the whole thing including the leading digits and the colon.**

## Step 3 — Configure the token in Claude Code

Open your Claude Code session and run:

```
/telegram:configure 1234567890:AAHfiqksKZ8...
```

(Paste your actual token.) This writes `TELEGRAM_BOT_TOKEN` to `~/.claude/channels/telegram/.env`.

## Step 4 — Relaunch with the channels flag

The MCP server only connects when Claude is started with the `--channels` flag. Exit your current session and start a new one from your terminal:

```powershell
claude --channels plugin:telegram@claude-plugins-official
```

You're back inside a Claude Code session, but now the Telegram bridge is live.

## Step 5 — Pair your account

1. On Telegram, search for your new bot's username (the one ending in `bot`) and DM it. Send any text — e.g., `hi`.
2. The bot replies with a 6-character pairing code, e.g., `a4f91c`.
3. In your running Claude Code session, run:

```
/telegram:access pair a4f91c
```

The bot confirms back on Telegram. Your numeric user ID is now stored in `~/.claude/channels/telegram/access.json`.

## Step 6 — Lock down the bot

Once you're paired, switch the policy from `pairing` (which replies to any stranger with a code) to `allowlist` (which silently drops messages from non-allowed senders):

```
/telegram:access policy allowlist
```

## Step 7 — Register the Windows Task Scheduler entry

From this project root in PowerShell:

```powershell
.\scripts\install-morning-task.ps1
```

By default this schedules the morning scan at **9:45 AM local time** Monday–Friday. If your local time isn't US Eastern, pass `-LocalTime` to match 9:45 AM ET in your zone:

```powershell
# US Pacific
.\scripts\install-morning-task.ps1 -LocalTime "6:45 AM"

# Malaysia (MYT, UTC+8)
.\scripts\install-morning-task.ps1 -LocalTime "9:45 PM"

# UK (BST, UTC+1)
.\scripts\install-morning-task.ps1 -LocalTime "2:45 PM"
```

## Step 8 — Smoke test the pipeline

Without waiting for the next 9:45 AM fire:

```powershell
Start-ScheduledTask -TaskName ClaudeTradingMorningScan
```

Within ~1–3 minutes, you should see:
- 3 candidate tickers in your Telegram chat
- The full candidate report at `journal/candidates/YYYY-MM-DD.md`

If nothing arrives in Telegram:
- Check the task's last run result: `Get-ScheduledTask -TaskName ClaudeTradingMorningScan | Get-ScheduledTaskInfo`
- Check the candidates file did get written
- Verify your session is still paired: `/telegram:access` (shows current state)

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Task fires but no Telegram message | Session not relaunched with `--channels` after pairing | Restart Claude with the flag and re-run `Start-ScheduledTask` |
| `claude` not found by Task Scheduler | Claude Code CLI not on system PATH | Add Claude Code's install dir to PATH; restart the machine; re-run install script |
| Bot replies with pairing code instead of forwarding messages | Sender isn't on the allowlist | `/telegram:access pair <code>` to add them |
| 9:45 AM ET fires at the wrong local hour | DST drift between ET and your local zone | Re-run `install-morning-task.ps1 -LocalTime "..."` with the correct local hour |

## Disabling temporarily (market holidays, vacation)

```powershell
Disable-ScheduledTask -TaskName ClaudeTradingMorningScan
# Re-enable later:
Enable-ScheduledTask  -TaskName ClaudeTradingMorningScan
```

## Removing entirely

```powershell
Unregister-ScheduledTask -TaskName ClaudeTradingMorningScan -Confirm:$false
```
