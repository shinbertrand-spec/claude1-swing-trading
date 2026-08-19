# Morning Candidate Scan — 2026-07-16

Headless run via `/morning-scan-telegram` (risk-and-compliance Mode 1).

> **Note on location:** intended path `journal/candidates/2026-07-16.md` is still blocked
> by the `journal/**` Write deny in settings.local.json (active since 2026-07-06 — 9th
> straight blocked weekday). Written here per the established workaround; summary pushed to
> Telegram via `scripts/send-to-telegram.ps1` directly (delivered, message_id 782).

## Gate 0 — Regime circuit breaker

```
uv run python -m tools.regime_check SPY
→ broad_market_stage_class: "stage_2_weakening" (SPY trend_template_passes=6/7)
→ circuit_breaker_stage_4: false
```

Not Stage 4. Scan proceeded.

## PORTFOLIO-LEVEL BLOCKER — flagged before the scan

**Open positions = 9. Hard cap (CLAUDE.md § Position Sizing & Capital) = 8 concurrent.** This is *already* breached — independent of the 2026-06-22 sector/cluster carve-out, which only downgrades the sector (20%) and cluster (~30%) caps to warnings on the discretionary track. The 8-position cap is explicitly listed as one of the caps that **survives** the carve-out ("What this does NOT change"). Consequence: **any candidate below that clears every mechanical rule will still fail Gate 4 in Mode 2 verification on the position-count rule alone**, unless a position is trimmed first (post-trade count must be ≤8, so you need to be at ≤7 before adding one). Placing any of these today requires closing a position first.

Secondary flag: **5 of 9 open positions (MRVL, NBIS, QCOM, ALAB, COHR) are already XLK.** That concentration is WARNING-only per the 2026-06-22 AI-concentration carve-out — not a block — but it should weigh which 2 of 3 candidates get the deep-dive if the correlated-gap exposure is to stop growing.

## Ranked candidates (2026-07-16)

| # | Ticker | Sub-theme | Current price | Why now (1 line) | Key risk | Next earnings | Trend template | Stage |
|---|--------|-----------|---------------|------------------|----------|---------------|----------------|-------|
| 1 | GS | Capital markets / financials (XLF — true diversifier, zero overlap with current book) | $1,114.54 | Record Q2 print 2026-07-14: EPS $20.98 vs $14.47 est., record equities+FICC revenue, +8% gap to fresh ATH | Chasing an already-extended post-earnings pop, only 3.5% off new high; no fresh catalyst before next print (63 trading days out) | 2026-10-13 (63 trading days) | 7/8 (fails c8 RS only) | 2 |
| 2 | OKTA | Identity/AI-security software (XLK — adds to already-crowded sector) | $151.07 | 4 analyst PT hikes this week (Scotiabank/BTIG/Needham/KeyBanc) on agentic-AI identity-security demand; +21% in 7 sessions | Extended move (+21%/7d), RS rating unresolved by tool (c8 fail); pushes XLK to 6 of 10 positions if entered | 2026-08-26 (29 trading days) | 7/8 (fails c8 RS only) | 2 |
| 3 | HSIC | Dental/medical distribution (XLV — mild overlap with VRDN) | $88.98 | BTIG upgrade to Buy (PT $100, 2026-06-11) + 4.1% YoY US-dental organic growth + new GoTu Technology workforce partnership | Catalyst is 4-5 weeks stale, no fresh 2-6wk trigger; earnings 2026-08-04 is only 13 trading days out — a normal swing hold runs straight into the earnings blackout unless earnings becomes the explicit thesis | 2026-08-04 (13 trading days) | 7/8 (fails c8 RS only) | 2 |

### Compact pass/fail lines

**#1 GS**
- Stage 2 stock (trend_template ≥ 6/8 + criterion 6): PASS — 7/8, c6=true
- Broad market not Stage 4: PASS — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: PASS — 63 trading days
- Market cap > $2B: PASS — $328.8B
- Avg daily volume > 500K: PASS — 1.88M sh/day
- Sector qualifies for long (XLF): PASS — sector_trend_template_passes=5/7, qualifies_for_long=true

**#2 OKTA**
- Stage 2 stock (trend_template ≥ 6/8 + criterion 6): PASS — 7/8, c6=true
- Broad market not Stage 4: PASS — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: PASS — 29 trading days
- Market cap > $2B: PASS — $26.3B
- Avg daily volume > 500K: PASS — 3.21M sh/day
- Sector qualifies for long (XLK): PASS — sector_trend_template_passes=6/7, qualifies_for_long=true

**#3 HSIC**
- Stage 2 stock (trend_template ≥ 6/8 + criterion 6): PASS — 7/8, c6=true
- Broad market not Stage 4: PASS — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: PASS (marginal — 13 vs 10-day threshold, blackout not yet triggered but tight)
- Market cap > $2B: PASS — $10.1B
- Avg daily volume > 500K: PASS — 1.32M sh/day
- Sector qualifies for long (XLV): PASS — sector_trend_template_passes=6/7, qualifies_for_long=true

Also screened and **rejected before inclusion** (fail the 6/8-with-c6 threshold, not padded in): DDOG (6/8, c2 fails — SMA150 < SMA200, longer-term trend not confirmed), NET (6/8, same c2 failure).

Note: none of the three clear criterion 8 (RS rating ≥ 70) — the tool returned `rs_status: "unknown"` for all six tickers screened because no live RS-rating feed was supplied. Per the Mode 1 pass rule this doesn't block (threshold is ≥6/8 + c6), but it means the RS/leadership confirmation still needs a real IBD-style feed at deep-dive.

**Pick 2 of 3 to deep-dive. Trades only enter the pipeline once `trade-researcher` builds their ledger and risk-and-compliance verifies it via Mode 2.**

Given the position-count blocker above, whichever 2 are picked, resolve the 9-vs-8 cap first — otherwise Mode 2 Gate 4 will reject on arrival regardless of setup quality.

## Sources

- [OKTA Stock Climbs As Analysts Chase AI Security Boom](https://stockstotrade.com/news/okta-inc-okta-news-2026_07_14/)
- [Okta (NASDAQ:OKTA) Shares Up 6.8% - Here's What Happened](https://www.marketbeat.com/instant-alerts/okta-nasdaqokta-shares-up-68-heres-what-happened-2026-07-14/)
- [CrowdStrike Surges 5%, Palo Alto and Okta Gain 4% as Cybersecurity Stocks Rally on Analyst Upgrades](https://247wallst.com/investing/2026/07/06/crowdstrike-surges-5-palo-alto-and-okta-gain-4-as-cybersecurity-stocks-rally-on-analyst-upgrades/)
- [Goldman Sachs posts Q2 2026 earnings beat on record equities trading](https://qz.com/goldman-sachs-q2-2026-earnings-record-equities-trading-071426)
- [Goldman Sachs Stock Bags Record After Historic Q2](https://www.schaeffersresearch.com/content/news/2026/07/14/goldman-sachs-stock-bags-record-after-historic-q2)
- [Goldman Q2 Earnings Beat on Solid Trading & IB Revenues, Shares Rise](https://finance.yahoo.com/markets/stocks/articles/goldman-q2-earnings-beat-solid-133500401.html)
- [Where The Buying Ran Strongest: 16 Mid Cap Stocks At 52-Week Highs](https://www.trefis.com/stock/spy/articles-v3/607297/where-the-buying-ran-strongest-16-mid-cap-stocks-at-52-week-highs/2026-07-15)
- [Henry Schein, Inc. (HSIC) Stock Price, News, Quote & History - Yahoo Finance](https://finance.yahoo.com/quote/HSIC/)
