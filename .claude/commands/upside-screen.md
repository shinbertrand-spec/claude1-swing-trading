---
description: /upside-screen — rank a themed universe by the Clenow momentum "winner DNA" (annualised regression slope × R²) to surface the strongest-momentum candidates for the human-discretionary book. Default - the broad AI universe (~132). Flags - "/upside-screen --pure" (tight 41-name AI list), "/upside-screen --tickers NVDA MRVL NBIS", "/upside-screen --top 15". Triage only - it places nothing and every surfaced name still goes through trade-researcher → trade-skeptic → risk-and-compliance.
---

# /upside-screen — momentum idea generation (discretionary track)

Run the repeatable upside screen and relay the ranked shortlist. This finds the
*profile* the realised winners (NBIS / MRVL / QCOM) shared — a strong, smooth,
established uptrend in a liquid name — NOT guaranteed winners. It is **triage**,
human-discretionary track only.

## What to do

1. Run the tool, passing through any `$ARGUMENTS` verbatim (e.g. `--pure`,
   `--tickers ...`, `--top N`, `--lookback N`, `--adv N`):
   ```bash
   uv run python -m tools.upside_screen $ARGUMENTS
   ```
   Default (no args) screens the broad AI universe (~132 names) — that fetch can
   take a couple of minutes. `--pure` (41 names) is faster.
2. Relay the ranked table. Lead with the **new** names (not tagged `[held]` /
   `[watch]`) — those are the fresh ideas — then note where the operator's
   existing holdings rank (a held name ranking low is a "laggard" signal worth
   surfacing).
3. Call out the **flags**: `gap%` = a >15% single-day move in the window (the
   name may already be extended / late); `<200d` = below the 200-day (weaker
   trend); `RSI>80` = overbought. A high score with heavy flags is "hot but
   risky," not a clean entry.

## Guardrails

- **Triage, not a verdict.** Passing the screen is necessary, not sufficient.
  Never imply a buy. Anything worth pursuing goes through the full
  `trade-researcher → trade-skeptic → risk-and-compliance` pipeline next.
- **Honesty about hit rate.** Most names it surfaces will NOT 3×. The screen
  finds candidates; position sizing + stops are what make a low-hit-rate /
  fat-winner approach profitable. Don't oversell it.
- **Discretionary track only.** This is idea generation for the cash book; it
  does not feed the automated paper-auto pipeline and places nothing.
- The momentum score is the project's own backtested Clenow regression score
  (`tools.upside_screen` reuses `clenow_momentum._annualised_log_slope_r2`) — do
  not hand-recompute or re-rank; the tool is the source of truth.
