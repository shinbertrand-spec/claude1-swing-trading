# Roster-replenishment sprint — verdict (2026-07-25)

**Branch:** `cherrypick-batch-bc` (DSR/CPCV Batch C stack lives here).
**Mandate:** judge 3 candidates through the FULL current gate stack — two-clause
gate (gross rolling walk-forward) + net-of-cost walk-forward + C1 Deflated-Sharpe
— **no grid-widening beyond the spec files**; measure (not assert) each
candidate's return-stream correlation to the live survivor `ts_momentum_liquid_us`.
Promotion to `deployable_setups.yml` is the operator's call.

**Trial registry:** re-derived after the runs → **N = 83, unchanged**
(all three candidate grids were already registered; the runs added no new trials).
`as_of` bumped 2026-07-24 → 2026-07-25.

## Verdict table — setup × gate clause

| setup (promoted combo) | two-clause gate (gross rolling WF) | net-of-cost WF (promoted combo) | C1 DSR (N=83) | corr → ts_mom (OOS) | VERDICT |
|---|---|---|---|---|---|
| **connors_rsi2** (thr=15, cd=5) | agg OOS Sharpe **0.59** ✗ · MDD 8.5% ✓ · n 1392 ✓ · per-window 3/6=0.50 ✓ → **FAIL** | FULL 0.30 / OOS **0.58** → **FAIL** | **0.8456** < 0.95 → **FAIL** (closest) | **+0.264** | **DO NOT PROMOTE** |
| **xs_low_volatility** (bottom_n=20) | agg OOS Sharpe **0.88** ✗ · MDD 18.8% ✓ · n 177 ✓ · per-window 1/2=0.50 ✓ → **FAIL** | FULL 0.68 / OOS **0.18** → **FAIL** | **0.3415** < 0.95 → **FAIL** | **+0.004** | **DO NOT PROMOTE** |
| **event_insider_buying** (fixed) | agg OOS Sharpe **0.23** ✗ · MDD 22.9% ✓ · n 117 ✓ · per-window 1/4=0.25 ✗ → **FAIL** | FULL 0.12 / OOS **0.23** → **FAIL** | N/A (grid<2, no trial variance) | **+0.294** | **DO NOT PROMOTE** (already retired 2026-06-17; re-confirmed) |
| _ts_momentum_liquid_us (baseline)_ | _sole live survivor_ | _FULL **1.36** / OOS **1.04** → **KEEP**_ | _(roster-level)_ | _1.000_ | _CONTROL — validates harness_ |

**Result: 0 of 3 clear the bar. No replenishment. Live generic deployables remain 1 (`ts_momentum_liquid_us`).**

## Reading the numbers

- **The bar is real, not a formality.** The control (`ts_momentum_liquid_us`)
  reproduced its known net result (net OOS Sharpe **1.04**, PASS) inside the same
  harness that failed all three candidates — so the failures are the strategies,
  not the pipeline.
- **connors_rsi2** was the closest miss (DSR 0.8456, net OOS 0.58) but still fails
  every clause. Consistent with the 2026-06-09 "connors parked" finding — the
  200d-SMA regime-filter hypothesis (flat-not-loss worst window) holds on MDD
  (8.5%) but the edge is too thin to clear Sharpe 1.0. Costs barely move it
  (gross OOS 0.59 → net 0.58): low turnover, thin edge.
- **xs_low_volatility** is a **turnover-cost casualty**: gross OOS Sharpe 0.88
  collapses to **0.18 net**. The monthly-rebalance low-vol factor's gross edge is
  eaten by spread+impact. The v2 regime filter did its job on drawdown
  (MDD 18.8%, inside 25%) but there's no net edge to protect.
- **event_insider_buying** re-confirms its 2026-06-17 retirement almost exactly
  (net FULL Sharpe 0.12 here vs 0.14 then; net OOS 0.23 vs 0.12). It also fails
  the per-window clause (1/4). Retired stays retired.

## Diversification — measured, not asserted

Correlations to `ts_momentum_liquid_us` on each candidate's OOS window:
`connors +0.264`, `xs_low_volatility +0.004`, `event_insider_buying +0.294`.

The only **genuinely orthogonal** candidate is `xs_low_volatility` (r ≈ 0.00) —
but its net-of-cost OOS Sharpe is 0.18. **Orthogonality without a surviving edge
is noise, not diversification**: a zero-correlation sleeve with no edge adds
variance and cost, not risk-adjusted return. The diversification case for adding
any of these to the book does not exist because none of them has a net edge to
diversify *with*.

## Artifacts (this branch, `ledgers/improvements/`)

- `2026-07-25-connors_rsi2-roster-sprint.md` — run_spec (two-clause + C1 DSR)
- `2026-07-25-xs_low_volatility-roster-sprint.md` — run_spec
- `2026-07-25-event_insider_buying-roster-sprint.md` — run_spec
- `2026-07-25-roster-netcost-corr.md` + `.py` — net-of-cost (promoted combo) + OOS corr
- `ledgers/trials.yml` — re-derived (N=83)

## Method notes / caveats

- **Net-of-cost was run on the two-clause-PROMOTED combo**, not the tool's default
  first-combo — a fair test of the same config the gross gate would promote
  (narrowing the grid to one combo, never widening). psim defaults
  (`full_spread_marketable=True`, marketable-limit OHLC fills) = the hardened
  net-cost model from `scripts/net_gate_rerun.py`.
- **No grid-widening.** Every combo evaluated is declared in the spec files
  (connors 6, xs_lowvol 3, insider 1). Each is a priced DSR trial already in N=83.
- **Net-of-cost split is single-OOS (last-30%)**, matching the established
  net-gate; the two-clause gate is the rolling-walk-forward view. Both agree: FAIL.
