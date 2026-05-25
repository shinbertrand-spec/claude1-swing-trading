---
description: End-of-day swing-trading journal write-up (4:15 PM ET). For each open position, runs the Phase 2 sell-evaluation pipeline (climax_top_detect, violations_detect, base_stage_detect, sell_into_strength, sell_decision), appends the result to the position ledger's sell_eval_history, and surfaces any non-hold actions for the next morning.
---

# End-of-Day Trading Routine

It is end-of-day ET on a US market day. Run the EOD routine.

This command now exercises the Phase 2 sell-discipline tools (v1-preliminary per swing-sell-discipline). Per-position daily evaluations append to each position ledger's `sell_eval_history` array. Any `sell_decision.action != "hold"` is surfaced to the user; the actual exit decision happens in the next morning's `/morning-deep-dive` after re-verification.

## Step 1 — Portfolio sweep

Read in parallel:

- Today's journal entry (`journal/YYYY-MM-DD.md` or `journal/Trading.md`)
- `journal/positions.json` (open positions index)
- Each position's ledger from `ledgers/positions/<TICKER>.yml`

Identify:

- Trades placed today (filled / unfilled / cancelled)
- Trades closed today (exits, P&L, reason)
- Open positions: current price vs entry, distance to stop, distance to target, days held
- Any stop-loss triggers or target hits during the day

## Step 2 — Market context

Invoke `trade-researcher` in EOD mode:

> EOD report for today's date. Provide: SPY/QQQ close + % day, VIX close + regime, sector leaders / laggards (top 2 each), notable moves in portfolio names and watchlist names, any macro events to note (Fed-speak, data releases, earnings reports of significance). One short paragraph each.

Also run `tools.regime_check SPY` and capture the regime classification — this is needed for the sell-decision composer downstream.

## Step 3 — Per-position sell evaluation

**For EACH open position from `journal/positions.json` (where `stage != "closed"`):**

1. Run the four detector tools in parallel:

   ```
   uv run python -m tools.climax_top_detect <ticker>
   uv run python -m tools.violations_detect <ticker> --entry-date <ledger.position_state.starter.fill_date>
   uv run python -m tools.base_stage_detect <ticker>
   ```

   And compute the sell-into-strength check from the current quote vs entry:

   ```
   uv run python -m tools.sell_into_strength \
     --gain-pct <(current - entry) / entry> \
     --days <trading days since starter.fill_date> \
     --grade <ledger.setup_classification.grade>
   ```

2. Compose via `tools.sell_decision` (library import, no CLI — see `tools/sell_decision.py`):

   ```
   uv run python -c "
   import json
   from tools.sell_decision import compute
   r = compute(
     climax_patterns_firing=<count from climax_top_detect>,
     violations_firing=<count from violations_detect>,
     violation_5_alone_full_exit=<flag from violations_detect>,
     base_stage=<from base_stage_detect>,
     new_high_today=<from base_stage_detect>,
     sell_into_strength_triggered=<from sell_into_strength>,
     sell_into_strength_fraction=<from sell_into_strength>,
     setup_grade='<grade>',
     pe_expansion_warning=False,
     regime_class='<from regime_check>',
   )
   print(r.to_json())
   "
   ```

3. **Append the result to the position ledger's `sell_eval_history`:**

   - Read `ledgers/positions/<TICKER>.yml`
   - Append a new entry to `sell_eval_history`:
     ```yaml
     - date: <YYYY-MM-DD>
       evaluated_at: <ISO timestamp>
       climax_top_patterns_firing: <count>
       climax_top_patterns_detail: [<list>]
       violations_firing: <count>
       violations_detail: [<list>]
       base_stage: <1-5>
       new_high_today: <bool>
       sell_into_strength_triggered: <bool>
       action: <from sell_decision.output.action>
       confidence: <from sell_decision.output.confidence>
       new_stop: <from sell_decision.output.new_stop or null>
       trace_refs: [<trace step ids>]
       v1_preliminary_flag: true
     ```
   - Append the corresponding tool outputs as new `reasoning_trace` entries (with fresh sequential ids) so `trace_refs` resolves.
   - Update `meta.updated_at` / `meta.updated_by = "eod-journal"`.
   - Write the result back to `ledgers/positions/<TICKER>.yml`.

## Step 3a — Social confluence cross-link (Phase 1.5)

After Step 3's per-position evaluation completes, but before surfacing alerts:

1. Find the latest hourly news snapshot for today: `ledgers/news/YYYY-MM-DD/HH.yml` where `HH` is the most recent hour written (typically `16` at 4:15 PM ET — but tolerate gaps; pick the highest `HH` available).
2. If the file is missing OR its `meta.schema_version` is `"1.0"` (pre-1.5), skip this step silently — note in journal that social confluence was not available.
3. Otherwise, read its `social_signals[]` array. For each open position, look up the matching `social_signals[<i>].ticker == position.ticker` entry (may be absent — most positions won't have one in any given hour).
4. Compute per-position confluence flags:
   - `social_confluence_climax` = `climax_top_detect.patterns_firing > 0 AND social_signals[<i>].classification == "climax_warning"`
   - `social_confluence_bearish` = `social_signals[<i>].classification == "bearish_pile_on"` (stands on its own — no detector pairing required; bearish social on an open long is a thesis-break warning regardless of price action)
   - `social_signal_only` = `social_signals[<i>]` exists AND classification ∈ `[climax_warning, bearish_pile_on, cooling]` but no detector fired

These flags are **informational amplifiers** for Step 4 surfacing. They do NOT modify `sell_decision.output.action` — the sell tool's decision stands as computed. They DO escalate user attention.

5. Append the confluence flags to each position's `sell_eval_history` entry (the one just written in Step 3.3) as additional fields:

   ```yaml
   social_confluence_climax: <bool>
   social_confluence_bearish: <bool>
   social_signal_only: <bool>
   social_snapshot_ref: ledgers/news/YYYY-MM-DD/HH.yml  # which snapshot we read
   ```

   These are additive metadata. The existing `action` / `confidence` / `new_stop` fields are untouched.

## Step 4 — Surface non-hold actions

For each position whose `sell_decision.output.action != "hold"` OR whose Step 3a produced `social_confluence_climax: true` / `social_confluence_bearish: true` (even if `action == "hold"`), present a clear summary:

> **<TICKER> — sell-eval recommends `<action>` (`<confidence>` confidence)**
> Climax-top patterns: <count> [<names>]
> Violations: <count> [<names>]
> Base stage: <N>, new high today: <bool>
> Sell-into-strength triggered: <bool> (fraction <X>)
> Contributing triggers: <list>
> Recommended new stop: $<X.XX>
> **Social confluence:** <one of: "climax (OHLCV + StockTwits)" | "bearish pile-on (StockTwits)" | "social-only — detectors quiet" | "none">
> Ledger: `ledgers/positions/<TICKER>.yml`
>
> **Tomorrow morning, run `/morning-deep-dive <TICKER>` for full re-verification before acting.** This EOD evaluation is informational; no exit happens tonight.

When `social_confluence_climax: true`, lead the summary with **"⚠ CLIMAX CONFLUENCE"** to signal high-conviction sell candidate. When `social_confluence_bearish: true` and detectors are quiet, lead with **"⚠ SOCIAL-ONLY BEARISH"** — the OHLCV doesn't say sell yet but retail sentiment is collapsing on this long.

Mark these in today's journal under a new **Sell-eval alerts** section.

## Step 5 — Watchlist refresh

For each name on the watchlist, briefly note (one line each) whether today's price action moved it CLOSER or FURTHER from its re-evaluation trigger. Update or remove triggers as needed.

## Step 6 — Journal write-up

Update today's journal with:

- **Market context** (from Step 2)
- **Trades placed today** (filled / unfilled / cancelled — with thesis recap + ledger path)
- **Trades closed today** with P&L (absolute + %), reason for exit, one-line lesson
- **Open positions status** (table: ticker, entry, current, % P/L, days held, distance-to-stop, distance-to-target, sell_eval action)
- **Sell-eval alerts** (from Step 4, if any)
- **Watchlist for tomorrow** with trigger conditions
- **End-of-day reflection** — three lines: one thing done well, one done poorly, one adjustment to test tomorrow

## Step 7 — Weekly review check

If today is Friday (US market day), additionally generate the weekly review block:

- Win rate this week (# winners / # closed trades)
- Average winner % vs average loser %
- Realized R:R vs the 1:2 target
- Sector exposure at week's end
- Per-setup hit rate (group closed trades by `setup_type` from positions.json; report wins/losses per setup type)
- One pattern observed (good or bad) to carry into next week

Append to `journal/weekly/YYYY-WW.md` (create the directory if it doesn't exist).

## Guardrails

- **Always write the journal even on no-trade days** — the discipline rule is non-negotiable.
- **If today was a no-trade day**, the entry can be brief but must still include market context, watchlist status, and per-position sell-eval results for any open positions.
- **EOD sell-eval is informational only**; no exits happen tonight. Exits go through full `/morning-deep-dive` re-verification.
- **Sell-discipline tools are v1-preliminary** — flagged as such in each ledger entry. Treat their recommendations as suggestive until the Minervini book v2 upgrade.
- **Social confluence (Step 3a) is informational**; it does NOT modify `sell_decision.output.action`. The detector pipeline stays the system of record for the action; social only escalates user attention. If the news snapshot is missing or pre-1.5, skip silently and note in the journal.
- **If a tool errors during per-position eval**, write the error to the position's ledger as a `reasoning_trace` step tagged `manual:tool_failure` and note in the journal — do not silently skip.
- **Sensitive information** — see `CLAUDE.md` § Sensitive Information. Never echo positions to public channels without explicit user direction.
