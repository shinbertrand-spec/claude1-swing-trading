# Scope — futures trend & options delta/gamma hedging (feasibility)

**Pilot artifact, 2026-08-04. Suggest-only. No code, no spend — a costed feasibility read
grounded in the free VRP proxy (`2026-08-04-variance-risk-premium-probe`).**

The operator bundled two very different things. They have opposite implications.

## A. Futures (trend) — the edge we already have, on a better instrument

Time-series momentum on futures is the *same shape* as `ts_momentum` — the one surviving
live deployable. This is not a new edge; it's an **instrument upgrade** for a known edge:
futures add leverage, 24h access, and genuine cross-asset diversity (rates / FX /
commodities / equity index) that ETF proxies approximate with a decay confound.

- **What's missing:** real futures price history (continuous-contract, roll-adjusted) +
  a futures data adapter + a futures-capable broker paper account. The project is
  equity-only today.
- **Data cost:** Databento / CSI Data / Norgate futures — **~$50-300 one-time** for a
  bounded continuous-contract pull across ~20-40 liquid contracts, 2000-2026.
- **Standing discipline (from memory `project_crossasset_trend`):** run a **clean
  long/short trend backtest on REAL futures** BEFORE any infra or doctrine change. Live
  shorts = dismantling the NFLX long-only guard, which requires explicit operator auth.
- **Verdict:** the **more defensible** of the two directions — it extends a *proven* edge
  rather than betting on a new one. Gated behind a data pull + the long/short research
  backtest, not a green light to build.

## B. Options with delta/gamma hedging — a different business, and a negative-skew trap

A continuously delta-hedged option's P&L is, to leading order, the **variance-swap payoff**
`realized_var − implied_var`. So VIX-vs-realized *is* the delta-hedged edge — no option
data needed to measure it. The free proxy (idealized upper bound, before option spreads /
hedge error / margin) over 113 non-overlapping months, 2017-2026:

| Metric | Short-gamma (harvest premium) | Long-gamma (the "volatile market" play) |
|---|---|---|
| Avg variance risk premium | **+3.7 vol points** (implied > realized) | pays −3.7 vol pts carry |
| Hit rate | 86% of months | 14% of months |
| Idealized annualized Sharpe | **0.31** | −0.31 |
| Skew | **−7.40** | +7.40 |
| |MDD| | **72%** | — |

Worst short-gamma months: **2020-03** (implied 33 → realized 91, one month erases years),
**2025-04** (implied 22 → realized 52 — a *recent* spike), 2018-12, 2018-02 (volmageddon).

**Read:**
- The premium is **real** (+3.7 vol pts, 86% win rate) — the textbook variance risk
  premium. But harvesting it short-gamma is an **insurance-selling carry**: idealized
  Sharpe 0.31, skew −7.4, 72% drawdown — the *same negative-skew tail that just killed
  gap-fade, leveraged*. And 0.31 is the ceiling; real option bid/ask + discrete hedging
  push it lower.
- **Long gamma** — the honest "profit from a volatile market" expression — **pays the
  premium every calm month** and only wins when realized blows past implied (2020-03,
  2025-04). The market already *charges* for the "vol clusters" insight: implied sits 3.7
  points above realized precisely because everyone knows it. There is no free lunch on
  either side.
- **Infra gap is large:** historical option chains + IV surface (OptionMetrics ~$1000s/yr,
  ORATS ~$100s/mo, CBOE DataShop pay-per-set, Polygon options ~$199/mo) + an options
  greeks/hedge-simulator engine (build) + an options-capable broker paper account
  (Tiger is equity long-only today) + a completely different risk regime (negative gamma,
  margin, pin risk).
- **Verdict:** **not worth building.** It's a leveraged version of the negative-skew carry
  the project keeps rejecting, the edge is a mediocre Sharpe even idealized, and the
  infrastructure lift is the heaviest of anything scoped. If the operator ever wants vol
  exposure cheaply, note it's already tradeable via ETFs (SVXY/VXX) — but those carry the
  same inverse-ETF decay confound already falsified in `project_crossasset_trend`.

## Recommendation

Between the two: **futures trend is the only one worth a next step**, and only as a *free*
long/short research backtest on real futures data (~$50-300 one-time) — because it extends
`ts_momentum`, a proven edge, rather than opening a new negative-skew business. **Options
delta/gamma hedging: pass** — real premium, poor risk-adjusted return, heaviest infra,
and the market already prices the volatility insight it's built on.

Operator decides. Suggest-only; nothing applied.
