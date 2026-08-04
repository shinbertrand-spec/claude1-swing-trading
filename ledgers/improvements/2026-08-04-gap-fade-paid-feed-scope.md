# Scope — paid intraday feed for high-volatility gap-fade

**Pilot artifact, 2026-08-04. Suggest-only. Nothing here spends money or writes code —
it is a costed plan + a go/no-go recommendation grounded in a free Stage-0 result.**

## 1. The hypothesis, and what "deployable" requires

High-volatility **gap-fade**: on a down-gap open, buy and exit same day at the close.
The hourly probe (`2026-08-04-hourly-strategies-vol-regime-probe`) flagged it as the one
intraday mechanism with a pulse — HIGH-vol slice +0.17%/trade net @5bps, t=1.30 (thin,
below significance, un-gateable on 2yr of coarse bars).

To *deploy*, it must clear the project's live gate on 2017-2026, net of cost:
Sharpe > 1.0 AND |MDD| < 25% AND n ≥ 30 AND ≥50% of 6 rolling OOS windows clear
Sharpe > 0.5 AND Deflated Sharpe > 0.95.

## 2. Stage 0 (FREE, done) — the daily open→close proxy through the real gate

Gap-fade is a single-day strategy. The **only** part that needs intraday data is entry
*timing* (first-hour close vs the open print). The open→close leg runs on daily bars we
already have. So before spending anything, I gated the daily proxy on the 88-name
universe, 2017-2026, trend-filtered (fade only names above their 200d SMA), across gap
thresholds {1,2,3}% × cost {5,10,20} bps. Result
(`2026-08-04-gap-fade-daily-proxy-backtest`):

| gap% | 5 bps | 10 bps | 20 bps |
|---|---|---|---|
| ≥1% | Sh 1.08 / MDD **44.8%** → FAIL | 0.73 / 51.0% → FAIL | 0.02 / 68.8% → FAIL |
| ≥2% | Sh 1.06 / MDD **45.9%** → FAIL | 0.86 / 51.5% → FAIL | 0.44 / 61.5% → FAIL |
| ≥3% | **Sh 1.17 / MDD 24.5% → PASS** | 1.03 / **29.3%** → FAIL | 0.75 / 38.1% → FAIL |

**Read:** the reversion effect is real (corroborates the vol intuition), but gap-fade is a
textbook **negative-skew / short-volatility** pattern — it earns pennies most days and
takes 25-68% drawdowns when a gap-down keeps falling. It clears the gate in **exactly one
cell** — biggest gap (≥3%) at the *lowest, most optimistic* cost (5bps), and it sits right
on the 25% |MDD| cliff (24.5%). Every realistic-cost cell FAILS on drawdown.

The catch that matters: **5bps is the least believable cost here.** Gap-down opens on
single names have *wide* spreads precisely when this fires — the daily proxy is an
**optimistic upper bound**, and it barely passes even so. The two things killing the
realistic-cost cells are (a) true spread on gap-open fills and (b) tail drawdown — and
**both are exactly what a paid intraday feed would let us measure/attack.**

## 3. The decision Stage 0 leaves open

The daily proxy can't test the one lever that could rescue it: **intraday entry timing.**
Buying blindly at the open is what produces the tail drawdown (you buy names that keep
falling). Waiting for an intraday **reversal confirmation** (e.g. enter only after the
first-hour close reclaims the open, or after a 15-min higher-low) could cut the drawdown
that's failing the gate — but you need intraday bars to test it. So Stage 1 answers two
specific questions the free data cannot:
1. **What is the real gap-open spread?** If > ~5-8bps for the volatile names, the edge is
   already dead (Stage 0 cliff) — stop.
2. **Does confirmation-entry cut |MDD| below 25% at realistic cost?** If yes, deployable
   candidate → blind judge+critic. If no, retire the idea with a clean negative.

## 4. Data source options + real cost (bounded research pull, ~10-30 names, 2017-2026)

| Source | What you get | Cost | Notes |
|---|---|---|---|
| **Databento** (recommended) | US-equity 1-min bars, exchange-sourced, proper corp-action handling; pay-as-you-go | **~$50-200 one-time** for this bounded pull | Best quality; buy exactly the window/names needed, no subscription |
| **Polygon.io Developer** | 1-min aggregates, full history to 2003, unlimited REST | **$79/mo** (cancel after 1-2 mo → ~$79-158) | Easiest API; aggregates not true tick, spread must be proxied |
| **Alpaca Algo Trader Plus** | Full SIP consolidated + historical min bars from 2016 | **$99/mo** (~$99-198) | Doubles as a broker, but project routes through Tiger |
| Tiingo / IEX-only | Cheap but IEX-only history is thin/unrepresentative | $10-50/mo | **Not adequate** — need consolidated tape for real spread |

For the two questions above, **Databento one-time (~$50-200)** is the cheapest credible
path and avoids a subscription. Polygon Developer is the fallback if a subscription API is
easier to wire.

## 5. Build effort (only if Stage 1 go)

1. **Intraday adapter, PIT-safe.** Current `data_cache` is daily-only (`interval="1d"`
   hardcoded). Add an `intraday_cache` (separate cache dir + interval param), registered
   as PIT-capable per the A3 as-of contract (historical minute bars *are* replayable).
   ~0.5-1 day.
2. **Gap-fade intraday replay.** At each day's open: measure gap; if triggered, test
   entry variants — {open, first-hour close, reversal-confirmation}; exit at day close;
   charge the **real** intraday spread from the bars, not a flat bps. Emit a daily return
   series. ~0.5 day.
3. **Gate it** with the return-series harness already built (reuse Stage-0 `evaluate()`).
   Trivial.
4. **If it clears → pilot proposal → blind judge + critic.** Process wrinkle: the blind
   critic MUST re-run the backtest, so the critic needs the same paid data locally. Budget
   for that (the data is bought once, shared).

Total: **~1-2 engineer-days + $50-200 data.**

## 6. Recommendation — a NARROW, cheap Stage 1, not an infrastructure build

Stage 0 gave a **weak, conditional** justification: one passing cell, at optimistic cost,
on the drawdown cliff, with the negative skew the |MDD| gate exists to catch. That does
**not** warrant building intraday infrastructure. But it does warrant a **time-boxed
$50-200 probe** to settle it:

- Buy a one-time **Databento 1-min pull** for the ~10 most-volatile liquid names, 2017-2026.
- Test *only* the two questions in §3: real gap-open spread, and whether
  confirmation-entry drops |MDD| under 25% at that real spread.
- **Kill criterion:** real spread > 8bps on the volatile names **OR** confirmation-entry
  can't get realistic-cost |MDD| < 25% → retire gap-fade with a clean negative and stop.
- **Promote criterion:** realistic-cost variant clears the full gate → build the adapter
  properly (§5) and route it to blind judge+critic.

My lean: **this is a marginal, cost-fragile, negative-skew idea** — consistent with the
four prior sweeps concluding concentrated trend is the only durable edge here. The $50-200
probe is cheap enough to settle it definitively rather than leave it as a "maybe," but I
would **not** invest the 1-2 day infra build until that probe survives realistic cost.

Operator decides. Suggest-only; nothing applied.
