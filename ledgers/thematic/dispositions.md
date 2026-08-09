# Disposition ledger — ai-stack-analyst

The mechanism that makes this book improvable rather than merely active
(handoff §3.3). **Every rejected thesis is logged with its reasoning and a
forward-check date; at that date, record what actually happened.** If
rejected names systematically outperform held/candidate names, the research
has negative skill — and that is measurable here, from day one.

Rules:
- One row per disposition (rejected / invalidated / candidate-lapsed). Never
  delete a row; forward-check outcomes append to the row's Outcome column.
- Every row carries reference prices (ticker + SPY) at disposition date so
  the forward check is arithmetic, not memory.
- Forward checks: +6 months and +18 months from disposition.
- Scoring at forward check: `beat` = rejected name outperformed SPY by >10pp
  (a rejection that cost alpha), `neutral` = within ±10pp, `avoided` =
  underperformed by >10pp (the rejection earned its keep). A run of `beat`
  rows is the negative-skill signal.
- Candidate/held names carry their own reference prices in their thesis
  files; the skill comparison is rejected-cohort vs held-cohort vs SPY.

| # | Date | Ticker | Disposition | Reasoning (one line) | Ref px | SPY ref | Fwd check 1 (+6mo) | Outcome 1 | Fwd check 2 (+18mo) | Outcome 2 |
|---|------|--------|-------------|----------------------|--------|---------|--------------------|-----------|---------------------|-----------|
| 1 | 2026-08-09 | MU | rejected (late — priced) | Memory scarcity real (HBM sold out, 3:1 wafer cannibalization) but consensus since late-2025 and violently priced: ~$877 ≈ 20x TRAILING on peak-cycle EPS, fwd-P/E-5.6x mirage debate live, −39% drawdown already underway; the layer's easy money is gone and cycle-timing it is a systematic-track job, not a thesis-book job | $877.57 (08-07 close) | $773.26 (08-07 close) | 2027-02-09 | — | 2028-02-09 | — |

<!-- Row template:
| n | YYYY-MM-DD | TICK | rejected|invalidated|lapsed | one-line reasoning | $x.xx (date) | $x.xx (date) | YYYY-MM-DD | — | YYYY-MM-DD | — |
-->
