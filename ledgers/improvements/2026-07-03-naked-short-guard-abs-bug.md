# Naked-short guard defeated by abs() — refresh_starter_stops re-arms stops on SHORT positions

**Severity:** HIGH — live hazard on next real monitor/reconcile cron run (Monday 2026-07-06, 10:00 ET).
**Found:** 2026-07-03 during /auto-paper-monitor (dry-run, market holiday).
**Incident class:** NFLX −29,760 runaway short (2026-06-30 memory note); COIN −639 (2026-06-02).

## The bug

`tools/auto_paper/reconcile.py:552` (inside `refresh_starter_stops`):

```python
held = int(abs(holdings.get(ticker.upper(), 0)))
if held < 1:
    # not_held guard ...
```

The `abs()` converts a broker SHORT (negative qty) into an apparent long holding.
NFLX is short −29,760 at Tiger; `abs(−29760) = 29760 ≥ 1` → the guard passes and
the function falls through to place a fresh `STP SELL 29,760 @ $77.23`.

Confirmed by today's dry-run:

```
{'ticker': 'NFLX', 'action': 'stop_dry_run', 'requested_qty': 29760,
 'reason': 'would place STP 29760sh @ $77.23'}
```

NFLX last close is $77.65 — the stop trigger is **0.5% below current price**. If
re-armed Monday and price dips, the STP SELL fires and the short doubles to
−59,520. Note the guard's own docstring names the COIN −639 short as its
motivation — but `abs(−639) = 639` would never have tripped it either. The guard
as shipped only catches FLAT positions, never shorts.

## Proposed fix (one line + message)

```python
        # Naked-short guard: refuse to place a SELL stop the broker can't back.
        # SIGNED quantity — a broker SHORT (negative qty) must trip the guard,
        # not pass it via abs() (the NFLX −29,760 runaway of 2026-06-30: a STP
        # SELL re-armed on a short deepens the short when it fires).
        held = int(holdings.get(ticker.upper(), 0))
        if held < 1:
            results.append(ReconcileResult(
                ticker=ticker, action="not_held",
                reason=("ledger=starter but broker holds <1 share (flat or "
                        "SHORT); STP SELL NOT placed (would be / deepen a naked "
                        "short). Journal/broker desync — stuck-closing / "
                        "pre-session sweep reconciler's domain."),
            ))
            continue
```

Downstream clamp `min(journal_shares, held)` at line ~592 is unaffected
(only reached when `held >= 1`).

## Regression test (add to tests/test_auto_paper_reconcile.py)

```python
def test_refresh_not_held_guards_broker_short(paper_dirs):
    """Ledger=starter but broker is SHORT -> not_held, NO STP SELL placed.

    Regression for the NFLX -29,760 runaway (2026-06-30): abs() on the signed
    broker qty let shorts pass the naked-short guard and re-armed a STP SELL
    that would deepen the short when it fires."""
    _seed_starter(paper_dirs, ticker="NFLX", shares=29760, stop_price=77.23,
                  stop_order_id=44002)
    client = _client(open_=[])
    results = reconcile.refresh_starter_stops(
        client=client, holdings={"NFLX": -29760})
    assert [r.action for r in results] == ["not_held"]
    assert _stp_sell_calls(client) == []          # critically: nothing placed
```

## Related unsigned-qty sites (the full "reconciler short-fix" — separate session)

The same short-as-long assumption exists elsewhere and is the engine of the
2026-06-30 runaway loop (short → classified "held" → stuck-closing flip-back →
ledger resurrected as LONG 29,760 → composer sell deepens short → repeat):

| Site | Effect on a short |
|---|---|
| `orphan_check.py:135` `_held_tickers` (`abs(q) >= 1`, docstring says "longs OR shorts") | short counts as held → feeds stuck_closing + orphan classification |
| `reconcile.py:899` stuck-closing flip-back `qty = int(abs(holdings[ticker]))` | resurrects closed ledger as a LONG sized to the short |
| `reconcile.py:936` orphan discovery `filled_qty=int(abs(...))` | cosmetic (reporting) |
| `reconcile.py:1468` intent-recovery `held` set | short counts as held |
| `reconcile.py:1581` `held_qty = int(abs(...))` | intent-recovery sizing from a short |
| `reconcile.py:1781` (pre-session sweep helper) | short counts as held |
| `stop_ratchet.py` | no lock support, no short guard (low risk: needs +5% gain) |
| `exits.py` | respects `position_state.operator_locked` (used for NFLX today) |

Design suggestion for the full fix: `classify_holdings` should put `q <= -1`
into a dedicated `shorts` bucket that ALERTS + GATES (like orphans) and is
excluded from every flip-back / stop-arm / sell path. A short in a long-only
system is always a critical desync, never a position to manage.

## Interim containment (in place as of 2026-07-03)

1. `ledgers/paper-auto/NFLX.yml` → `position_state.operator_locked: true`
   (blocks `evaluate_exits` from placing sells; noted in ledger `notes`).
2. NFLX's recorded stop #43794434314880000 is NOT live at the broker (DAY
   expired) — nothing re-arms it while the market is closed today.
3. NOT yet contained: Monday 10:00 ET monitor cron will re-arm the phantom
   stop via `refresh_starter_stops` unless (a) this fix lands, or (b) the
   `ClaudeTradingAutoPaperMonitor` + `ClaudeTradingAutoPaperReconcile` tasks
   are disabled, or (c) NFLX is flattened at the broker first.
