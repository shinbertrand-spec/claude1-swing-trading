---
description: Morning swing-trading routine — candidate scan, deep-dive, compliance check, trade approval (9:30–10:30 AM ET)
---

# Morning Trading Routine

It is morning ET on a US market day. Run the morning routine end-to-end.

## Step 1 — State of the world

Read in parallel:
- `CLAUDE.md` (the framework)
- The most recent file in `journal/` (current portfolio state, open positions, watchlist)
- `price_check_may14.jsx` IF it exists (user-provided portfolio snapshot — verify any data inside against live values before trusting)

Confirm: market is open, portfolio state is current, no overnight stop triggers on open positions.

## Step 2 — Candidate scan (9:45 AM ET)

Invoke `risk-and-compliance` in **candidate-scan mode** with the prompt:

> Morning candidate scan for today's date. Suggest 3 swing-trade candidates that pass ALL framework hard rules (see `CLAUDE.md`). Return the standard candidate-scan output schema. If you can only find 2 clean candidates, return 2 and say so — do not pad.

Present the 3 (or fewer) candidates cleanly. **Wait for the user to pick up to 2** (or pass on all of them).

## Step 3 — Deep-dive on picks (10:00 AM ET)

For EACH ticker the user picks:

1. Invoke `trade-researcher` for the full 6-section deep-dive.
2. Based on the researcher's data, propose specific trade parameters that comply with the framework. Show the math:
   - Entry near the ask (limit within 0.2% of ask = at-market entry on a triggered setup, OR a pullback limit if that's the strategy — flag clearly which)
   - Stop ≤ 8% from entry, placed at credible technical support
   - Target ≥ 2× the entry-to-stop distance (R:R ≥ 1:2)
   - Position size: 1 share = ___% of portfolio (must be ≤ 5%); if bear regime, halve
3. Invoke `risk-and-compliance` in **verification mode** with the researcher's report + the proposed trade.

## Step 4 — Verdict and trade placement

Present each verdict clearly:

- **APPROVE** → ask the user to confirm; on confirmation, log the limit order to today's journal (ticker, qty, limit price, stop, target, thesis)
- **APPROVE-WITH-CONDITIONS** → present the conditions; user decides whether to proceed, modify, or pass
- **BLOCK** → don't trade; add to watchlist with the specific re-evaluation trigger ("re-enter when X happens")

## Step 5 — Update journal

Update today's journal entry (`journal/YYYY-MM-DD.md` or `journal/Trading.md`) with:

- Market context (SPY, VIX, sector leaders/laggards, macro events)
- Pre-trade portfolio snapshot
- The 3 candidates and which the user picked
- Deep-dive summaries (1 paragraph each)
- Each verdict and the user's decision
- Trades placed (limit price, size, thesis, stop, target)
- Watchlist additions / changes

## Guardrails

- Never recommend or place a trade that fails any hard framework rule.
- Always read `CLAUDE.md` before making framework judgments — do not rely on memory of the rules.
- If the candidate-scan returns fewer than 2 names, that's fine — no-trade days are allowed and required by the discipline rules.
- The user has the final say on every trade — never auto-execute.
