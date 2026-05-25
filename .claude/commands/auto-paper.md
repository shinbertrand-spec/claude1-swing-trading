---
description: /auto-paper — autonomous paper-trading entry. Reads today's morning-scan candidates, filters to deployable setups (tools/deployable_setups.yml), runs the 5-gate compliance per candidate, sizes via tools.position_sizer against the Tiger paper account, auto-places limit-buy orders via TigerClient, writes to a PARALLEL ledger track (ledgers/paper-auto/<TICKER>.yml + journal/paper-auto/positions.json) that's separate from the human-discretionary track. Paper-only — refuses live. Supports --dry-run. Pair with /auto-paper-monitor (intraday exits) and /auto-paper-reconcile (EOD fills + stops); see /auto-paper-perf for realized vs backtest scoring.
---

# /auto-paper — Autonomous Paper-Trading (Entry Pipeline)

You are running the autonomous paper-trade entry pipeline. **No human approval per trade** — the human approval boundary is at the *deployable-setup list* level (only setups that cleared rolling walk-forward + clean 5-gate compliance get placed).

**Track separation.** Everything written here goes to the parallel paper-auto track (`ledgers/paper-auto/*.yml` + `journal/paper-auto/positions.json`). The human-discretionary track (`journal/positions.json`) is untouched.

**Safety properties (invariants):**
- Tiger paper account only — `TigerClient()` refuses live by default; this command NEVER passes `allow_live=True`.
- Only setups on `tools/deployable_setups.yml` are placed.
- Paper-auto-track hard rules (5% / 20% / 8 / 15%) checked separately from human track in `tools.auto_paper.pipeline._check_track_limits`.
- `--dry-run` prints what would be placed without calling Tiger.

## $ARGUMENTS parsing

- `--dry-run` — print planned placements only; no broker calls, no file writes.
- `--candidates-file <path>` — override default `journal/candidates/YYYY-MM-DD.md` (today's scan output).
- `--limit <N>` — cap the number of placements this run (default: no cap; respects 8-position limit naturally).
- `--tickers <T1,T2>` — restrict to specific tickers from today's candidate list.

## Step 1 — Read today's candidates

Read `journal/candidates/YYYY-MM-DD.md` (today's ET date) or `$ARGUMENTS --candidates-file`. Parse the candidate list: ticker, setup type, suggested entry, suggested stop, suggested target, grade.

If no candidates file exists (Task Scheduler didn't fire, weekend, holiday), exit with:

```
AUTO_PAPER_NO_CANDIDATES — no candidates file at <path>. /morning-scan-telegram may not have fired today.
```

## Step 2 — Pre-filter by deployable setup

For each candidate, check `tools.auto_paper.config.is_deployable(setup_type)`. Drop candidates whose setup_type is not on the deployable list — surface in the summary as `skipped (non-deployable)`.

## Step 3 — Per-surviving-candidate: deep-dive + 5-gate

For each candidate that passes Step 2:

1. **Deep-dive via `trade-researcher`** — same as `/morning-deep-dive` Step 3. Writes the candidate ledger to `ledgers/candidates/YYYY-MM-DD/<TICKER>.yml`. Capture the ledger path.

2. **Compliance via `risk-and-compliance` Mode 2** — same as `/morning-deep-dive` Step 4. Pass the candidate ledger path + a proposed trade dict (entry = pivot from ledger, stop = stop from ledger, target = 2× R from entry, shares = TBD).

   **For sizing, the trade-researcher's proposed shares may not match what the paper account can support.** Provide a placeholder size of 1 for the hard-rule check; we re-size from `position_sizer` + account state below. The other checks (freshness, trace audit, stale phrases, adversarial review) are independent of size.

   **Phase 7 H1 SHADOW MODE (effective 2026-05-26 until further notice).** The risk-and-compliance agent now runs six gates (Gate 6 = `debate_synthesis` composing the H3 `SwingVerdict` enum). Per `swing-2026-05-25-paper-trade-handoff` §Step 4 D1 default = Option A (shadow): the paper-auto track is the first live use of Gate 6, with zero prior validation data. To avoid first-day-live untested-gate blocks, instruct risk-and-compliance as follows when invoking:

   > "Run all six gates per your standard sequence. Write the Gate 6 debate output to `ledgers/debate/<TICKER>-<DATE>.yml` as designed. **For the FINAL verdict line returned to me, the auto-paper track is operating in H1 shadow mode: any SwingVerdict in {ENTRY_STRONG, ENTRY_NORMAL, WATCH_BUILD_THESIS, DEFER} maps to APPROVE for placement purposes; only REJECT (any mechanical gate 1-4 BLOCK, OR Gate 6 `already_fired` risk-trigger override) blocks. Surface the actual SwingVerdict in the report; the placement decision is the shadow-mode-mapped value.** Shadow mode lifts after 2-4 weeks once `ledgers/debate/` contains enough comparison data per H1 spec §A.4, or sooner if the retrospective A/B simulation clears the lift threshold."

   If verdict is `BLOCK` (or shadow-mapped to REJECT): skip this candidate; surface in summary as `blocked: <reason>` (include the original SwingVerdict). On placement, surface a one-line "shadow gate 6: <SwingVerdict>" in the summary so Bertrand can compare verdicts side-by-side in `ledgers/debate/`.

3. **Re-size via `tools.position_sizer`** using the paper account's current `available_funds`:

   ```python
   from tools.broker.tiger import TigerClient
   c = TigerClient()  # paper-routed
   summary = c.account_summary().output
   account = summary["available_funds"]
   ```

   Then:

   ```
   uv run python -m tools.position_sizer \
     --account <account> --entry <pivot> --atr <ledger.technical.atr_14> \
     --setup-grade <ledger.setup_classification.grade> \
     --regime <ledger.regime.broad_market_stage_class> \
     --cash-available <account>
   ```

   Use `output.shares` and `output.capital`. Don't compute by hand.

   Note: `available_funds` may come back as `Infinity` on a fresh paper account (SDK quirk noted in `project_broker_bridge` memory). When `Infinity`, fall back to `summary["cash"]` for sizing math.

4. **Build the `CandidateInput`:**

   ```python
   from tools.auto_paper.pipeline import CandidateInput
   cand = CandidateInput(
       ticker=<ticker>,
       setup_type=<ledger.setup_classification.type>,
       setup_grade=<ledger.setup_classification.grade>,
       pivot_price=<ledger.setup_classification.pivot_price>,
       limit_price=<pivot * 1.001>,  # ask + 0.1% per CLAUDE.md
       stop_price=<ledger.setup_classification.stop_price>,
       target_price=<entry + 2 * (entry - stop)>,
       shares=<position_sizer output.shares>,
       sector_etf=<ledger.regime.sector_etf>,
       reasoning_trace=<ledger.reasoning_trace>,
   )
   ```

5. **Place via `tools.auto_paper.pipeline.place_candidate`:**

   ```python
   from tools.auto_paper.pipeline import place_candidate
   result = place_candidate(cand, client=c, dry_run=<args.dry_run>)
   ```

   The pipeline enforces:
   - Deployable-setup filter (already applied; defensive double-check)
   - Refuses if a paper-auto ledger already exists for this ticker
   - Track-level hard rules (5% / 20% / 8 / 15%) against paper-auto track only
   - Calls `TigerClient.place_limit_buy` (or skips if `dry_run=True`)
   - Writes `ledgers/paper-auto/<TICKER>.yml` with `state: submitted`
   - Appends to `journal/paper-auto/positions.json`

   Capture `result.status`, `result.broker_order_id`, `result.ledger_path`, `result.reason`.

## Step 4 — Summary report

Output (and reply via Telegram if invoked from a Telegram session):

```
**Mode:** auto-paper (entry)  |  Dry-run: <yes|no>
**Asof:** YYYY-MM-DD HH:MM ET
**Account:** ...<last-4>  |  Paper: ✅  |  Net liq: $X  |  Cash: $Y

### Outcomes

| Ticker | Status | Detail |
| NVDA   | placed | order #10001, 12 sh @ $850.50, stop $810.00, ledger paper-auto/NVDA.yml |
| AAPL   | rejected | setup_type 'Pullback-20SMA' not on deployable list |
| MSFT   | blocked | 5-gate: trace_audit BLOCK on uncited claim |
| GOOGL  | dry_run | would place limit-buy 8 GOOGL @ $401.50 (~$3,212) |
| TSLA   | error  | place_limit_buy: INSUFFICIENT_FUNDS |
| ...

### Track state after this run
- Paper-auto positions: N / 8
- Cash buffer: Z%
- New orders placed this run: K
- Cost basis of new placements: $W
```

## Step 5 — Companion commands in the auto-paper loop

This command handles entry only. The rest of the lifecycle is owned by sibling commands:

- **`/auto-paper-reconcile`** (default 4:30 PM ET) — pulls `TigerClient.get_filled_orders()`, updates each ledger's `fill_price` to the broker's `avg_fill_price`, transitions `submitted` → `starter` (or → `closed` for DAY-expired), and **places a broker-side STP SELL at the ledger's `stop_price`** sized to filled qty.
- **`/auto-paper-monitor`** (default every 30 min, 10 AM – 3:30 PM ET) — per-bar sell-decision composer over `starter` positions; on a non-hold action places a limit-sell and cancels the resting stop.
- **`/auto-paper-perf`** (on demand) — realized vs backtest expectation across the closed paper-auto track.

Install the full cron loop with `.\scripts\install-auto-paper-tasks.ps1`. For monitoring in-flight today, `/p_s_sync --include-orders` shows open Tiger orders that haven't reconciled yet.

**v1 simplifications:** partial sells from the composer close the whole position; no live trailing stop ratchet; PE-expansion warning hardcoded False (no fundamentals source); STP SELL only (no OCA bracket).

## Guardrails

- **Paper-only.** Never pass `allow_live=True` to `TigerClient`. The pipeline refuses to construct a live client; this is the framework's safety boundary.
- **Track separation.** Never write to `journal/positions.json` or `ledgers/positions/<TICKER>.yml`. Those are the human-discretionary track. Paper-auto goes to `journal/paper-auto/positions.json` and `ledgers/paper-auto/<TICKER>.yml`.
- **Deployable filter is hard.** Don't override `is_deployable` even if the setup looks great. If a new setup should be placeable, edit `tools/deployable_setups.yml` deliberately (and update `project_swing_phases` memory).
- **No-trade is a valid outcome.** All candidates skipped / blocked is fine. Still emit the summary.
- **Sensitive information** — account number is masked by `TigerClient`. Never echo unmasked PII into Telegram or anywhere else.
