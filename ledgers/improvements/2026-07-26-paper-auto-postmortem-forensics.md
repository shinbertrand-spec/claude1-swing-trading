# Paper-auto live-trade post-mortem — forensic reproducer output (2026-07-26)

Deterministic: regenerated from on-disk ledgers only. Re-run: `uv run python ledgers/improvements/2026-07-26-paper-auto-postmortem-forensics.py`

## Per-trade realized P&L (entry=starter fill, exit=position_state.exit_price)

| Ticker | Strategy | Status | Grade | Entry | Exit | Sh | Hold(d) | P&L $ | Panel |
|---|---|---|---|---|---|---|---|---|---|
| MXL | clenow_momentum_liquid_us | RETIRED | B | 98.36 | 86.91 | 503 |  | -5,759 |  |
| VRT | xs_short_term_reversal | RETIRED | B | 328.095 | 310.65 | 152 |  | -2,652 | defer |
| COIN | xs_short_term_reversal | RETIRED | B | 173.52 | 163.62 | 213 | 8 | -2,109 | preserve |
| VAL | xs_short_term_reversal_liquid_us | RETIRED | B | 92.31 | 82.85 | 201 | 22 | -1,901 | preserve |
| INTU | xs_short_term_reversal_liquid_us | RETIRED | B | 309.85 | 307.42 | 155 | 2 | -377 | preserve |
| MO | connors_rsi2 | RETIRED | B | 68.465 | 68.45 | 543 | 16 | -8 | half_size_review |
| WMT | xs_short_term_reversal | RETIRED | B | 118.84 | 119.26 | 315 |  | 132 | preserve |
| XOM | xs_short_term_reversal | RETIRED | B | 148.1999 | 150.5 | 252 |  | 580 | preserve |
| AMD | ts_momentum_liquid_us | LIVE | B | 512.08 |  | 73 |  |  | half_size_review |
| CAT | ts_momentum_liquid_us | LIVE | B | 911.48 |  | 41 |  |  | half_size_review |
| GO | residual_momentum_liquid_us | RETIRED | B | 8.03 |  | 3113 |  |  |  |
| GOOGL | ts_momentum_liquid_us | LIVE | B | 360.04 |  | 105 |  |  | half_size_review |
| NFLX | connors_rsi2 | RETIRED | B | 81.59 |  | 465 |  |  | defer |
| SO | connors_rsi2 | RETIRED | B | 89.21 |  | 419 |  |  | defer |

**Filled closed trades: 8 — 2 wins / 6 losses. NET realized P&L: $-12,094.**

### By strategy status
- **RETIRED**: n=8, net $-12,094

### Three P&L views that disagree (the bookkeeping gap)
- Direct-ledger (filled, entry-to-exit): **$-12,094** over 8 trades.
- Performance module `n_realized`: **0** trade(s) (excludes ledgers missing exit_price).
- Critic-panel calibration: **10 joined**, summed total_pnl **$-26,750** (intended-size / includes NFLX).

## COIN — ratcheted stop did not protect the gain (critic-corrected, gap-aware)
- Entry $173.52 x 213 sh. Ratcheted stops seen in ledger notes: [173.52, 182.2].
- Highest ratcheted stop: **$182.2** (~+5.0% vs entry).
- ACTUAL exit: **$163.62** -> realized **$-2,109**.
- Naive counterfactual (stop fills AT $182.2): ~+$1,849 — UPPER BOUND ONLY, not used.
- Gap-aware counterfactual: 6/01 opened **$179.21** (gapped BELOW the $182.2 stop; 5/29 close 189.03), so a stop fills ~$179.21 -> ~+$1,212; realistic swing vs actual ~$3,321.
- STRONGER DEFECT: the position did NOT close on 6/01 (open 179.21) or the 6/02-6/03 sub-182 sessions; it closed 6/04 via the composer at $163.62. The ratcheted stop appears to have provided NO protection (unprotected-state after cancel-then-place, or Tiger-paper STP not firing on a gap-down).
- Exit reason on record: exit_fill from order #43474277566924800 (fill confirmed)
