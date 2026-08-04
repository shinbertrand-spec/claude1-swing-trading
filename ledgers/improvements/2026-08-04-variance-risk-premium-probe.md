# Variance Risk Premium / delta-hedged gamma — PROXY (pilot, 2026-08-04)

**Research-only. Idealized UPPER BOUND on a delta-hedged SHORT-vol book (ignores option bid/ask, discrete-hedge error, vega/skew, margin — all adverse). A real book is strictly worse.**

- Non-overlapping monthly obs: **n=113** (2017-01..2026-05)
- Avg variance risk premium: **+3.7 vol points** (implied − realized; positive = short-vol earns carry)
- SHORT-GAMMA carry: monthly mean +0.0067 var-pts · **annualized Sharpe 0.31** · hit 86% · **skew -7.40** · |MDD| 72%
- LONG-GAMMA (gamma scalp, the 'volatile market' play) = the exact mirror: Sharpe -0.31, POSITIVE skew 7.40, pays the carry, wins the tail.

### Worst 6 months for SHORT gamma (the crash tail you are selling insurance against)
| month | implied | realized | short-γ P&L (var-pts) |
|---|---|---|---|
| 2020-03 | 33 | 91 | -0.7110 |
| 2025-04 | 22 | 52 | -0.2222 |
| 2018-12 | 16 | 30 | -0.0648 |
| 2018-02 | 13 | 28 | -0.0603 |
| 2018-10 | 12 | 22 | -0.0349 |
| 2020-02 | 18 | 25 | -0.0318 |

### Best 3 months for SHORT gamma (calm = collect premium)
| month | implied | realized | short-γ P&L (var-pts) |
|---|---|---|---|
| 2020-04 | 57 | 40 | +0.1648 |
| 2020-11 | 37 | 16 | +0.1117 |
| 2020-05 | 37 | 22 | +0.0881 |

## Read
- The premium is REAL: implied runs ~3.7 vol points over realized on average, short-vol hit rate 86%. This is the well-documented variance risk premium.
- But it is a NEGATIVE-SKEW (skew -7.40) insurance-selling carry: a handful of crash months (see table) erase many months of premium. Sharpe here is the IDEALIZED ceiling; real option spreads + hedge error push it materially lower.
- LONG gamma is the mirror — the honest 'profit from a volatile market' expression — but it PAYS the premium every calm month and only wins when realized blows past implied.
- Neither is expressible in the current equity-only, long-only stack. See the companion scope note.
- Suggest-only; nothing applied.
