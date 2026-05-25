---
description: /auto-paper-monitor — intraday per-bar sell-decision composer for the paper-auto track. For every position in `starter` state, fetches recent OHLCV, runs the four OHLCV-derivable sell-discipline detectors (climax_top, violations, base_stage, sell_into_strength), composes via tools.sell_decision. If the composer returns a non-hold action (sell_50/sell_75/sell_100), auto-places a limit-sell via TigerClient (bid - 0.1%), cancels the resting broker stop, and transitions the ledger to closed. Read-and-write; only touches the paper-auto track. Cron-eligible every 30 min during US session. Supports --dry-run.
---

# /auto-paper-monitor — Intraday Sell-Decision Auto-Exit

Run this on a cron during the US session (default: every 30 min 10:00 AM – 3:30 PM ET). It's the third leg of the paper-auto loop:

```
9:35 ET   /auto-paper             → places limit-buys
10:00-15:30 ET, every 30 min  /auto-paper-monitor  → exits via sell-composer
16:30 ET  /auto-paper-reconcile   → reconciles fills, places stops
```

**Track-bound.** Only touches `ledgers/paper-auto/<TICKER>.yml` + `journal/paper-auto/positions.json`. The human-discretionary track is never read or modified.

## $ARGUMENTS parsing

- `--dry-run` — runs the detector composition and writes `sell_eval_history` entries but does NOT place sells, does NOT cancel stops, does NOT close ledgers. Useful before enabling cron to inspect what the composer would do.

## Step 1 — Run the exit evaluator

```python
from tools.auto_paper.exits import evaluate_exits
results = evaluate_exits(dry_run=<args.dry_run>)
```

The module:

1. Loads paper-auto positions in `starter` state from `journal/paper-auto/positions.json`.
2. For each: fetches recent OHLCV via `tools.data.fetch_ohlcv`.
3. Runs the four OHLCV-derivable detectors:
   - `tools.climax_top_detect` — 6 climax-top patterns
   - `tools.violations_detect` — 5 post-entry violations
   - `tools.base_stage_detect` — base count + new-high flag
   - `tools.sell_into_strength` — 10-15% in 2-3 days
4. Composes via `tools.sell_decision.compute(...)` (P/E expansion forced to False — no fundamentals in v1; queued via the edgartools dep).
5. If composer returns a SELL action (`sell_50` / `sell_75` / `sell_100`):
   - Places a limit-sell at last close × (1 − 0.001) per CLAUDE.md execution rules
   - Cancels the resting broker-side stop (recorded as `position_state.stop_order_id` by `/auto-paper-reconcile`) so we don't leave a stale stop after exit
   - Transitions the ledger to `closed` with `exit_price` + `exit_reason`
   - Updates `journal/paper-auto/positions.json` entry to `stage: closed`
6. Otherwise (`hold` or other) just appends to `sell_eval_history` and leaves the position open.

If no positions are in `starter` state, returns `[]` and the command exits with `AUTO_PAPER_MONITOR_NOTHING_OPEN`.

## Step 2 — Summary report

Output (and reply via Telegram if invoked from a Telegram session):

```
**Mode:** auto-paper monitor  |  Dry-run: <yes|no>
**Asof:** YYYY-MM-DD HH:MM ET
**Starter positions evaluated:** N

| Ticker | Action  | Detail |
| NVDA   | hold    | composer = hold (no triggers fired) |
| AAPL   | sell_100 | composer = sell_100 (climax_top: gap_up_high_vol). Placed SELL #11042 @ $180.27, cancelled stop #11001, closed @ $180.27 (vs entry $180.50, R=-0.05) |
| MSFT   | error   | broker fetch: HTTP 503 |
| ...

### Track state after this run
- Starter positions: N total
- Closed via sell-composer this run: K
- Still open: M
```

## Step 3 — Escalation

Surface these specifically:

- **closed positions** — they're done; check the realized R-multiple and exit_reason. Surfaced in `/auto-paper-perf` next time it runs.
- **errors** (broker call failures) — manual review. Position stays in `starter`; check Tiger Trader app for actual state.
- **cancel rejections** — if the stop cancellation fails (e.g. stop already filled), the ledger still transitions to `closed` and a warning surfaces. Verify in Tiger Trader app — you may have a duplicate / orphan sell at the broker.

## v1 simplifications (deferred to a later session)

- **Partial sells (sell_50, sell_75) close the WHOLE position.** Pyramid leg management is a future-session enhancement. The ledger note records what the composer actually wanted (e.g. "composer=sell_50; treated as full close v1").
- **No trailing stop ratchet** — the broker stop sits at the original `stop_price`. Tightening as the position moves favorably is Phase 5.b backtest territory; live trailing is post-MVP.
- **PE-expansion warning is False** — needs a fundamentals source (queued via `edgartools` dep in pyproject.toml).
- **Bid offset uses last-bar Close** — the module doesn't have live L1 quotes. The limit lands close to last and Tiger fills against the current book.

## Guardrails

- **Paper-only.** `TigerClient()` defaults to `allow_live=False`; the monitor never overrides.
- **Track-bound.** Never reads `journal/positions.json` or writes to `ledgers/positions/`.
- **Re-validates schema** on every ledger write.
- **Idempotent on dry-run.** A dry-run can be re-fired safely — it only appends a `sell_eval_history` entry; it never closes a position or touches the broker.
- **Real-run is idempotent at the position level.** Once a ledger transitions to `closed`, it's no longer in `starter` state and won't be re-evaluated.
- **No trade recommendations.** This is mechanical exit policy following the composer's verdict. The composer's logic is in `tools.sell_decision` and the four detectors — fix there if the policy is wrong.
