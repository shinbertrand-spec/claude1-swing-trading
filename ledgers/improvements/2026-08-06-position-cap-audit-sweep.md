# Handoff B §3.3 — constant-gross cap sweep (2026-08-06)

Design frozen pre-run: `2026-08-06-position-cap-audit-prereg.md` (commit f0a3c6d). Binding psim net-of-cost harness, LIVE fills. Constant gross: N x pct = 40% every arm.

## connors_rsi2 (pinned {'entry_threshold': 15, 'cooldown_days': 3})

| N | pct | OOS-agg S | OOS MDD% | OOS n | windows | FULL S | gross(inv) | gross(peak) | at-cap % | cap-rejected (est) |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 0.0500 | 0.27 | 7.9 | 1329 | 1/6 | 0.22 | 25.8% | 41.8% | 20.7% | 1415 |
| 12 | 0.0333 | 0.29 | 8.1 | 1673 | 0/6 | 0.23 | 21.5% | 41.5% | 12.3% | 921 |
| 16 | 0.0250 | 0.30 | 6.6 | 1916 | 0/6 | 0.20 | 18.3% | 41.3% | 7.2% | 580 |
| 20 | 0.0200 | 0.32 | 6.5 | 2074 | 0/6 | 0.23 | 15.7% | 41.2% | 4.5% | 362 |

## dual_ma_trend_following (pinned {'short_period': 20, 'long_period': 100})

| N | pct | OOS-agg S | OOS MDD% | OOS n | windows | FULL S | gross(inv) | gross(peak) | at-cap % | cap-rejected (est) |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 0.0500 | 0.50 | 11.1 | 293 | 1/6 | 0.67 | 39.9% | 45.6% | 84.5% | 2756 |
| 12 | 0.0333 | 0.69 | 12.0 | 440 | 2/6 | 0.77 | 39.6% | 47.1% | 79.8% | 2532 |
| 16 | 0.0250 | 0.68 | 11.3 | 586 | 2/6 | 0.83 | 39.3% | 46.6% | 75.2% | 2314 |
| 20 | 0.0200 | 0.72 | 11.4 | 730 | 2/6 | 0.88 | 38.9% | 45.9% | 72.3% | 2097 |

## ts_momentum_liquid_us (pinned {'lookback_days': 252})

| N | pct | OOS-agg S | OOS MDD% | OOS n | windows | FULL S | gross(inv) | gross(peak) | at-cap % | cap-rejected (est) |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 0.0500 | 1.23 | 16.9 | 525 | 4/6 | 1.37 | 29.4% | 48.8% | 29.1% | None |
| 12 | 0.0333 | 1.13 | 15.5 | 796 | 3/6 | 1.26 | 29.6% | 44.2% | 20.6% | None |
| 16 | 0.0250 | 1.08 | 14.2 | 1073 | 2/6 | 1.20 | 29.8% | 43.5% | 16.8% | None |
| 20 | 0.0200 | 1.05 | 13.6 | 1345 | 3/6 | 1.13 | 29.7% | 43.4% | 14.7% | 0 |

## Void-arm correction (pre-declared path, run once)

The connors_rsi2 N=16/N=20 arms above FAILED the prereg exposure control
(realized invested-gross 18.3%/15.7% vs the 25.8% baseline — occupancy grows
sublinearly with the cap for bursty reversion signals). Per prereg §B they were
VOID; the scaling was fixed ONCE (occupancy-corrected pct = design pct x
25.8/realized) and re-run. The JSON carries the corrected rows (source of
truth); corrected table:

| N | pct | OOS-agg S | OOS MDD% | OOS n | windows | gross(inv) | gross(peak) | at-cap % | cap-rejected (est) |
|---|---|---|---|---|---|---|---|---|---|
| 16 | 0.0352 | 0.30 | 9.3 | 1916 | 0/6 | 25.8% | 57.9% | 7.2% | 580 |
| 20 | 0.0329 | 0.32 | 10.5 | 2074 | 0/6 | 25.9% | 67.5% | 4.5% | 362 |

Control now holds across all connors arms (25.8/21.5/25.8/25.9%). Note the
corrected arms' PEAK gross rises (57.9%/67.5%) — burst-stacking under the
higher per-position pct; minimum cash in the worst burst ~32.5%, still above
the 15% floor.
