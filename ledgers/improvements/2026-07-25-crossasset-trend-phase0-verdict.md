# Cross-asset trend — Phase 0 verdict (2026-07-25)

**Question (operator's chosen lever):** enable more strategies by porting the
proven trend edge (`ts_momentum`) to cross-asset instruments for genuinely
uncorrelated return streams.

**Phase 0 scope:** validate on the EXISTING equity rails using liquid ETF/crypto
proxies (no new broker/data). Real futures/FX = the deferred infra lift, justified
only if the proxy backtest earns it.

## What's reliable

1. **Data/infra is a non-issue for validation.** The data layer is yfinance —
   it reaches ETFs, futures (`ES=F`), FX (`EURUSD=X`), and crypto (`BTC-USD`).
   All 18 probed proxies fetched; core ETFs have 2008→ history. The backtest
   needed only a universe list. The "big infra lift" is ONLY the live-broker
   side (real futures), and it's deferred.

2. **Cross-asset trend FAILS the standalone gate.** `ts_momentum` on a
   diversifier ETF universe (bonds/metals/commodities/USD/crypto):
   - gross rolling-WF: agg OOS Sharpe **0.80** (< 1.0 FAIL) · per-window **7/13
     PASS** · DSR **0.6237** FAIL
   - net-of-cost (with crypto): OOS Sharpe **0.52**, **MDD −39.6%** (crypto
     crashes drive it) → FAIL on Sharpe AND MDD
   - Regime-robust and strong exactly when equity trend breaks (2022 window
     Sharpe **2.03**, 2020 COVID 1.41) — but the aggregate doesn't clear 1.0.

3. **Decorrelation is MILD, not strong (corrected).** A v1 measurement bug
   (full-period equity curve sliced to a sub-window → non-stationary Sharpe;
   plus crypto's 7-day calendar polluting a 5-day ETF series) falsely showed
   r ≈ 0.00 and a Sharpe-7 combined book. Corrected v2 (crypto-free 8-ETF book,
   fresh-window sims, validated: ts Sharpe 1.06 ≈ psim's known 1.04):
   - **OOS correlation to ts_momentum: r = +0.205** (mild positive)
   - cross-asset crypto-free standalone Sharpe **0.38** (weak)

4. **Combined-book value is DRAWDOWN reduction, not return** (2021-06→2026-05,
   1235 weekday days, no look-ahead):

   | book | ann. Sharpe | MDD% |
   |---|---|---|
   | ts_momentum ALONE | 1.06 | −17.3 |
   | cross-asset ALONE (crypto-free) | 0.38 | −7.9 |
   | **50/50 blend** | **1.07** | **−9.6** |
   | inverse-vol (0.18/0.82) | 0.93 | −6.8 |

   50/50 leaves Sharpe flat but **nearly halves drawdown** (−17.3% → −9.6%),
   lifting Calmar. Inverse-vol *hurts* Sharpe (overweights the weak sleeve).

## Honest verdict

Cross-asset trend on ETF rails is a **real but modest drawdown-diversifier — not
a second return engine.** Crypto-free it's mildly positively correlated (+0.20)
and standalone-weak (0.38); its book value is cutting drawdown at ~flat Sharpe.
The crisis-alpha teased by the 2022 window (2.03) was largely crypto+commodities;
crypto also brings a −39.6% MDD, so it can only enter small and risk-scaled.

This does NOT clear the standalone gate and I am NOT promoting it. But it proves
the structural point: **a standalone-failing sleeve can still improve the book**
(here, on drawdown). That is the case for a portfolio-marginal gate — though at
+0.20 / flat-Sharpe, the marginal case for THIS book is a risk-reduction case,
not a Sharpe case.

## Where the return upside actually lives (cut-2 candidates, not yet run)

- **Small risk-scaled crypto-trend sleeve.** Crypto is the most idiosyncratic
  asset; at 50/50 its −39.6% MDD is disqualifying, but at ~10-15% weight it
  could add uncorrelated *return* the crypto-free book lacks. Needs business-day
  calendar handling. Cheapest high-info next run.
- **Real-futures cross-asset with SHORT capability** = the textbook managed-
  futures crisis-alpha. Requires (a) a non-equity broker adapter (big infra) AND
  (b) re-opening the long-only invariant — DECLINED here post-NFLX. Out of scope
  unless the operator explicitly re-opens shorting.

## Artifacts (this branch, ledgers/improvements/)
- `2026-07-25-crossasset_trend.yml` — inline-universe spec (Phase 0)
- `2026-07-25-crossasset_trend-gate.md` — two-clause + DSR (gross)
- `2026-07-25-crossasset-corr-combined-v2.md` + `.py` — CORRECTED decorrelation + combined book
- `2026-07-25-crossasset-netcost-corr.{md,py}`, `-combined-book.{md,py}` — v1 (SUPERSEDED — bug noted)
- `2026-07-25-crossasset-probe.py` — data-reachability probe

## Method notes
- ETF proxies trade on the existing Tiger cash-equity broker + cash-equity
  simulator — no futures roll/margin. Long-only throughout (stack invariant).
- No grid-widening: only ts_momentum's declared lookbacks (126/252).
- v1→v2 correction documented above; standalone Sharpes validated against psim.
