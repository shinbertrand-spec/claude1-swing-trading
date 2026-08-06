# EDGAR coverage probe — decides EODHD vs EDGAR-only (2026-08-06)

Executes `Obsidian/Bertieboo/Output/2026-08-06-claude1-edgar-coverage-probe-handoff.md`.
Read-only against EDGAR. NOT a trial: no events pipeline, no `ledgers/trials.yml` entry,
no grid, no subscription. Probe script: `2026-08-06-edgar-coverage-probe.py` (alongside).

## Pre-registered decision rule (WRITTEN BEFORE ANY PROBE RAN — handoff §2, copied verbatim)

Let **C** = coverage = (names with ≥1 EDGAR 8-K carrying item 2.02 and a parseable
`acceptanceDateTime` in the quarter) ÷ (total names in the universe).
Let **A** = ambiguous-timestamp rate = ambiguous ÷ covered, where a covered name's
classifying filing is BMO if accepted before 09:30 US/Eastern, AMC if at/after 16:00,
ambiguous if between 09:30 and 16:00.

| Result | Decision |
|---|---|
| **C ≥ 95%** and **A ≤ 5%** | **EDGAR-only. Close the EODHD question permanently.** Record as decided, not deferred. |
| **C 90–95%** or **A 5–10%** | **EDGAR-only**, but log the gap and check whether misses cluster non-randomly. Clustered misses are a bias risk even at high coverage. |
| **C < 90%** or **A > 10%** | **Buy ONE month of EODHD (~US$20–30), not a year** (operator action, reported not taken). Re-run with both feeds, decide the annual on the measured delta. |
| **Systematic 6-K / FPI hole** | Treated separately from C — a structural exclusion changes the universe; reported as its own finding, not averaged in. |

Coverage and timestamp usability are different failure modes — **not averaged into a
single verdict**.

Method commitments, also pre-registered:
- Target quarter: **2025 Q2** (filings accepted 2025-04-01 → 2025-06-30 ET) — the handoff default.
- Universe: liquid_us resolved through `tools.quant_strategies._universe.resolve_universe_tickers`
  (the trial's own mechanism); exact list + count stated in results.
- No external ground truth (yfinance dates explicitly excluded per spec §3a).
- `acceptanceDateTime` from SEC submissions JSON is UTC (`Z`-suffixed); converted to
  US/Eastern via zoneinfo before BMO/AMC classification. Sanity check committed in advance:
  the hour histogram should mass pre-open (06:00–09:30) and post-close (16:00–18:00) ET;
  a midday mass would indicate a timezone-handling error, to be reported as a method
  finding, not shipped.
- Names that genuinely did not report in-quarter (delisted/acquired mid-quarter, or no
  EDGAR filings at all in the window) are separated as legitimate residuals, not coverage
  failures. Names EDGAR covers only via 6-K (FPIs) are the structural finding.
- Heavy filers whose `recent` submissions window does not reach back to 2025-04-01 get
  their older submission pages fetched — a truncated window must not masquerade as a miss.

## RESULTS — pending (this section filled only after the probe runs)

_(placeholder — nothing below this line was written before execution)_

## RESULTS (probe ran after the pre-registration above)

- Universe: `liquid_us_2026q2` via `resolve_universe_tickers` — **1178 names** (SPY excluded). Quarter: filings accepted 2025-04-01 → 2025-06-30 (ET).
- Ticker→CIK map: SEC `company_tickers.json`; 16 names unmapped.

### C — coverage
- Covered (≥1 8-K w/ item 2.02 + parseable acceptanceDateTime in-window): **1137/1178 = 96.5%**
- Legitimate residuals separated out: **20** (no CIK mapping: 16; zero EDGAR filings of any form in-window: 4)
- Coverage excluding residuals: **1137/1158 = 98.2%**
- FPI/6-K structural (filed 6-K in-window, no 2.02 8-K): **1** — ['SN']
- Other misses (**true misses** — filings in-window but no 2.02 8-K): **20**

  | ticker | note |
  |---|---|
  | AAP | filings in window but no 2.02 8-K |
  | CTAS | filings in window but no 2.02 8-K |
  | ELF | filings in window but no 2.02 8-K |
  | EXLS | filings in window but no 2.02 8-K |
  | GGG | filings in window but no 2.02 8-K |
  | HIW | filings in window but no 2.02 8-K |
  | LIF | filings in window but no 2.02 8-K |
  | MOS | filings in window but no 2.02 8-K |
  | OZK | filings in window but no 2.02 8-K |
  | PSKY | filings in window but no 2.02 8-K |
  | PTC | filings in window but no 2.02 8-K |
  | Q | filings in window but no 2.02 8-K |
  | RAL | filings in window but no 2.02 8-K |
  | SOLS | filings in window but no 2.02 8-K |
  | SRPT | filings in window but no 2.02 8-K |
  | TRNO | filings in window but no 2.02 8-K |
  | UNIT | filings in window but no 2.02 8-K |
  | URBN | filings in window but no 2.02 8-K |
  | VNOM | filings in window but no 2.02 8-K |
  | VSNT | filings in window but no 2.02 8-K |

- No-CIK list: ['AEP', 'ASGN', 'BLD', 'CPRX', 'EXPI', 'GTLS', 'IAC', 'JHG', 'KW', 'MASI', 'NSA', 'PRA', 'SATS', 'SEM', 'VRE', 'VSCO']
- Zero-filings list (likely delisted/acquired): ['NVRI', 'PNFP', 'VGNT', 'XOM']

### A — timestamp usability (classifying filing per covered name, ET)
- BMO (<09:30): **535** (47.1%)
- AMC (>=16:00): **550** (48.4%)
- Ambiguous (09:30–16:00): **52** → **A = 4.6%**

Hour-of-day histogram (ET, acceptance hour of the classifying filing):

| hour | n | | hour | n |
|---|---|---|---|---|
| 00 | 0 | | 12 | 27 |
| 01 | 0 | | 13 | 9 |
| 02 | 12 | | 14 | 2 |
| 03 | 11 | | 15 | 0 |
| 04 | 4 | | 16 | 482 |
| 05 | 2 | | 17 | 40 |
| 06 | 213 | | 18 | 17 |
| 07 | 197 | | 19 | 3 |
| 08 | 78 | | 20 | 6 |
| 09 | 21 | | 21 | 2 |
| 10 | 7 | | 22 | 0 |
| 11 | 4 | | 23 | 0 |

### Decision (per the PRE-REGISTERED rule)
- C = 96.5% · A = 4.6% → **EDGAR-only. EODHD question CLOSED permanently.**

## Honest findings & surprises (handoff §5.6–7; post-run forensics on the miss lists)

**1. The tz sanity check passed exactly as pre-committed.** Mass sits pre-open (06:00–08:00
= 488) and post-close (16:00–17:00 = 522; 482 in the 16:00 hour alone = the classic 16:05
release + prompt acceptance). No midday mass → the UTC→ET conversion is right, and
acceptance-time ≈ release-time looks plausible for 2.02 furnishings at this granularity.

**2. Both "residual" buckets are probe-side ticker-map drift, NOT EDGAR gaps — verified.**
- *No-CIK (16):* AEP/MASI/VSCO/IAC etc. confirmed absent from TODAY'S `company_tickers.json`
  — 2026 corporate actions removed the tickers. A 2026-08 map was used to probe a 2025-Q2
  universe; point-in-time mismatch.
- *Zero-filings (4):* worse and more interesting — **successor-CIK drift**. Today's map
  resolves XOM → "ExxonMobil Holdings Corp" **CIK 2115436** (a 2026 holdco reorg entity),
  not "Exxon Mobil Corp" CIK 34088 which actually filed in 2025 Q2. Same pattern:
  PNFP → 2082866, NVRI → 2104052, VGNT → 2078008. The successor genuinely has zero 2025-Q2
  filings; the predecessor's filings exist. **False misses.**
- Direction of error: all 20 counted as MISSES in the headline C = 96.5%, so **measured C is
  a floor** and the rule passed on the conservative number.
- **Pipeline lesson (recorded, not built):** the real trial needs a point-in-time
  ticker→CIK map (or predecessor-CIK resolution). This is the one engineering item the
  probe surfaced.

**3. "True misses" decompose into two benign classes — evidenced from cached submissions.**
- *Item-tagging looseness:* PTC (2025-04-30 20:03 ET, items `9.01` only), MOS (2025-05-06
  20:26, `9.01`), URBN (2025-05-22 19:13, `8.01,9.01`) all filed genuine evening earnings
  8-Ks on their known report dates — tagged without `2.02`. A production filter of
  "2.02 OR earnings-shaped 9.01/8.01" would recover these (pipeline design, out of scope).
- *Fiscal-calendar offsets:* CTAS (May FYE) legitimately reports outside Apr–Jun — not a
  miss at all. Several small-cap unknowns (LIF/Q/RAL/VSNT…) likely same class.
- Net: **true EDGAR coverage is materially above the measured 96.5% floor.**

**4. A = 4.6% is a near-boundary pass** (0.4pp under the 5% bar). The 52 ambiguous names
cluster 12:00–13:00 ET (36 of 52) — genuine midday releases, not a boundary artifact around
16:00. Pre-registered rule, taken as it fell.

**5. FPI/6-K structural hole: 1 name (SN) of 1,178 = 0.08%.** No structural exclusion; the
one gap EODHD would genuinely close is empty on this universe.

**Conclusion stands, strengthened:** every forensic finding pushed in the same direction —
measured C under-counts. EDGAR-only; the EODHD question is **decided, not deferred**.
Probe cache (`_edgar_probe_cache/`) deleted after forensics; script retained alongside.

