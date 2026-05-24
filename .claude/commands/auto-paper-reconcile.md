---
description: /auto-paper-reconcile — end-of-day reconciliation for the paper-auto track. Pulls today's filled orders + currently-open orders from Tiger, matches each pending (submitted-state) paper-auto position by broker_order_id, and updates the ledger + positions.json — submitted → starter on full or partial fill (with broker's avg_fill_price), submitted → closed for DAY-expired unfilled orders. Read-and-write; only touches the paper-auto track. Supports --dry-run.
---

# /auto-paper-reconcile — EOD Paper-Auto Reconciliation

Run this after the US close (default Task Scheduler time: 4:30 PM ET). It bridges the gap between "I placed a limit order this morning" and "I know what actually filled and at what price."

**Track-bound.** Only touches `ledgers/paper-auto/<TICKER>.yml` + `journal/paper-auto/positions.json`. The human-discretionary track is never read or modified.

## $ARGUMENTS parsing

- `--dry-run` — print what would change without writing ledgers or positions.json. Still hits Tiger for the read (filled + open orders).
- `--lookback-days <N>` — how far back to pull filled orders (default 5; covers a long weekend if reconcile didn't run for a few days).

## Step 1 — Run the reconciler

```python
from tools.auto_paper.reconcile import reconcile_today
results = reconcile_today(dry_run=<args.dry_run>, lookback_days=<args.lookback_days or 5>)
```

The module:
1. Loads paper-auto positions in `submitted` state (with `broker_order_id`)
2. Pulls today's filled orders + currently-open orders via `TigerClient`
3. Per pending ledger, matches by `broker_order_id` and decides:
   - **filled** (`filled_qty == requested_qty`) → state submitted → starter; `fill_price` ← broker `avg_fill_price`
   - **partial** (`filled_qty < requested_qty`) → state submitted → starter; `shares` shrinks to `filled_qty`
   - **still_open** → no state change (rare for TIF=DAY)
   - **expired** → state submitted → closed; ledger gets a `notes` entry
   - **no_match** → no order at Tiger at all (manually cancelled outside framework)
   - **error** → broker call or ledger-write failed; surface for manual fix
4. Updates the ledger (re-validates against schema) + positions.json entry

If no positions are in `submitted` state, the reconciler returns `[]` and the command exits with `AUTO_PAPER_RECONCILE_NOTHING_PENDING`.

## Step 2 — Summary report

Output (and reply via Telegram if invoked from a Telegram session):

```
**Mode:** auto-paper reconcile  |  Dry-run: <yes|no>
**Asof:** YYYY-MM-DD HH:MM ET
**Pending positions reconciled:** N

| Ticker | Action  | Order ID | Req qty | Filled | Avg fill | Notes |
| NVDA   | filled  | #10001   | 10      | 10     | $850.42  | — |
| AAPL   | partial | #10002   | 15      | 8      | $180.55  | shrunk to 8 sh |
| MSFT   | expired | #10003   | 12      | 0      | —        | TIF=DAY expired unfilled |
| TSLA   | still_open | #10004 | 5      | —      | —        | order still open at broker |
| GOOGL  | error   | #10005   | —       | —      | —        | broker fetch: HTTP 503 |

### Track state after this run
- Paper-auto positions: N total
- in `starter` (filled today): X
- in `submitted` (still pending): Y
- in `closed` (expired today): Z
```

## Step 3 — Anything to escalate?

Surface these specifically:
- **partial fills** — the position is smaller than the strategy intended; check whether the R:R / sizing math still makes sense. Don't auto-resize the stop.
- **errors** — manual review needed. Either retry once or inspect Tiger Trader to confirm the order's actual status.
- **still_open** — rare and worth a glance; usually means the order was placed pre-market and DAY semantics work weirdly.

For these three classes, append a short "what to look at" block.

## Guardrails

- **Paper-only.** `TigerClient()` defaults to `allow_live=False`; the reconciler never overrides.
- **Track-bound.** Never reads `journal/positions.json` or writes to `ledgers/positions/`. Same boundary as `/auto-paper`.
- **Re-validates schema** on every ledger write (catch drift early).
- **Idempotent on dry-run.** A dry-run can be re-fired safely; it never mutates state.
- **Real-run is NOT idempotent.** Once a ledger is moved to `starter`, a second reconcile won't re-match (the position is no longer in `submitted` state). The reconciler only acts on `submitted`-state positions.
- **No trade recommendations.** This is bookkeeping, not portfolio management. `/p_s` or `/p_s_sync` for state inspection; `/auto-paper` for new entries.

## What's NOT done by this command (deferred to sessions 3-4)

- **Placing broker-side stops once filled** — session 3 ships OCA stop+target groups that fire automatically when a `submitted` → `starter` transition happens. For now, the stop is a number in the ledger; not at the broker.
- **Per-bar sell-decision composer auto-exit** — session 3.
- **Performance scoring** — `/auto-paper-perf` ships in session 4 (realized vs backtest expectation).
