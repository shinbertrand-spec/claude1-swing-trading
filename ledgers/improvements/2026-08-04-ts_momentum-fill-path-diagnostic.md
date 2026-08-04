# ts_momentum fill-path diagnostic — the real pre-condition #0

**Pilot artifact, 2026-08-04. Suggest-only.** Diagnoses the live-validation scope's stated
critical-path blocker (`2026-07-25-ts_momentum-live-validation-SCOPE.md`, pre-condition #0:
"ts_momentum has never filled a single order"). No code changed — this is the diagnosis +
proposed fixes + a verification plan. Any change to the live order path is operator /
wall-lift work.

## TL;DR — the fix isn't broken, it's UNTESTED

The marketable-limit price fix (`tools/auto_paper/entry_pricing.py`, landed 2026-06-16) has
**never been exercised on a live rebalance.** The only post-fix monthly ts_momentum
rebalance (~2026-07-15) fell inside the 2026-07-01 → 07-24 headless-scan outage
(journal-write-deny), so no scan ran, no candidate was placed, and nothing could fill. The
three "unfilled" June entries predate the fix. **First real test of the fix is the next
monthly rebalance (~mid-August).** This reframes pre-condition #0: it's not "diagnose a
broken fill mechanism," it's "the mechanism has had zero live at-bats — get it one, watched."

## Confirmed facts (evidence)

| Fact | Evidence |
|---|---|
| ts_momentum has 0 fills, ever | `journal/paper-auto/positions.json`: AMD/CAT/GOOGL all `stage: closed_unfilled`, `setup_type: ts_momentum_liquid_us`, entry 2026-06-15 |
| Those 3 predate the price fix | AMD ledger `limit_price_placed: 512.08` = pivot 511.57 × **1.001** (old resting-limit), NOT ×1.03; note: "expired unfilled on 2026-06-16: TIF=DAY" |
| Price fix routes momentum to a marketable limit | `entry_pricing.py`: `ts_momentum ∈ MOMENTUM_KINDS` → `pivot × (1 + 0.03)`, caps chase at 3% |
| Orders are DAY TIF | `tiger.py:402` `limit_order(...)` — no TIF arg → SDK default DAY |
| No quant picks placed since 2026-07-01 | `ledgers/candidates/` has no dated dir after `2026-07-01` |
| Scans were down 07-01 → 07-24 | memory `project_journal_write_deny_breaks_headless` — deny broke headless scans for 14 trading days, "DENY APPEARS LIFTED 2026-07-24" |
| ts_momentum rebalance is monthly | `ts_momentum_liquid_us.yml` `rebalance_period_days: 21`; scanner only emits candidates on schedule-aligned days (`quant_scanner.scan_setup` Bug-2 anchor comment) |

## Root cause chain — "why no fill since the 06-16 fix"

1. **06-15:** 3 ts_momentum names placed at the *old* pivot×1.001 resting limit → gapped up
   past it → DAY-expired unfilled. (The bug `entry_pricing.py` was written to fix.)
2. **06-16:** price fix lands (marketable limit pivot×1.03). Correct in code.
3. **~07-15:** next monthly rebalance — **falls inside the 07-01→07-24 scan outage.** No
   scan → no candidate → no placement. The fix gets no at-bat.
4. **07-24 → today:** deny lifted, but no scan day since has been a ts_momentum rebalance day
   (no post-07-01 candidate dirs). Next rebalance ≈ one 21-day step past 07-15 → **~08-13.**

Net: the fill fix is **plausibly correct but empirically unproven** — the validation clock
still reads zero not because the fix failed but because its one window was eaten by an
unrelated outage.

## Residual fill-rate risks — these WILL bite once it fires

Even with the price fix working, three structural issues threaten the fill rate on the next
rebalance and should be instrumented, not assumed away:

- **R1 — DAY TIF + 9:35 placement vs the open print.** The backtest fills at the *open*;
  the live order is a DAY marketable limit placed ~9:35 (5 min post-open by the cron). If a
  momentum name runs past pivot×1.03 in those 5 minutes it won't fill — a systematic,
  one-directional miss on exactly the strongest names. A market-on-open (OPG/MOO) or
  at-open-limit TIF would track the certified fill far better than a 9:35 DAY limit.
- **R2 — the 3% chase cap skips the best names.** `MOMENTUM_ENTRY_BUFFER = 0.03` skips any
  name gapping >3% at the open. For a trend book on rebalance day, >3% overnight gaps are
  common — so the cap can silently drop the highest-momentum candidates, biasing the live
  book away from the backtest's fills. Instrument the skip rate; if high, the cap needs
  revisiting (the backtest filled these at the open regardless).
- **R3 — the monthly clock vs the 30-trade criterion.** ~12 rebalances/yr × a few names =
  the scope's "≥30 closed trades / ≥6 months" bar is *slow* to reach for a monthly
  strategy, and any missed rebalance (like 07-15) pushes it out a full month. The
  validation horizon should be recomputed against the true fill cadence, not assumed.

## Proposed actions (staged, testable — suggest-only)

1. **Watch the ~08-13 rebalance end-to-end** (operator / next-scan): confirm the scan fires
   on the rebalance day, emits ts_momentum candidates, `place_candidate` places a
   pivot×1.03 marketable limit, and record fill vs no-fill + the skip list. This is the
   first real at-bat — the single most informative event.
2. **Instrument fill-rate + skip-rate** (code, wall-lift): log, per rebalance, (candidates,
   placed, filled, skipped-by-3%-cap, expired-DAY). Without this the fill mechanism stays a
   black box. Cheap; belongs in `pipeline.place_candidate`.
3. **If the 9:35 DAY limit under-fills (R1), test OPG/MOO or at-open TIF** (code, wall-lift):
   a limit-on-open tracks the backtest's open fill; measure fill-rate delta before adopting.
4. **Recompute the validation-window horizon (R3)** against the realized monthly cadence and
   fold into the scope's success criteria (≥N trades will take ~N/(names-per-rebalance)
   months — likely >6mo).

## Effect on the live-validation timeline

Pre-condition #0 is **not cleared and cannot be** until the fix takes at least one live
at-bat. The scope's Phase 0 ("paper dress rehearsal on the exact live path, ≥1 full monthly
rebalance") is exactly the right gate — but note it needs a rebalance that *actually fires*,
which the 07-15 outage denied. **Do not advance toward live capital until a ts_momentum
rebalance fills cleanly on the paper path.** The ~08-13 window is the earliest that can happen.

Suggest-only; nothing applied. Wall intact.
