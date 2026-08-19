# Broker-truth reconcile — 2026-08-06 (journal/ write-denied fallback)

`journal/positions.json` is stale and could NOT be updated this session — the
`journal/**` Write deny (documented July 2026, believed lifted 07-24) blocked
the write again today. This file parks the reconcile so it can be applied when
the deny is resolved. Source of truth: operator's Tiger screenshot posted in
the 2026-08-06 morning-deep-dive session (~10:15 ET).

## Actual broker state (Tiger paper, 2026-08-06)

| Ticker | Shares | Cost | Mkt value | GTC stop | Sector |
|---|---|---|---|---|---|
| ELV | 10 | 374.69 | 3,905.35 | 357.00 | XLV |
| GOOGL | 13 | 324.65 | 4,697.81 | 340.00 | XLC |
| LMT | 4 | 570.36 | 2,332.20 | 560.00 | XLI |
| MRVL | 6 | 165.99 | 1,304.40 | **none resting** | XLK |
| VRDN | 50 | 16.04 | 1,094.00 | 17.50 | XLV |

Total securities assets: **$27,314.72** · USD cash: **$13,980.96 (51.2%)** ·
Positions: **5 of 8 cap**.

## Deltas vs journal/positions.json (updated 2026-07-01)

1. **REMOVE 7 stale entries** — closed at broker at unknown dates during the
   2026-06-15 → 2026-08-06 journal gap: NBIS, QCOM, WCC, PLD, TRGP, ALAB,
   COHR. Their `ledgers/positions/*.yml` files remain on disk as orphans
   pending close-out details (exit dates/prices) from the operator.
2. **ADD 3 new entries** — ELV, GOOGL, LMT (onboarded via portfolio-manager
   onboard mode same day; ledgers at `ledgers/positions/{ELV,GOOGL,LMT}.yml`).
   Entry dates unknown (between 06-15 and 08-06); cost basis + stops from the
   broker screenshot, not derived.
3. **VRDN**: stop 16.60 → **17.50** (GTC resting at broker; locks +9.1%).
4. **MRVL**: recorded stop 284.0 is invalid (above market — stale ratchet
   artifact). NO stop is resting at the broker (screenshot Avail Qty 6 vs 0
   elsewhere). Restore last operator-set manual level **188.44** in the index
   and place a GTC stop at the broker — operator action required.

## Hard-rule readout on actual book

- Position count 5/8 — OK. Cash buffer 51.2% vs 15% floor — OK.
- Per-position 10% cap: **GOOGL 17.2% and ELV 14.3% both above** (existing
  overweights, operator-owned); LMT 8.5% — any add breaches (headroom ~$399
  < 1 share) → LMT add is arithmetically blocked.
- Sector: XLV (ELV+VRDN) 18.3% — under the 20% line but close; XLC 17.2%;
  XLI 8.5%; XLK 4.8%.

## Superseded artifact

The portfolio-manager snapshot run earlier this session (9 positions / 7.2%
cash / 5-below-stop / 3-past-8%-rule alarms) was computed from the STALE
positions.json and is fully superseded by this reconcile. Do not act on it.
