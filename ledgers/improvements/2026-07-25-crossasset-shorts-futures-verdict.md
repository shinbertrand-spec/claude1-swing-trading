# Cross-asset trend — shorts & futures (Phase 0b verdict, 2026-07-25)

**Operator direction:** drop crypto; explore shorts + futures.

**What was tested (safely):** synthetic shorts via -1x inverse ETFs (SH/PSQ/RWM/
TBF/EFZ/EUM/DGZ) added to the long diversifier book. Inverse ETFs are LONG
positions that rise when the underlying falls → down-trend capture on the
existing long-only rails. **No real shorts, no new broker, no removal of the
NFLX long-only guards.** Same ts_momentum engine, crypto-free.

## Result: the short side decorrelates but doesn't pay

| book (2021-06→2026-05, 1235 weekday days) | corr → ts_mom | standalone Sharpe | 50/50 combined Sharpe | 50/50 combined MDD |
|---|---|---|---|---|
| long-only cross-asset (crypto-free) | **+0.205** | 0.38 | 1.07 | −9.6% |
| **+ synthetic shorts (inverse ETFs)** | **+0.063** | **0.18** | **1.06** | **−10.0%** |
| ts_momentum ALONE | 1.000 | 1.06 | — | −17.3% |

- Standalone L/S gate: agg Sharpe **0.44**, MDD **29.2%** (breaches 25%),
  per-window 5/13, DSR 0.354 — **worse** than long-only on every clause.
- The short side lowered correlation (+0.205 → **+0.063**, toward zero) — it IS
  decorrelating as theory predicts. **But** the combined book didn't improve
  (Sharpe 1.06 vs 1.07; MDD −10.0% vs −9.6%).
- **Why:** -1x inverse ETFs carry daily-reset **decay** + expense drag, and they
  fight the equity risk premium in bull markets. The extra decorrelation is
  exactly cancelled by the implementation cost. This is a well-known inverse-ETF
  property, and the net-of-cost backtest reproduced it (decay is embedded in the
  actual price series).

## The important nuance — this does NOT kill the real-shorts idea

The inverse-ETF proxy has a **decay confound**: it can't cleanly test whether the
short side pays, because the ETF's implementation cost masks the signal. **Real
short futures are decay-free** — no daily reset, no expense ratio, and they earn
the short leg cleanly. The 2022 window (L/S Sharpe **1.91**, exactly when equities
crashed) shows the crisis alpha is THERE; the proxy just bleeds it away the other
years via decay.

So the honest state: **synthetic shorts (cheap, safe) don't pay. Whether REAL
shorts pay is unproven — and the ETF proxy structurally cannot prove it.**

## The fork this leaves

To settle whether the short side is worth pursuing, in order of cost/risk:

1. **Research-only clean long/short backtest (SAFE, next step).** Model true
   decay-free shorts (short P&L = −asset return, + realistic borrow/cost) in a
   research simulator — NO live orders, NO doctrine change. Answers "does the
   short side pay when implemented cleanly?" If yes → justifies the infra +
   doctrine cost. If no → shorts don't pay here; stop. **Must be built carefully
   and validated (a bespoke sim; the v1 measurement bug this session is the
   cautionary tale).**
2. **Real short futures (LIVE).** Requires (a) a futures-capable broker adapter
   (Tiger equity stack doesn't have it — real infra) AND (b) **dismantling the
   long-only invariant** — the `short_anomaly` bucket, point-of-sale guard, and
   sign-aware reconciler built after the NFLX incident that cost $129k three
   weeks ago. This is a deliberate doctrine reversal requiring explicit operator
   authorization and careful sequencing. **Evidence-gate it on step 1 — do not
   build it for an unproven edge, and do not remove the NFLX guards on spec.**

## RESULT — clean decay-free long/short backtest (step 1, DONE)

Ran a research-only decay-free long/short trend sim (short P&L = −asset return,
8 bps turnover + 30 bps/yr borrow, futures roll unmodeled), A/B within one sim
(long-only vs long/short, same universe incl. equity indices SPY/QQQ/IWM/EFA/EEM
so shorts can capture equity crashes), 2018→2026 (COVID + 2022 shock).
Validation: ts through the sim = Sharpe 1.62 (psim ref ~1.0-1.4 — runs HOT: no ATR
stops, gross=1; trust the A/B DELTA, not absolute levels).

| cross-asset mode | standalone Sharpe | corr → ts | 50/50 combined Sharpe | combined MDD |
|---|---|---|---|---|
| long-only | 0.75 | +0.52 | **1.36** | −14.7% |
| long/short (clean) | 0.28 | **+0.18** | **1.24** | **−8.3%** |
| _ts alone_ | _1.62_ | _1.00_ | — | — |

**Clean shorts trade RETURN for DRAWDOWN protection — they don't pay on Sharpe.**
- Decorrelate strongly (0.52 → 0.18) ✓ — the short side IS a genuine crisis hedge.
- Crush standalone Sharpe (0.75 → 0.28) ✗ — trend shorts bleed fighting the equity
  risk premium in a bull-dominated sample; the 2022 crisis alpha doesn't offset it.
- Combined: shorts LOWER Sharpe (1.24 < 1.36 < 1.62) but LOWER drawdown
  (−8.3% < −14.7%). Insurance that costs a premium.
- **Decay was never the main problem — direction is.** Clean shorts don't pay
  either; they buy modest drawdown reduction at a Sharpe cost.

**Verdict: the short side does NOT justify the futures-broker build + dismantling
the NFLX long-only guards.** You'd pay a huge operational + doctrine cost to LOWER
risk-adjusted return for a modest tail-hedge that the long-only cross-asset
diversifier already largely provides (−14.7% combined) with zero shorting, zero
new broker, zero guard removal. **Recommendation: do not pursue real shorts/futures.**

Caveat (regime): 2018-2026 is bull-dominated (only 2020 + 2022 drawdowns; 2008
predates ts data). A sustained bear/high-vol regime would favor shorts more — but
betting the doctrine reversal on a regime forecast is exactly the discretionary
call this stack avoids.

## Standing conclusion (both Phase 0 + 0b + clean-short test)

Cross-asset trend via ETFs — long-only OR synthetic-short — is a **drawdown
diversifier that does not add return** on the existing rails. The short side
decorrelates but the ETF decay makes it not pay. Real futures/shorts *might*
change that, but only a clean research backtest can tell, and live implementation
is a serious infra + doctrine commitment gated on that evidence.

## Artifacts (ledgers/improvements/)
- `2026-07-25-crossasset_trend_ls.yml` — L/S inline spec (longs + inverse ETFs)
- `2026-07-25-crossasset_trend_ls-gate.md` — two-clause + DSR (standalone FAIL)
- `2026-07-25-crossasset-ls-corr-combined.{md,py}` — decorrelation + combined book
- `2026-07-25-inverse-etf-probe.py` — inverse-ETF reachability
- `2026-07-25-crossasset-trend-phase0-verdict.md` — long-only Phase 0 verdict
