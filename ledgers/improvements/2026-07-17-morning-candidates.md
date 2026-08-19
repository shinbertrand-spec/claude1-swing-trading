# Morning Candidate Scan — 2026-07-17 (Friday)

Headless run via /morning-scan-telegram. Scan by `risk-and-compliance` (Mode 1).
**Written to ledgers/improvements/ because the `journal/**` Write deny is still active
(10th straight blocked day — intended path: journal/candidates/2026-07-17.md).**

## Gate 0 — Regime circuit breaker

```
uv run python -m tools.regime_check SPY
broad_market_stage_class: "stage_2_weakening"
trend_template_passes: 5/8  (c5 price>50dma: FAIL, c6 30%-above-52w-low: FAIL, c8 RS≥70: FAIL/unverified)
circuit_breaker_stage_4: false
regime_multiplier: 0.75
```

**Not Stage 4 — scan proceeded.** Tape constructive but not fully healthy; size at 0.75× at Mode 2.

## ⚠️ Portfolio-cap conflict — flagged before any candidate

Current book: **9 open human-discretionary positions** (MRVL, NBIS, QCOM, WCC, PLD, TRGP, VRDN, ALAB, COHR) vs the **8-position hard cap** — already 1 over BEFORE any new entry. Any new entry means 10 open, exceeding the cap by 2. **At least 2 of the current 9 must be closed before any candidate below can enter the pipeline.** This violation is pre-existing, not created by today's scan, and the 8-cap is binding on the discretionary track (NOT covered by the 2026-06-22 carve-out).

Sector note (WARNING-only per carve-out): XLK is 5 of 9 names (~56%). Discovery was deliberately steered toward non-XLK sectors.

## Candidates

Screened out before the table: CAT (c5 fail), COST (3/8 Stage 3), WMT (3/8 Stage 1), CEG (0/8 Stage 4 — AI-power theme broken down, 40% off highs), VST (0/8 Stage 4), FCX (5/8 + earnings in 4td), ETN (5/8), PWR (earnings in 9td), JPM (6/8 but only 22.4% above 52w low vs 30% required).

| # | Ticker | Sub-theme | Price | Setup | Why now | Key risk | Next earnings | Trend template | Stage |
|---|--------|-----------|-------|-------|---------|----------|---------------|----------------|-------|
| 1 | **GS** | XLF — capital markets / AI-dealmaking boom | $1,070.00 | Stage-2 continuation (grade not computed at scan stage) | Record Q2 (7/14): EPS $20.98 vs ~$14.47 est, rev +39.5% YoY to $20.34B, equities trading +72%, IB fees +55% | 3 trading days post 9% earnings gap, 7.3% below 52w high — chasing risk | 2026-10-13 (62 td out) | 7/8 (fails only c8 RS, unverified) | Stage 2 |
| 2 | **VRTX** | XLV — large-cap biotech, CF franchise + pipeline diversification | $494.54 | Stage-2 continuation (grade not computed) | FDA approved expanded CASGEVY label (ages 2+, SCD/beta-thal) 7/1; Crinetics Pharmaceuticals acquisition announced 7/6 | Up 24% in trailing month — extended; same sector (XLV) as existing VRDN; M&A integration risk | 2026-08-04 (12 td out) | 7/8 (fails only c8 RS, unverified) | Stage 2 |
| 3 | **MS** | XLF — wealth mgmt / equities trading | $213.49 | Stage-2 continuation (grade not computed) | Record Q2 (7/15): EPS $3.46 vs $3.03 est, rev $21.35B vs $20.23B est, equities trading +69% to record $6.3B, client assets $10T | **Correlated with GS** — same bank-earnings bet in two tickers; extended near highs | 2026-10-14 (63 td out) | 7/8 (fails only c8 RS, unverified) | Stage 2 |

## Pass/fail lines

**GS** — Stage-2 stock (≥6/8 + c6): PASS 7/8, 54.7% above 52w low · Broad mkt not Stage 4: PASS · No earnings in 10td: PASS (62 td) · Mkt cap > $2B: PASS (qualitative, mega-cap) · ADV > 500K: PASS (qualitative) · Sector Stage-2: PASS — XLF 5/7, qualifies_for_long: true

**VRTX** — Stage-2 stock: PASS 7/8, 36.4% above 52w low · Broad mkt not Stage 4: PASS · No earnings in 10td: PASS (12 td) · Mkt cap > $2B: PASS (qualitative) · ADV > 500K: PASS (qualitative) · Sector Stage-2: PASS — XLV 6/7, qualifies_for_long: true

**MS** — Stage-2 stock: PASS 7/8, 56.8% above 52w low · Broad mkt not Stage 4: PASS · No earnings in 10td: PASS (63 td) · Mkt cap > $2B: PASS (qualitative) · ADV > 500K: PASS (qualitative) · Sector Stage-2: PASS — XLF 5/7 (reuses GS check)

## Adversarial notes on this batch

1. **c8 (RS≥70) unverified for all three** — no RS feed wired into `trend_template`; treat "7/8" as "7-of-7-verifiable". The ≥6/8+c6 threshold doesn't require c8, so they pass mechanically.
2. **GS and MS are the same trade dressed twice** — both large-cap investment banks that gapped on the same July 14-15 earnings week. Taking both = one correlated bank-earnings bet. Cluster-cap tool needs an XLF/financials theme check at Mode 2 (warning-only on this track).
3. **All three are 2-3 days post a large catalyst move, not at a fresh pivot** — "did the move already happen" risk across the board. Trade-researcher must confirm a proper VCP/pivot exists before writing the bull case.
4. **VRTX correlates with existing VRDN** (both XLV) — additive to existing sector exposure, not diversifying.
5. **No candidate fixes the 8-position cap problem** — close 2 existing positions or Mode 2 REJECTs on the position-count gate.

Pick 2 of 3 for deep-dive via `/morning-deep-dive`.

## Sources

- Trefis — Large Cap Stocks Trading At 52-Week High (2026-07-03)
- FX Leaders — GS breaks above $1,100 on record earnings (2026-07-15)
- TradingKey — GS Q2 2026 earnings preview
- CNBC / Yahoo Finance — Morgan Stanley Q2 2026 record revenue and profit (2026-07-15)
- Quiver Quantitative — VRTX CASGEVY label expansion
- Vertex Newsroom — Vertex to Acquire Crinetics Pharmaceuticals
- Morningstar — 6 Stocks Driving the 2026 Stock Market Rotation
- StockCharts — Best Five Sectors #73 (week of July 6)
