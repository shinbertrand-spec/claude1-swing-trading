# Morning Candidate Scan — 2026-07-21

Headless run (`/morning-scan-telegram`, Windows Task Scheduler). Scan by `risk-and-compliance` subagent, Mode 1.

> **Write-deny workaround (day 11+):** `journal/candidates/` Write still denied in headless sessions
> (settings.local.json deny active since 2026-07-06). Report written here per the proven fallback;
> Telegram summary pushed directly via `scripts/send-to-telegram.ps1`. Canonical path once fixed:
> `journal/candidates/2026-07-21.md`.

## Gate 0 — Regime circuit breaker

```
uv run python -m tools.regime_check SPY
```

`broad_market_stage_class = "stage_2_weakening"` (trend_template_passes = 5/7). **Not Stage 4.** Circuit breaker does not trip — proceed to scan. (`regime_multiplier: 0.75` — sizing should reflect the weakening tape, not full-conviction sizing.)

## Portfolio cap flag (standing violation, independent of any new candidate)

Current book: MRVL, NBIS, QCOM, WCC, PLD, TRGP, VRDN, ALAB, COHR = **9 open positions**. CLAUDE.md hard rule caps concurrent positions at **8**. This is already a breach today, before any new entry is considered. **Any candidate below cannot be entered until an existing position is closed first** — this is not optional, it's the binding hard rule (unlike the sector/cluster caps, which are WARNING-only on the discretionary track per the 2026-06-22 carve-out). The scan does not recommend which position to close; that's the operator's call, but the gate is real.

## Candidate scan

Discovery via finviz new-highs/horizontal-breakout screen (`--f=ta_highlow52w_nh,ta_pattern_horizontal,sh_avgvol_o500`) plus web catalyst checks. 11 tickers ran through `trend_template` + `earnings_calendar`. Disqualified before the table: **VLO, PBF, HXL** (earnings 7 trading days out, inside the 10-day blackout); **ZD** (sector XLC only 2/7, fails sector-qualifies rule); **DK, PAA, PARR, KNTK** all cleared every hard rule but are the same correlated refining-complex trade as PSX — excluded from the final 3 to avoid padding with a redundant name (noted as a risk on PSX instead).

| # | Ticker | Sub-theme | Current price | Why now (1 line) | Key risk | Next earnings | Trend template | Stage |
|---|--------|-----------|---------------|------------------|----------|---------------|----------------|-------|
| 1 | PSX | Refining margin expansion | $212.17 | Raymond James/Citi/Jefferies all hiked PT (RJ → $235) on tight crack spreads + falling gasoline inventories | Whole refining complex (DK/PAA/PARR/KNTK) cleared the same screen simultaneously — a crack-spread reversal hits the cluster at once; earnings 11 trading days out, near the edge of a comfortable hold window | 2026-08-05 (11 trading days) | 7/8 (c8 RS-rating not sourced this session — see note) | 2 |
| 2 | RXO | Freight-brokerage capacity tightening | $28.72 | Brokerage volumes +80% YoY on tightening truckload capacity; BMO initiated Outperform $35 PT (+29%), Barclays turned bullish mid-July | Stock already +114% YTD — extended move, elevated reversal/climax risk on any freight-rate disappointment | 2026-08-06 (12 trading days) | 7/8 (c8 RS-rating not sourced) | 2 |
| 3 | ESTA | Breast-implant/reconstruction medtech | $94.54 | Q1 revenue +44.7% YoY, guidance raised to $266.5-268.5M; Citi hiked PT to $92, added to Russell 2000 | Small-cap ($2.78B) thin ADV (611K) magnifies slippage/gap risk; implant-maker litigation overhang is a structural industry risk even with no active suit found this session | 2026-08-06 (12 trading days) | 7/8 (c8 RS-rating not sourced) | 2 |

**Note on criterion 8 (RS rating):** `trend_template` requires a live IBD-style RS-rating input (`--rs-rating N`) that had no wired data source this session — it defaults to fail absent that input. All three candidates clear 7/8 core criteria including criterion 6, which is above the ≥6/8 threshold regardless of c8, so this doesn't change any pass/fail call, but it means these are not confirmed 8/8 A+ setups — flag as a residual unknown.

### Per-candidate pass/fail lines

**#1 PSX**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — 7/8, c6=true
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 5/7
- No earnings within 10 trading days: **pass** — 11 trading days to 2026-08-05
- Market cap > $2B: **pass** — $85.1B
- Avg daily volume > 500K: **pass** — 2.60M shares
- Sector qualifies for long (XLE): **pass** — 7/7, qualifies=True

**#2 RXO**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — 7/8, c6=true
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 5/7
- No earnings within 10 trading days: **pass** — 12 trading days to 2026-08-06
- Market cap > $2B: **pass** — $4.74B
- Avg daily volume > 500K: **pass** — 2.31M shares
- Sector qualifies for long (XLI): **pass** — 6/7, qualifies=True

**#3 ESTA**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — 7/8, c6=true
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 5/7
- No earnings within 10 trading days: **pass** — 12 trading days to 2026-08-06
- Market cap > $2B: **pass** — $2.78B (marginal; also note CLAUDE.md's current fundamental framework treats volume, not market cap, as the binding liquidity constraint — moot here since both clear)
- Avg daily volume > 500K: **pass** — 611K shares (thinnest of the three — flagged as a risk above)
- Sector qualifies for long (XLV): **pass** — 6/7, qualifies=True

All three candidates pass every mechanical hard rule. None is padding — DK/PAA/PARR/KNTK also passed but were excluded as redundant refining-complex duplicates of PSX rather than inflating the list to 3 with correlated names.

**Pick 2 of 3 to deep-dive. Trades only enter the pipeline once `trade-researcher` builds their ledger and risk-and-compliance verifies via Mode 2.**

Reminder: even after deep-dive, the 8-position cap (currently at 9, already in breach) means no new entry places until an existing position closes — Gate 4 of the Mode 2 verification will hard-FAIL any of these on the position-count rule until that happens.

## Sources

- [PSX Stock Jumps As Analysts Hike Targets On Refining Tailwinds](https://www.timothysykes.com/news/phillips66-psx-news-2026_07_17/)
- [Phillips 66 Gains as Strong Fuel-Margin Backdrop Lifts Refiners](https://www.quiverquant.com/news/Phillips+66+Gains+as+Strong+Fuel-Margin+Backdrop+Lifts+Refiners)
- [Can Tight Fuel Markets Benefit Phillips 66's Refining Business?](https://finance.yahoo.com/energy/articles/tight-fuel-markets-benefit-phillips-173200873.html)
- [This trucking stock has doubled in 2026. BMO says it has more room to run](https://www.cnbc.com/2026/07/14/this-trucking-stock-has-doubled-in-2026-bmo-says-more-gains-will-come.html)
- [RXO Pops As Barclays Turns More Bullish On Freight](https://www.tipranks.com/news/catalyst/rxo-pops-as-barclays-turns-more-bullish-on-freight)
- [Why RXO (RXO) Is Up 27.6% After Momentum Surge In Freight-Tech Valuation Repricing](https://finance.yahoo.com/markets/stocks/articles/why-rxo-rxo-27-6-131416190.html)
- [Establishment Labs stock hits 52-week high at 90.54 USD](https://www.investing.com/news/company-news/establishment-labs-stock-hits-52week-high-at-9054-usd-93CH-4777005)
- [Establishment Labs Holdings (ESTA) Stock Price & Overview](https://stockanalysis.com/stocks/esta/)
