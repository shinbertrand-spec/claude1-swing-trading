# Clean decay-free long/short cross-asset trend — research backtest

- Eval window: 2018-01-01 → 2026-05-25 (incl. 2020 COVID + 2022 rate-shock)
- Cross-asset underlyings (both directions in L/S mode): TLT, IEF, GLD, SLV, DBC, DBA, USO, UUP, SPY, QQQ, IWM, EFA, EEM
- Clean shorts: P&L = -(asset return), NO decay; costs = 8 bps turnover + 30 bps/yr borrow; futures roll not modeled
- **VALIDATION:** ts_momentum through this sim (long-only) Sharpe = **1.62** (psim ref ~1.0-1.4 — CHECK)

## Standalone (my-sim, gross=1)

| cross-asset mode | Sharpe | MDD% | avg positions | avg shorts |
|---|---|---|---|---|
| long-only | 0.75 | -22.6 | 6.9 | 0 |
| **long/short (clean)** | 0.28 | -30.7 | 8.0 | 2.7 |

## Correlation to ts_momentum + equal-risk 50/50 combined book

| cross-asset mode | corr → ts | combined Sharpe | combined MDD% |
|---|---|---|---|
| long-only | +0.523 | 1.36 | -14.7 |
| long/short | +0.183 | 1.24 | -8.3 |

_ts_momentum solo (equal-risk basis) Sharpe ≈ 1.62 over this window._

**Decision rule:** clean shorts PAY iff the long/short row shows (a) a LOWER (ideally negative) corr than long-only AND (b) a combined book that beats both ts-alone and the long-only-combined on Sharpe or MDD. Otherwise the short side does not justify the futures-broker + long-only-doctrine-reversal cost.
