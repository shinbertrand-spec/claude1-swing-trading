---
description: /auto-paper — autonomous paper-trading entry. Reads today's morning-scan candidates, filters to deployable setups (tools/deployable_setups.yml), runs the 5-gate compliance per candidate, sizes via tools.position_sizer against the Tiger paper account, auto-places limit-buy orders via TigerClient, writes to a PARALLEL ledger track (ledgers/paper-auto/<TICKER>.yml + journal/paper-auto/positions.json) that's separate from the human-discretionary track. Paper-only — refuses live. Supports --dry-run. Session 1 scope - entry only; EOD reconciliation and broker-side stops are subsequent sessions.
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

2. **5-gate via `risk-and-compliance` Mode 2** — same as `/morning-deep-dive` Step 4. Pass the candidate ledger path + a proposed trade dict (entry = pivot from ledger, stop = stop from ledger, target = 2× R from entry, shares = TBD).

   **For sizing, the trade-researcher's proposed shares may not match what the paper account can support.** Provide a placeholder size of 1 for the 5-gate's hard-rule check; we re-size from `position_sizer` + account state below. The 5-gate's other checks (freshness, trace audit, stale phrases, adversarial review) are independent of size.

   If verdict is `BLOCK`: skip this candidate; surface in summary as `blocked: <reason>`.

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

## Step 5 — What's NOT done by this command (Session 1)

- **EOD reconciliation** — actual fill prices for placed orders are NOT yet written back. Session 2 ships a `/auto-paper-reconcile` command that pulls `TigerClient.get_filled_orders()` and updates each ledger's `fill_price` to the broker's `avg_fill_price`.
- **Broker-side stops** — the stop is recorded in the ledger but NO stop-loss order is placed at Tiger. Session 3 ships the OCA stop+target group.
- **Per-bar sell-decision auto-exit** — Session 3.
- **Performance dashboard** — Session 4.

For Session 1, after running this command, **manually monitor fills via `/p_s_sync --include-orders`** (or pull `TigerClient.open_orders()` directly).

## Guardrails

- **Paper-only.** Never pass `allow_live=True` to `TigerClient`. The pipeline refuses to construct a live client; this is the framework's safety boundary.
- **Track separation.** Never write to `journal/positions.json` or `ledgers/positions/<TICKER>.yml`. Those are the human-discretionary track. Paper-auto goes to `journal/paper-auto/positions.json` and `ledgers/paper-auto/<TICKER>.yml`.
- **Deployable filter is hard.** Don't override `is_deployable` even if the setup looks great. If a new setup should be placeable, edit `tools/deployable_setups.yml` deliberately (and update `project_swing_phases` memory).
- **No-trade is a valid outcome.** All candidates skipped / blocked is fine. Still emit the summary.
- **Sensitive information** — account number is masked by `TigerClient`. Never echo unmasked PII into Telegram or anywhere else.
