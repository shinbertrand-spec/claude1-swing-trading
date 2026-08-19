# Morning Candidate Scan — 2026-07-15

> **Delivery note:** this report lives here instead of
> `journal/candidates/2026-07-15.md` because the `Write(journal/**)` deny in
> `.claude/settings.local.json` is still active (8th consecutive blocked day —
> see `2026-07-15-morning-scan-blocked.md`). The Telegram summary was pushed
> out-of-band via `scripts/send-to-telegram.ps1`. **/morning-deep-dive should
> read THIS file today.**

## Mode 1 — Morning Candidate Scan, 2026-07-15

### Step 0 — Regime circuit breaker (mandatory first)

```
uv run python -m tools.regime_check SPY
```

`broad_market.stage_class = "stage_2_weakening"` (6/7 trend-template passes, regime_multiplier 0.75). **Not Stage 4** — circuit breaker does not trip. Proceeding to scan (regime multiplier 0.75 means size at 75% of full per `position_sizer` when this reaches Mode 2).

### Sector-ETF pre-screen (Level 2 of the regime playbook, run before naming stocks)

| Sector ETF | 7-max passes | Class | Qualifies for long |
|---|---|---|---|
| XLE (Energy) | 7/7 | stage_2_confirmed | yes |
| XLK (Tech) | 7/7 | stage_2_confirmed | yes |
| SMH (Semis) | 7/7 | stage_2_confirmed | yes |
| XOP (Oil E&P) | 7/7 | stage_2_confirmed | yes |
| XLI, XLU, XSD | 6/7 | stage_2_weakening | yes |
| XLF, XLV, XLB, XLP | 5/7 | stage_2_weakening | yes |
| XLY, XLC | 1-2/7 | stage_4 | **no** |

Screened 12 individual names against the qualifying sectors (Energy: EQT, AR, BKR, OXY, EOG; Semis: MU, AVGO, AMD; Utilities/nuclear: CEG, TLN, D, BWXT). Only 5 cleared the ≥6/8 + criterion-6 stock-level bar (MU, AVGO, AMD, OXY, EOG); D/TLN/BKR/CEG/BWXT/EQT/AR failed criterion 6 or the ≥6/8 floor outright (CEG in particular: 0/8, stage_4 — do not chase the nuclear-AI headline into a broken chart).

MU and AMD were dropped from the shortlist despite 7/8 passes: both are extremely extended (MU +819% above 52w low / AMD +287% above 52w low, AMD only 6% below 52w high) — textbook chase risk, not an entry. AVGO is the cleaner semis name (pulled back slightly below its 50-day SMA, not blown-off).

### Ranked candidates

| # | Ticker | Sub-theme | Current price | Why now (1 line) | Key risk | Next earnings | Trend template | Stage |
|---|--------|-----------|---------------|------------------|----------|---------------|----------------|-------|
| 1 | EOG | Energy — oil E&P | $137.66 | Iran-US strike escalation pushed oil +4.2% (Jul 13); $4.7B FCF, 100% of it returned to shareholders; cleanest chart of the three (no MA violations) | Macro/geopolitical-driven catalyst reverses fast on any de-escalation headline; earnings Aug 5 (16 trading days — inside normal hold window) | 2026-08-05 (BMO) | 7/8 | Stage 2 |
| 2 | OXY | Energy — oil E&P | $54.08 | 4 analyst upgrades since March (Evercore/Barclays/Goldman/Wells Fargo+Piper) on deleveraging thesis + Bandit Gulf-of-America discovery | Price is BELOW 50-day SMA right now (criterion 5 fails) — this is a pullback, not a confirmed breakout; still carries balance-sheet leverage overhang; **same sector as EOG (XLE) and existing TRGP position — 3 XLE names would be one correlated bet, not three** | 2026-08-06 | 6/8 | Stage 2 |
| 3 | AVGO | Semis / AI-capex (XLK) | $396.94 | Apple $30B US-made AI-chip supply deal + Meta AI chip production start Sep 2026 — specific contract catalyst, not vague tailwind | Also below 50-day SMA (pullback, unconfirmed); **this would be the operator's 6th XLK/AI-momentum name (MRVL, NBIS, QCOM, ALAB, COHR already held) — heaviest correlation flag on this list even before Gate 4 sector-cap math runs** | 2026-09-04 | 6/8 | Stage 2 |

### Per-candidate pass/fail lines

**EOG**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — trend_template_passes=7/8, c6=true
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: **pass** — trading_days_to_earnings=15
- Market cap > $2B: **pass** — $73.36B (finviz)
- Avg daily volume > 500K: **pass** — 3.82M shares/day (finviz)
- Sector qualifies for long: **pass** — XLE 7/7, stage_2_confirmed

**OXY**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — trend_template_passes=6/8, c6=true (c5 fails — below 50sma)
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: **pass** — trading_days_to_earnings=16
- Market cap > $2B: **pass** — $53.78B (finviz)
- Avg daily volume > 500K: **pass** — 11.29M shares/day (finviz)
- Sector qualifies for long: **pass** — XLE 7/7, stage_2_confirmed

**AVGO**
- Stage 2 stock (trend_template ≥6/8 + criterion 6): **pass** — trend_template_passes=6/8, c6=true (c5 fails — below 50sma)
- Broad market not Stage 4: **pass** — SPY stage_2_weakening, 6/7
- No earnings within 10 trading days: **pass** — trading_days_to_earnings=37
- Market cap > $2B: **pass** — $1,893.4B (finviz)
- Avg daily volume > 500K: **pass** — 26.30M shares/day (finviz)
- Sector qualifies for long: **pass** — XLK 7/7, stage_2_confirmed

All three mechanically clear every Mode-1 gate. None are padding — MU, AMD, D, TLN, BKR, CEG, BWXT, EQT, AR were all tested and rejected before these three were kept (see sector pre-screen above).

**Adversarial note (unprompted, Gate-5-style):** if you pick EOG + OXY, you're stacking a third XLE name on top of the existing TRGP position — that's a correlation bet on oil, not three independent trades, even though each clears its own hard rules individually. If you pick AVGO, you're adding a sixth XLK/AI-momentum name to a book that already has five. Either combination should get the cluster-concentration math run explicitly in Mode 2 (`tools.cluster_concentration`) before sizing — don't let three individually-clean passes hide one concentrated bet.

**Pick 2 of 3 to deep-dive. Trades only enter the pipeline once `trade-researcher` builds their ledger and verification runs via Mode 2.**

Sources:
- [5 Best Energy Stocks To Buy And Invest In 2026 | July Edition — Forbes](https://www.forbes.com/sites/investor-hub/article/5-energy-stocks-to-buy-in-2026/)
- [Top 3 Energy Stocks That Could Blast Off In July — Benzinga](https://www.benzinga.com/news/26/07/60210232/top-3-energy-stocks-that-could-blast-off-in-july)
- [AI Semiconductor Stocks July 2026: NVDA AMD AVGO Market Analysis & Investment Strategy — Intellectia](https://intellectia.ai/blog/ai-semiconductor-stocks-july-2026-market-rotation-2026-07-12)
- [3 Nuclear Power Stocks Set to Flourish in 2026 on AI Data Center Boom — Nasdaq](https://www.nasdaq.com/articles/3-nuclear-power-stocks-set-flourish-2026-ai-data-center-boom)
- finviz quote panels: EOG, OXY, AVGO, MU, AMD
