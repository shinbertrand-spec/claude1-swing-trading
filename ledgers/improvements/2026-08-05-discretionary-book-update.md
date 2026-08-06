# Discretionary book update — staged from broker screenshot (2026-08-05)

**Staged because the pilot wall denies `Write(journal/positions.json)` + `Write(ledgers/positions/**)`.**
Source: operator's Tiger Prime screenshot, 2026-08-05. Apply by either (a) lifting the two
deny pairs in `.claude/settings.local.json` and asking the session to apply, or (b) pasting
the JSON below over `journal/positions.json`'s `positions` array (keep the header keys) and
bumping `updated`.

## Broker ground truth

| ticker | sh | avg cost | last | mkt val | wt % | resting stop (GTC) | P&L shown |
|---|---|---|---|---|---|---|---|
| ELV | 10 | 374.69 | 382.82 | 3,828.20 | **13.9%** | 357.00 | +81.30 |
| GOOGL | 13 | 324.65 | 379.54 | 4,934.02 | **18.0%** | 340.00 | +713.55 |
| LMT | 4 | 570.36 | 586.02 | 2,344.06 | 8.5% | 560.00 | +62.64 |
| MRVL | 6 | 165.99 | 219.08 | 1,314.48 | 4.8% | **NONE** | +2,052.08* |
| VRDN | 50 | 16.04 | 21.03 | 1,051.50 | 3.8% | 17.50 | +272.25 |

Cash 13,980.96 · long value 13,472.26 · **equity 27,453.22** · cash buffer 50.9%.
\* MRVL's +2,052.08 exceeds the 6-share unrealized math (+318.54) — includes realized P&L
from the June trims (journal records +$938 realized on 2026-06-02 trim). Broker figure recorded as-shown.

## Delta vs journal (last updated 2026-07-01)

- **CLOSED since 07-01 (7):** NBIS, QCOM, WCC, PLD, TRGP, ALAB, COHR — exit prices/dates
  unknown from the screenshot; operator to supply if trade-log entries are wanted. Their
  `ledgers/positions/*.yml` should move to `closed` on apply.
- **NEW (3):** ELV, GOOGL, LMT — true entry dates unknown; onboarded with `entry_date`
  = 2026-08-05 (onboard date) + note.
- **UNCHANGED (2):** MRVL 6 sh, VRDN 50 sh — journal share counts match the broker.
  (Journal MRVL `stop: 284.0` is stale/wrong — above the market; broker shows NO resting
  MRVL stop.)

## Hard-rule check (CLAUDE.md, discretionary track)

| Rule | Status |
|---|---|
| Per-position 10% cap (**binding**, no carve-out) | **BREACH: GOOGL 18.0%, ELV 13.9%.** Trim to ≤10% (~$2,745): GOOGL −6 sh → 7, ELV −3 sh → 7. Or explicit operator override — on record. |
| 15% cash buffer | ✓ 50.9% |
| ≤8 positions | ✓ 5 |
| 20% sector cap (WARNING-only) | ✓ XLV 17.8% (ELV+VRDN) worst |
| Stops present | **MRVL HAS NO STOP** at +32% gain. Trail rule floor (+10% ⇒ stop at +5%) ⇒ ≥174.29; Minervini 8%-below-market ⇒ ~201.55. Place a GTC stop ~200. |
| Trail compliance | GOOGL +16.9% ⇒ stop floor 340.88; resting 340.00 is $0.88 short — nudge up. ELV/LMT <+5%, initial OK. VRDN 17.50 (+9.1% over cost) ✓ |
| Earnings proximity | VRDN earnings ~2026-08-06 (journal catalyst) — TOMORROW, holding through with stop 17.50. ELV/GOOGL/LMT next earnings unverified — refresh via news pass. |

## Staged `positions` array (paste over journal/positions.json `positions`; bump `updated`)

```json
[
  {
    "ticker": "ELV",
    "ledger_path": "ledgers/positions/ELV.yml",
    "entry_date": "2026-08-05",
    "entry_price": 374.69,
    "shares": 10,
    "stop": 357.0,
    "target_1": null,
    "thesis": "Onboarded from broker screenshot 2026-08-05; true entry date unknown (entry_date = onboard date). Managed-care value; GTC stop 357 resting at broker.",
    "sector": "XLV",
    "catalysts": [],
    "trail_state": "initial",
    "alerts_sent": [],
    "stage": "trailing",
    "setup_type": "Manual",
    "setup_grade": null
  },
  {
    "ticker": "GOOGL",
    "ledger_path": "ledgers/positions/GOOGL.yml",
    "entry_date": "2026-08-05",
    "entry_price": 324.65,
    "shares": 13,
    "stop": 340.0,
    "target_1": null,
    "thesis": "Onboarded from broker screenshot 2026-08-05; true entry date unknown. +16.9% over cost; GTC stop 340 resting (0.26% below the +5% trail floor 340.88 - nudge up). 18.0% of equity - BREACHES the binding 10% per-position cap; trim ~6 sh or record explicit operator override.",
    "sector": "XLC",
    "catalysts": [],
    "trail_state": "plus5",
    "alerts_sent": [],
    "stage": "trailing",
    "setup_type": "Manual",
    "setup_grade": null
  },
  {
    "ticker": "LMT",
    "ledger_path": "ledgers/positions/LMT.yml",
    "entry_date": "2026-08-05",
    "entry_price": 570.36,
    "shares": 4,
    "stop": 560.0,
    "target_1": null,
    "thesis": "Onboarded from broker screenshot 2026-08-05; true entry date unknown. Defense prime; tight GTC stop 560 (-1.8% from cost) resting at broker.",
    "sector": "XLI",
    "catalysts": [],
    "trail_state": "initial",
    "alerts_sent": [],
    "stage": "trailing",
    "setup_type": "Manual",
    "setup_grade": null
  },
  {
    "ticker": "MRVL",
    "ledger_path": "ledgers/positions/MRVL.yml",
    "entry_date": "2026-05-23",
    "entry_price": 165.99,
    "shares": 6,
    "stop": null,
    "target_1": null,
    "thesis": "Earnings beat + raised guide across FY27/FY28; custom ASIC >$10B by FY29. Trimmed 8 sh 2026-06-02 (+$938 realized). 2026-08-05 broker sync: NO resting stop at broker (journal's 284.0 was stale/above-market). RULE GAP at +32%: trail floor >=174.29, Minervini ~201.55 - place GTC stop ~200.",
    "sector": "XLK",
    "catalysts": [],
    "trail_state": "plus5",
    "alerts_sent": [],
    "stage": "trailing",
    "setup_type": "Manual",
    "setup_grade": null
  },
  {
    "ticker": "VRDN",
    "ledger_path": "ledgers/positions/VRDN.yml",
    "entry_date": "2026-06-11",
    "entry_price": 16.04,
    "shares": 50,
    "stop": 17.5,
    "target_1": null,
    "thesis": "Veligrotug (TED) post-PDUFA launch-ramp hold. 2026-08-05 broker sync: GTC stop 17.50 resting (+9.1% over cost - locks gain, trail-compliant). Earnings ~2026-08-06 - holding through with stop.",
    "sector": "XLV",
    "catalysts": [
      {"date": "2026-08-06", "event": "earnings"}
    ],
    "trail_state": "plus5",
    "alerts_sent": [],
    "stage": "trailing",
    "setup_type": "Manual",
    "setup_grade": null
  }
]
```

On apply, also: mark `ledgers/positions/{NBIS,QCOM,WCC,PLD,TRGP,ALAB,COHR}.yml` closed
(exit data from operator when available) and create minimal ledgers for ELV/GOOGL/LMT
(the `portfolio-manager` onboard mode builds proper ones with ATR).

Suggest-only; nothing applied. Wall intact.
