---
description: HEADLESS morning candidate scan. Writes the full report + a Telegram-ready summary to journal/candidates/. A separate PowerShell wrapper handles actually POSTing to Telegram (we cannot use the plugin's MCP reply tool from --print mode — it isn't loaded).
---

# Morning Candidate Scan — Headless (writes files only)

You are running in headless `--print` mode, triggered by Windows Task Scheduler. **No user is present and no Telegram MCP tool is available** — your output is files on disk, not Telegram. The wrapper PowerShell script reads those files and POSTs to Telegram via the bot API directly.

This is the candidate-scan-only headless variant. The full deep-dive + verification flow lives in `morning-deep-dive` and runs later at the user's IDE / Telegram session.

## Pre-flight checks

1. **Is today a US market day?** If today is Saturday or Sunday, write the date + `weekend — skipped` to `C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD-summary.txt` and exit. (Holiday detection not yet implemented — assume weekdays are trading days.)
2. Confirm `C:\Users\User\Desktop\Claude1\journal\candidates\` exists; create if missing.
3. Confirm `uv run python --version` works. If Python or uv is unavailable, write a failure summary (see Guardrails) and exit — the candidate scan now depends on `tools.regime_check` for the Stage-4 circuit-breaker.

## Step 1 — Read framework + portfolio state

Read in parallel:

- `C:\Users\User\Desktop\Claude1\CLAUDE.md`
- `C:\Users\User\Desktop\Claude1\ledgers\README.md`
- Most recent file in `C:\Users\User\Desktop\Claude1\journal\` (current portfolio state)
- `C:\Users\User\Desktop\Claude1\journal\positions.json`

## Step 2 — Run candidate scan

Invoke the `risk-and-compliance` subagent in candidate-scan mode (Mode 1):

> Morning candidate scan for today's date. Mode 1 protocol per your prompt — invoke `tools.regime_check SPY` first; circuit-break and STOP if broad market is Stage 4. Otherwise propose up to 3 swing-trade candidates that pass ALL framework hard rules in `CLAUDE.md`. For each candidate, run `tools.trend_template <ticker>` and `tools.earnings_calendar <ticker>` to populate the per-candidate pass/fail line. Return the standard candidate-scan output schema. If you find fewer than 3 clean candidates, return what you have and say so — do not pad.

## Step 3 — Write the full report

Write the FULL candidate-scan output to:

`C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD.md`

Use today's actual date. If the subagent returned the Stage-4 circuit-breaker message, write that message verbatim as the report content (the wrapper will use the summary file for the push).

## Step 4 — Write the Telegram summary

Write a CONCISE Telegram-formatted summary to:

`C:\Users\User\Desktop\Claude1\journal\candidates\YYYY-MM-DD-summary.txt`

### Normal candidate-day format (Telegram Markdown — `*bold*`, no fancy formatting):

```
🔔 *Morning Candidates — YYYY-MM-DD*

1. *TICKER* · sub-theme · $price · setup <grade>
Why: <one-line thesis>
Risk: <one-line risk>
Next earnings: <date or "outside 10d window">

2. *TICKER* · sub-theme · $price · setup <grade>
Why: <one-line thesis>
Risk: <one-line risk>
Next earnings: <date or "outside 10d window">

3. *TICKER* · sub-theme · $price · setup <grade>
Why: <one-line thesis>
Risk: <one-line risk>
Next earnings: <date or "outside 10d window">

Pick 2 of 3. At your desk run /morning-deep-dive
```

Grade is the per-setup grade from the subagent's output (e.g. `A+`, `Swan`, `GoldenEP`). Use whatever `setup_classification.grade` value the subagent attached to that candidate.

### Stage-4 circuit-breaker format:

If the subagent returned the Stage-4 message:

```
🚫 *Stage 4 broad market — YYYY-MM-DD*

SPY trend_template_passes: X/7 — no new entries today.

Per swing-regime-playbook circuit breaker: even perfect setups drop to 30-40%
hit rate in Stage 4. Manage existing positions; no scan today.
```

### Fewer-than-3-passed format:

Include only what passed and add a final line `Only N candidates cleared the framework today.`

### Zero-passed format:

```
🔔 *No candidates passed framework rules — YYYY-MM-DD*

No-trade day. Watchlist remains tradeable if triggers fire.
```

## Step 5 — Output a one-line status to stdout

Print exactly one of:

- `MORNING_SCAN_OK YYYY-MM-DD` (candidates delivered)
- `MORNING_SCAN_STAGE_4 YYYY-MM-DD` (circuit-breaker fired)
- `MORNING_SCAN_NONE YYYY-MM-DD` (zero candidates passed)
- `MORNING_SCAN_WEEKEND_SKIP YYYY-MM-DD` (weekend)
- `MORNING_SCAN_FAIL YYYY-MM-DD — <reason>` (any failure)

The wrapper reads this to know the scan completed and which summary to send.

## Guardrails

- **Never ask the user anything.** This is headless.
- **Do not attempt to use any Telegram MCP tool** (`reply`, `react`, `edit_message`) — not available in --print mode. Delivery is handled by the wrapper that runs AFTER you exit.
- **Always write both files** (the full report AND the summary) even if one looks redundant — the wrapper expects both.
- **If `risk-and-compliance` errors**, write the error to the summary file as a single line `⚠️ Morning scan failed: <reason>` and write the same reason to the full report file. Output `MORNING_SCAN_FAIL` to stdout. This way the wrapper still has something to push and Bertrand isn't silently left in the dark.
- **If a Phase 2 tool errors during the subagent's scan** (e.g. `tools.regime_check SPY` fails to fetch SPY data), treat it the same as the previous bullet — failures BLOCK; don't fall back to LLM-only reasoning.
- **Never auto-execute trades.** Telegram delivery is informational only.
- **Sensitive information** — never include `.env` contents, bot tokens, or PII in either file. The summary is candidates only. See `CLAUDE.md` § Sensitive Information.
