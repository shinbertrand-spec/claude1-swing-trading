---
description: HEADLESS morning candidate scan. Writes the full report + a Telegram-ready summary to journal/candidates/. A separate PowerShell wrapper handles actually POSTing to Telegram (we cannot use the plugin's MCP reply tool from --print mode — it isn't loaded).
---

# Morning Candidate Scan — Headless (writes files only)

You are running in headless `--print` mode, triggered by Windows Task Scheduler. **No user is present and no Telegram MCP tool is available** — your output is files on disk, not Telegram. The wrapper PowerShell script reads those files and POSTs to Telegram via the bot API directly.

## Pre-flight checks

1. **Is today a US market day?** If today is Saturday or Sunday, write the date + "weekend — skipped" to `C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD-summary.txt` and exit. (Holiday detection not yet implemented — assume weekdays are trading days.)
2. Confirm `C:\Users\User\Desktop\Claude1\journal\candidates\` exists. It should; if it doesn't, create it.

## Step 1 — Read framework + portfolio state

Read in parallel:
- `C:\Users\User\Desktop\Claude1\CLAUDE.md`
- Most recent file in `C:\Users\User\Desktop\Claude1\journal\` (current portfolio state)

## Step 2 — Run candidate scan

Invoke the `risk-and-compliance` subagent in candidate-scan mode:

> Morning candidate scan for today's date. Suggest 3 swing-trade candidates that pass ALL framework hard rules in `CLAUDE.md`. Return the standard candidate-scan output schema. If you can find fewer than 3 clean candidates, return what you have and say so — do not pad.

## Step 3 — Write the full report

Write the FULL candidate-scan output to:

`C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD.md`

(Use today's actual date.)

## Step 4 — Write the Telegram summary

Write a CONCISE Telegram-formatted summary to:

`C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD-summary.txt`

Exact format (Telegram Markdown — single-line `*bold*`, no fancy formatting):

```
🔔 *Morning Candidates — YYYY-MM-DD*

1. *TICKER* · sub-theme · $price · setup X/5
Why: <one-line thesis>
Risk: <one-line risk>

2. *TICKER* · sub-theme · $price · setup X/5
Why: <one-line thesis>
Risk: <one-line risk>

3. *TICKER* · sub-theme · $price · setup X/5
Why: <one-line thesis>
Risk: <one-line risk>

Pick 2 of 3. At your desk run /morning-deep-dive
```

If fewer than 3 candidates passed: include only what passed and add a final line "Only N candidates cleared the framework today."

If ZERO candidates passed:

```
🔔 *No candidates passed framework rules — YYYY-MM-DD*

No-trade day. Watchlist remains tradeable if triggers fire.
```

## Step 5 — Output a one-line status to stdout

Print exactly: `MORNING_SCAN_OK YYYY-MM-DD` (or `MORNING_SCAN_WEEKEND_SKIP YYYY-MM-DD` if weekend). The wrapper script reads this to know the scan completed and the summary file is ready to send.

## Guardrails

- **Never ask the user anything.** This is headless.
- **Do not attempt to use any Telegram MCP tool** (`reply`, `react`, `edit_message`) — they are not available in --print mode. Delivery is handled by the wrapper script that runs AFTER you exit.
- **Always write both files** (the full report AND the summary) even if one looks redundant — the wrapper expects both.
- **If `risk-and-compliance` errors**, write the error to the summary file as a single line `⚠️ Morning scan failed: <reason>` so the wrapper still has something to send.
- **Never auto-execute trades.** Telegram delivery is informational only.
