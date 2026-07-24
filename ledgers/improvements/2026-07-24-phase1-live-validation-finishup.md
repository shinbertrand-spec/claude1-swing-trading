# Phase 1 — live-validation finish-up + roster sprint (2026-07-24)

- asof: 2026-07-24 (same-day follow-on to the Phase-0 unblock)
- plan: `plans/2026-07-24-second-stream-diversification-plan.md` Phase 1
- commits: `c9b17c7` (1a) + sprint artifacts; full suite **2074 green**

## 1a — close-writer fix, backfill, B1 wiring, cadence

- **Structured exits**: `_apply_realized_close` writes
  `position_state.exit_price/exit_date/exit_reason`; expiry writes
  `unfilled: true`; flip-back clears all four; schema declares them.
- **Backfill applied** (`scripts/backfill_exit_fields.py`): INTU $307.42
  (05-28) · COIN $163.62 (06-05) · MO $68.45 (06-18) · VAL $82.85 (06-19)
  from their own close notes; AMD/CAT/GOOGL flagged `unfilled` (phantom-fill
  class — seeded limits were masquerading as fills, including in the first
  B2 drag table).
- **Honest headline surfaced**: after cleaning, **ts_momentum_liquid_us has
  ZERO live fills** — all three June entries expired unfilled. The live
  setup's validation clock has NOT started. Regenerated B3 report: 10
  trades reconstructed, sleeve PSR 19.1% at T=27d (weak-sample by design).
- **B1 wired**: gate chain is the final sizing/veto word in
  `pipeline.place_candidate` (fail-closed; per-candidate JSONL log; breaker
  persists on real runs only; Kelly priors from the roster row —
  win_rate_prior 0.542 / payoff_ratio_prior 1.54).
- **Cadence**: `/auto-paper-reconcile` Step 1c runs the drag update nightly.

## 1b — roster sprint: 0 of 3 pass (pre-registered outcome)

| candidate | grid gate | DSR | net agg Sharpe | net windows | corr vs ts_mom | verdict |
|---|---|---|---|---|---|---|
| connors_rsi2 | FAIL | 0.846 | 0.28 | 0/6 | 0.22 | FAIL |
| xs_low_volatility | FAIL | 0.341 | -0.03 | 0/2 | 0.05 | FAIL |
| event_insider_buying | FAIL | n/a (1-combo grid) | 0.00 | 0/4 | 0.34 | FAIL |

Reading, per the plan's pre-registration: **this is the thesis-confirmation
branch** — the generic long-only-equity family is mined out at honest costs.
The connors stale-verdict question is now closed with evidence (its park was
justified; 0/6 windows net). The measured correlations are low, but low
correlation without edge diversifies nothing. **This is NOT a loosening
trigger**: Phase 2 (the options-VRP second stream, separate venture) carries
the diversification weight, and Phase 3 (insider scout, ~08-20) remains the
one new-family equity candidate.

- Grid detail: `journal/backtest/2026-07-24-sprint-<setup>.md`
- Trial registry re-derived post-sprint (`ledgers/trials.yml`, N unchanged —
  grids were already counted; as_of refreshed).

## Operator follow-ups

1. Phase 2 go/no-go: stand up the claude2 VRP paper loop (2–4 wks).
2. Optional roster hygiene: connors_rsi2's row can now cite this re-run as
   its definitive retirement evidence (operator edit, not automated).
3. Watch tonight's first Monitor/Reconcile passes + tomorrow's entry scan —
   the first REAL ts_momentum fill starts the validation clock at last.
