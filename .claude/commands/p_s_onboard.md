---
description: /p_s_onboard — onboard pre-framework positions into ledgered positions. Same input modes as /p_s (image attachment, inline --positions, or — for re-onboarding — journal/positions.json itself). Writes ledgers/positions/<TICKER>.yml + appends to journal/positions.json. After completion, the EOD sell-decision pipeline + check-positions.ps1 + news-research Scout pass all start picking these positions up. Direct-write — use deliberately.
---

# /p_s_onboard — Onboard pre-framework positions

You are the orchestrator for the portfolio-manager subagent's **onboard mode**. This is the write-side counterpart to `/p_s` (which is read-only snapshot).

**Write impact:** every successful onboard creates a real position ledger at `ledgers/positions/<TICKER>.yml` and appends an entry to `journal/positions.json`. Once onboarded, the position is picked up by:
- EOD sell-decision pipeline (`/eod-journal` runs `sell_decision` daily)
- `scripts/check-positions.ps1` daily stop/trail/alert checks
- `news-research` Scout pass (the position becomes part of the per-hour watch universe)

Use deliberately. There is no automatic rollback.

## Step 1 — Acquire positions

Same source priority as `/p_s`:

1. **Image attachment** — broker screenshot from Telegram (`<channel source="telegram" ...>` tag with `image_path` or `attachment_file_id`) or IDE-pasted. Read the image; parse the table (ticker, shares, cost basis, current price, market value, P&L). Same sanity check as `/p_s` (shares × current ≈ market value).
2. **Inline positions paste** in `$ARGUMENTS` — look for `--positions ... --end-positions` block.
3. **`journal/positions.json`** fall-through — only valid if positions.json has v1 entries that pre-date the ledger schema (no `ledger_path`). Re-onboarding is fine.

Capture `--total-portfolio-usd <N>` from `$ARGUMENTS` or the top of the screenshot.

## Step 2 — Pre-flight check

For each ticker the user wants to onboard:

1. Read `journal/positions.json` — if the ticker is already in `positions[]`, FLAG and skip it. Surface the existing entry in the confirmation report.
2. Glob `ledgers/positions/<TICKER>.yml` — if it exists, FLAG and skip. Refuse to overwrite.
3. Verify `tools.market_calendar` says today is not stage-4 (use `tools.regime_check SPY` if needed) — onboarding during a market panic is a bad idea. If `broad_market_stage_class == "stage_4"`, surface a warning but proceed if the caller insists.

## Step 3 — Invoke portfolio-manager subagent in onboard mode

Pass the full onboard brief to the subagent:

```
onboard

--total-portfolio-usd <N>   (if known)

--positions
TICKER SHARES COST_BASIS [SECTOR] [ENTRY_DATE]
...
--end-positions
```

The subagent runs Phase 2 tools per ticker, computes the stop per the `max(8%-from-cost, current_price - ATR)` rule, validates each ledger against the schema, writes the ledger, and updates positions.json. Wait for the subagent's confirmation report.

## Step 4 — Deliver the report

### Telegram channel session

Reply via `mcp__plugin_telegram_telegram__reply` with the confirmation table. Capture `chat_id` from the inbound `<channel>` tag. Plain text (no `parse_mode`) — the table has pipes that would break Markdown rendering.

Long onboards (>4 positions) may exceed Telegram's 4096-byte limit. If so, split: first message = header + table; second message = "Wrote ledgers/... Appended N entries to positions.json. Recommended next: /p_s".

### IDE session (no channel tag)

Print the confirmation report directly. Do not invoke any Telegram tool.

## Guardrails

- **No silent overwrites.** Pre-flight skips existing ledgers and existing positions.json entries. Surface every skip.
- **No grade fabrication.** The subagent uses `setup_classification.type: Manual` and omits grade — this is the honest record. Do NOT post-process the ledger to add a fake grade.
- **No catalyst fabrication.** `catalyst.type: none` is the honest answer for pre-framework positions. Do NOT post-process.
- **Stop re-baseline is loud.** If the subagent's confirmation report shows any position with `Stop basis: atr_rebased` (position was already past 8% threshold), surface that prominently in your delivery. The user needs to see it to make the close-vs-keep decision.
- **Sensitive information** — same as `/p_s`. Never echo broker account numbers or other PII from the screenshot. Position table needs only ticker/shares/prices.
- **No trade recommendations.** Onboarding ledgers a position; it does NOT close or trim anything. Trim proposals belong to `rebalance` mode (deferred).

## Post-onboard reminder

After delivering the confirmation, append a final line:

```
Onboarded N positions. Run /p_s to re-snapshot — onboarded positions now show as `managed` (with stop, trail, setup type) rather than `unmanaged (inline)`. Concentration violations remain unchanged until you actually trim.
```

This sets correct expectations — onboarding makes positions VISIBLE to the framework; it does not RESOLVE concentration violations.
