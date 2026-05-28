---
description: /auto-paper-monitor — intraday per-bar sell-decision composer + trailing-stop ratchet for the paper-auto track. For every position in `starter` state, fetches recent OHLCV, runs the five sell-discipline detectors (climax_top, violations, base_stage, sell_into_strength, pe_expansion via EDGAR), composes via tools.sell_decision. If the composer returns a non-hold action, auto-places a limit-sell via TigerClient (bid - 0.1%), cancels the resting broker stop, and transitions the ledger to closed. Then ratchets remaining starter positions' stops upward per CLAUDE.md trailing rules (+5% gain -> stop to BE; +10% gain -> stop to +5%). Read-and-write; only touches the paper-auto track. Cron-eligible every 30 min during US session. Supports --dry-run.
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
3. Runs the five sell-discipline detectors:
   - `tools.climax_top_detect` — 6 climax-top patterns
   - `tools.violations_detect` — 5 post-entry violations
   - `tools.base_stage_detect` — base count + new-high flag
   - `tools.sell_into_strength` — 10-15% in 2-3 days
   - `tools.pe_expansion_check.compute_from_ticker` — TTM EPS via EDGAR (edgartools); falls back to False on any error (unknown ticker, ADR, negative EPS, network). The result lands in `sell_eval_history.pe_doubled_late_stage` regardless.
4. Composes via `tools.sell_decision.compute(...)`.
5. If composer returns a SELL action (`sell_50` / `sell_75` / `sell_100`):
   - Places a limit-sell at last close × (1 − 0.001) per CLAUDE.md execution rules
   - Cancels the resting broker-side stop (recorded as `position_state.stop_order_id` by `/auto-paper-reconcile`) so we don't leave a stale stop after exit
   - Transitions the ledger to `closed` with `exit_price` + `exit_reason`
   - Updates `journal/paper-auto/positions.json` entry to `stage: closed`
6. Otherwise (`hold` or other) just appends to `sell_eval_history` and leaves the position open.

If no positions are in `starter` state, returns `[]` and the command exits with `AUTO_PAPER_MONITOR_NOTHING_OPEN`.

## Step 1a — Refresh DAY-expired broker stops (self-healing)

Before evaluating exits and before ratcheting, ensure every `starter` position has a live broker-side STP. Tiger paper STP orders are DAY-only and auto-cancel at session close; a fresh stop must be re-armed on the first monitor pass of the new session. The composer in Step 1 also depends on a live stop to cancel before exit — without this refresh, the exit path would try to cancel a non-existent order.

```python
from tools.auto_paper.reconcile import refresh_starter_stops
from tools.broker.tiger import TigerClient
client = TigerClient()
refresh_results = refresh_starter_stops(client=client, dry_run=<args.dry_run>)
```

Outcomes per starter:
- `stop_intact` — ledger's `stop_order_id` is live at the broker; no-op
- `stop_replaced` — placed a fresh STP at the ledger's `current_stop`, recorded the new `stop_order_id` on the ledger
- `stop_dry_run` — would have placed a fresh STP (`--dry-run`)
- `error` — could not refresh; ledger / broker state may need manual review

Surface `stop_replaced` and `error` rows in the summary so the operator can audit. `stop_intact` is the steady-state expectation post-fix.

## Step 1b — Trailing-stop ratchet (post-exit pass)

After Step 1 finishes, ratchet broker-side stops upward for any positions that survived (didn't get closed by the composer):

```python
from tools.auto_paper.stop_ratchet import ratchet_all
ratchet_results = ratchet_all(dry_run=<args.dry_run>)
```

Per CLAUDE.md § Risk Management:

- `gain >= 5%` → stop moves to break-even (entry price)
- `gain >= 10%` → stop moves to +5% (entry × 1.05)

Mechanic: cancel old broker STP SELL → place new STP SELL at the higher price → update `position_state.current_stop` + `position_state.stop_order_id` on the ledger. Idempotent — positions already at or above target are skipped. If `place_stop_loss` fails after a successful cancel, the ledger records the unprotected state in `notes` and clears `stop_order_id` so the next pass retries. Never lowers a stop.

Surface ratchet outcomes in the summary table alongside exit outcomes.

## Step 2 — Summary report

Output (and reply via Telegram if invoked from a Telegram session):

```
**Mode:** auto-paper monitor  |  Dry-run: <yes|no>
**Asof:** YYYY-MM-DD HH:MM ET
**Starter positions evaluated:** N

| Ticker | Action  | Detail |
| NVDA   | hold    | composer = hold (no triggers fired). Ratchet: no_change (gain +2.1% below tier-1) |
| AAPL   | sell_100 | composer = sell_100 (climax_top: gap_up_high_vol; pe_doubled=true). Placed SELL #11042 @ $180.27, cancelled stop #11001, closed @ $180.27 (vs entry $180.50, R=-0.05) |
| GOOGL  | hold    | composer = hold. Ratchet: tier-2 — old stop $390.00 → $420.00 (gain +12.0%) |
| MSFT   | error   | broker fetch: HTTP 503 |
| ...

### Track state after this run
- Starter positions: N total
- Closed via sell-composer this run: K
- Stops ratcheted this run: R (tier-1: X, tier-2: Y)
- Still open: M
```

## Step 3 — Escalation

Surface these specifically:

- **closed positions** — they're done; check the realized R-multiple and exit_reason. Surfaced in `/auto-paper-perf` next time it runs.
- **errors** (broker call failures) — manual review. Position stays in `starter`; check Tiger Trader app for actual state.
- **cancel rejections** — if the stop cancellation fails (e.g. stop already filled), the ledger still transitions to `closed` and a warning surfaces. Verify in Tiger Trader app — you may have a duplicate / orphan sell at the broker.
- **ratchet error after cancel** — if `place_stop_loss` fails after a successful cancel, the position is **temporarily unprotected**. The ledger records this in `notes` and clears `stop_order_id`. Investigate before next cron tick; next ratchet/reconcile pass will retry.

## v1 simplifications (deferred to a later session)

- **Partial sells (sell_50, sell_75) close the WHOLE position.** Pyramid leg management is a future-session enhancement. The ledger note records what the composer actually wanted (e.g. "composer=sell_50; treated as full close v1").
- **Bid offset uses last-bar Close** — the module doesn't have live L1 quotes. The limit lands close to last and Tiger fills against the current book.
- **OCA stop+target groups deferred** — Session 3 places a plain STP SELL only; bracket-style OCA with a profit target leg is post-MVP.

## Guardrails

- **Paper-only.** `TigerClient()` defaults to `allow_live=False`; the monitor never overrides.
- **Track-bound.** Never reads `journal/positions.json` or writes to `ledgers/positions/`.
- **Re-validates schema** on every ledger write.
- **Idempotent on dry-run.** A dry-run can be re-fired safely — it only appends a `sell_eval_history` entry; it never closes a position or touches the broker.
- **Real-run is idempotent at the position level.** Once a ledger transitions to `closed`, it's no longer in `starter` state and won't be re-evaluated.
- **No trade recommendations.** This is mechanical exit policy following the composer's verdict. The composer's logic is in `tools.sell_decision` and the four detectors — fix there if the policy is wrong.
