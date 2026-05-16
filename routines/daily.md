# Daily Trading Routine

Operational rhythm for the swing-trading agent on US market days. Read together
with `CLAUDE.md` (framework) and the agent prompts in `.claude/agents/`.

## Schedule

| Time (ET) | Activity | Trigger |
|-----------|----------|---------|
| 9:45 AM | Candidate scan → Telegram push | **Auto** (Windows Task Scheduler → `/morning-scan-telegram`) |
| 9:50–10:00 AM | You read the 3 candidates on your phone, decide picks | You |
| 10:00 AM | Deep-dive on picks → compliance check → trade approval | `/morning-deep-dive` |
| 12:00 PM | Midday check: fills, thesis breaks | Manual |
| 3:45 PM | Final-hour scan: trim / exit hitting targets | Manual |
| 4:15 PM | EOD journal write-up | `/eod-journal` |
| Friday 4:30 PM | Weekly review block | `/eod-journal` Step 5 (auto on Fridays) |

## Architecture summary

```
9:45 AM ET — Windows Task Scheduler
    ↓ fires
claude --print --channels plugin:telegram@claude-plugins-official "/morning-scan-telegram"
    ↓
risk-and-compliance (candidate-scan mode) → 3 candidates
    ↓
write to journal/candidates/YYYY-MM-DD.md
    ↓
telegram MCP `reply` tool → push summary to your Telegram

[ on your phone, you decide which 2 to pursue ]

At your desk:
    ↓
/morning-deep-dive
    ↓
read journal/candidates/YYYY-MM-DD.md
    ↓
[ you say which 2 ]
    ↓
for each pick:
    trade-researcher (deep-dive) → propose entry/stop/target
    risk-and-compliance (verification mode) → APPROVE / CONDITIONS / BLOCK
    ↓
[ you approve y/n per trade ]
    ↓
log trades to journal/Trading.md (or journal/YYYY-MM-DD.md)

4:15 PM ET (manual):
    ↓
/eod-journal
    ↓
trade-researcher (EOD mode) → market context, sector winners/losers
    ↓
write EOD section + watchlist + reflection to journal
```

## Agent responsibilities

### `trade-researcher` — 3 daily duties

1. **9:45 AM ET** — Deep-dive on each picked candidate (full 6-section ticker report). Triggered by `/morning-deep-dive` after the user picks 2.
2. **10:00 AM ET** — Technical evaluation of proposed trade parameters (sense-check entry/stop/target before compliance verification).
3. **4:15 PM ET** — EOD report: SPY/QQQ/VIX close, sector leaders/laggards, portfolio + watchlist moves, macro events.

Research-only. Returns Markdown. Does not write to files.

### `risk-and-compliance` — 2 modes

1. **9:45 AM ET — Candidate-scan mode** — Scan for 3 swing-trade candidates passing every framework hard rule. Invoked by `/morning-scan-telegram` (headless via Task Scheduler).
2. **10:00 AM ET — Verification mode** — Independent verification + framework rule check on each proposed trade. Verdict: APPROVE / APPROVE-WITH-CONDITIONS / BLOCK.

Research-only. Adversarial by design. Reads `CLAUDE.md` before judging.

## Slash commands

| Command | When | What it does |
|---------|------|--------------|
| `/morning-scan-telegram` | 9:45 AM ET (auto via Task Scheduler) | Headless. Generates 3 candidates and pushes summary to Telegram. Writes full report to `journal/candidates/YYYY-MM-DD.md`. |
| `/morning-deep-dive` | When you arrive at your desk | Reads today's candidate file, asks which 2 you picked, runs deep-dive + compliance + trade approval. |
| `/morning-scan` | Manual fallback only | Interactive version of the whole morning routine if Task Scheduler didn't fire (weekend, holiday, machine off). |
| `/eod-journal` | 4:15 PM ET (manual) | Sweeps portfolio, gets market context from researcher, updates journal with the day's record + watchlist + reflection. |

## Setup

One-time Telegram pipeline setup: see [`telegram-setup.md`](telegram-setup.md).

Currently configured:
- Plugin: `telegram@claude-plugins-official` v0.0.6 (installed at user scope)
- Telegram MCP server: present, but only loads with `--channels plugin:telegram@claude-plugins-official` flag
- Task Scheduler entry: register via `scripts/install-morning-task.ps1`

## Market holidays

No automatic holiday detection yet. On US market holidays, manually disable the task:

```powershell
Disable-ScheduledTask -TaskName ClaudeTradingMorningScan
# Re-enable next trading day:
Enable-ScheduledTask  -TaskName ClaudeTradingMorningScan
```

## Failure modes & fallbacks

| Failure | Fallback |
|---------|----------|
| Task Scheduler didn't fire (machine asleep) | `WakeToRun` is set in the task definition; if still missed, run `/morning-scan` interactively |
| Telegram bridge offline (session not started with `--channels`) | Slash command writes candidates to local file; you read at your desk instead of on phone |
| `risk-and-compliance` finds 0 framework-compliant candidates | Sends "no-trade day" message; routine continues, journal gets a no-trade entry |
| User declines all candidates | Valid outcome; journal records "evaluated, no trade" with reasons |
