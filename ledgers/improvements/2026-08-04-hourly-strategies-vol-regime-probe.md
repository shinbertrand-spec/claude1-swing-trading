# Hourly strategies — vol-regime probe — RESEARCH-ONLY, NOT GATE-CLEARED (pilot, 2026-08-04)

**Coarse 1h bars over ~2yr of yfinance data. CANNOT be walk-forward / DSR / net-cost gated (data too shallow + hourly too coarse). Directional pulse-check only — and specifically a test of whether a VOLATILE regime rescues any hourly mechanism, per the operator's question.**

- Universe ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'AMD', 'META', 'AMZN', 'COIN', 'PLTR'] · window 2023-09-07..2026-08-03 · long-only · triggers MR<=-1.0% / MOM>=+1.0% / GAP<=-1.0%

## Headline — per-trade expectancy, net of cost

| mech | cost bps | n | avg net % | hit % | t-stat |
|---|---|---|---|---|---|
| MR | 2 | 3989 | 0.0337 | 52.0 | 1.57 |
| MR | 5 | 3989 | 0.0037 | 50.5 | 0.17 |
| MR | 10 | 3989 | -0.0463 | 48.0 | -2.17 |
| GAP | 2 | 1180 | 0.0840 | 52.3 | 1.41 |
| GAP | 5 | 1180 | 0.0540 | 51.6 | 0.91 |
| GAP | 10 | 1180 | 0.0040 | 50.4 | 0.07 |
| MOM | 2 | 4075 | 0.0217 | 50.1 | 1.02 |
| MOM | 5 | 4075 | -0.0083 | 48.3 | -0.39 |
| MOM | 10 | 4075 | -0.0583 | 46.0 | -2.76 |

## Vol-regime split (net @ 5 bps round-trip) — DOES VOLATILITY HELP?

| mech | vol regime | n | avg net % | hit % | t-stat |
|---|---|---|---|---|---|
| MR | LOW | 1395 | -0.0798 | 48.2 | -2.35 |
| MR | MID | 1282 | 0.0612 | 54.1 | 1.83 |
| MR | HIGH | 1312 | 0.0362 | 49.4 | 0.84 |
| GAP | LOW | 420 | -0.0471 | 49.3 | -0.60 |
| GAP | MID | 396 | 0.0224 | 52.8 | 0.22 |
| GAP | HIGH | 357 | 0.1697 | 52.4 | 1.30 |
| MOM | LOW | 1267 | -0.0200 | 47.1 | -0.68 |
| MOM | MID | 1398 | -0.0022 | 48.1 | -0.07 |
| MOM | HIGH | 1410 | -0.0039 | 49.5 | -0.09 |

## Verdict (indicative — NOT a gate verdict; cannot deploy off this)
- **No mechanism clears a per-trade t-stat > 2 at realistic (5bps) cost.** Hourly edge is not distinguishable from noise net of cost in this window.
- MR: HIGH−LOW vol expectancy delta = +0.1160% → HIGH-vol materially better.
- GAP: HIGH−LOW vol expectancy delta = +0.2167% → HIGH-vol materially better.
- MOM: HIGH−LOW vol expectancy delta = +0.0161% → marginal.
- Suggest-only; nothing applied. A real hourly test needs a paid minute/hourly source (Alpaca/Polygon) with 8-10yr depth to run the actual gate.

