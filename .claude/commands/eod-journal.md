---
description: End-of-day swing-trading journal write-up (4:15 PM ET)
---

# End-of-Day Trading Routine

It is end-of-day ET on a US market day. Run the EOD routine.

## Step 1 — Portfolio sweep

Read today's journal entry. Identify:
- Trades placed today (filled vs unfilled vs cancelled)
- Trades closed today (exits, P&L, reason)
- Open positions: current price vs entry, distance to stop, distance to target, days held
- Any stop-loss triggers or target hits during the day

## Step 2 — Market context

Invoke `trade-researcher` in **EOD mode** with the prompt:

> EOD report for today's date. Provide: SPY/QQQ close + % day, VIX close + regime, sector leaders / laggards (top 2 each), notable moves in portfolio names and watchlist names, any macro events to note (Fed-speak, data releases, earnings reports of significance). One short paragraph each.

## Step 3 — Watchlist refresh

For each name on the watchlist, briefly note (one line each) whether today's price action moved it CLOSER or FURTHER from its re-evaluation trigger. Update or remove trigger conditions as needed.

## Step 4 — Journal write-up

Update today's journal with:

- **Market context** (from Step 2)
- **Trades placed today** (filled / unfilled / cancelled — with thesis recap)
- **Trades closed today** with P&L (absolute + %), reason for exit, one-line lesson
- **Open positions status** (table: ticker, entry, current, % P/L, days held, distance-to-stop, distance-to-target)
- **Watchlist for tomorrow** with trigger conditions
- **End-of-day reflection** — three lines: one thing done well, one done poorly, one adjustment to test tomorrow

## Step 5 — Weekly review check

If today is Friday (US market day), additionally generate the weekly review block:
- Win rate this week (# winners / # closed trades)
- Average winner % vs average loser %
- Realized R:R vs the 1:2 target
- Sector exposure at week's end
- One pattern observed (good or bad) to carry into next week

Append to `journal/weekly/YYYY-WW.md` (create the directory if it doesn't exist).

## Guardrails

- Always write the journal even on no-trade days — the discipline rule is non-negotiable.
- If today was a no-trade day, the entry can be brief but must still include market context and watchlist status.
- Discord auto-post is currently deferred — journal stays local.
