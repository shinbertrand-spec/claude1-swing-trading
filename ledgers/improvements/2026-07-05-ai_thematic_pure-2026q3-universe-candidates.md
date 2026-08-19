# ai_thematic_pure 2026q3 rebuild — CANDIDATE list (staging; suggest-only)

**Not a pilot proposal** — this is an operator-instructed staging artifact
(2026-07-05), filed here because the pilot permission wall correctly denies
writes under `tools/`. It does NOT get an accept-ledger row (the accept-ledger
counts drafter→judge→critic pilot proposals only; mixing this in would muddy
the ≥5-distinct-proposal wait-condition signal).

Status: **candidates only — normal gate evaluation, no direct add.**
The pinned `tools/quant_strategies/_universes/ai_thematic_pure_2026q2.yml` is
immutable-by-convention and is untouched. Nothing here enters a universe until
the 2026q3 rebuild runs the standard pipeline: hand-curation review → bucket
assignment in `scripts/build_ai_thematic_universes.py` (PURE_BUCKETS) →
ADV ≥ 500K trailing-60d floor → downstream strategy gate under the
`ai_thematic_pure` profile (Sharpe > 1.2 ∧ |MDD| < 22% ∧ n ≥ 30 ∧
per-window ≥ 60%).

**Merge target (operator, at q3 rebuild):** add the surviving names to
PURE_BUCKETS in `scripts/build_ai_thematic_universes.py` and re-run the build
to pin `ai_thematic_pure_2026q3.yml`. Consumer note: the only strategy on this
universe is `xs_short_term_reversal_ai_pure` (deployable_setups.yml,
`hold: true` since 2026-06-05 pending the v2 eval unblock); a q3 rebuild feeds
that row's re-validation.

---

## Candidate 1 — AAOI (Applied Optoelectronics)

- **Proposed bucket:** Networking / optical / interconnect
  (alongside ANET, CIEN, COHR, LITE, CRDO)
- **Origin:** vault swing note `swing-2026-07-05-optical-interconnect-retail-arrival`
  (Mode A ingest 2026-07-05). Answers the `optical-interconnect` open question:
  a US-listed optical pure-play below the COHR/LITE tier.
- **Fundamentals verified 2026-07-05** (vault note
  `2026-07-05-screen-clip-batch-canonical-verification`, verdict [CONFIRMED]):
  Q1 2026 datacenter revenue $81.4M **+154% YoY** (total $151.1M +51%);
  FY2026 guidance raised to >$1.1B; demand-exceeds-supply framing through at
  least mid-2027 (target >500K units/mo of 800G/1.6T by end-2026).
- **Rebuild checks required (normal gates, nothing waived):**
  - ADV ≥ 500K trailing-60d median — the binding structural check at build time.
  - Standard sourcing review at hand-curation (the q2 sourcing caveat applies
    here too).
- **Caveats to carry into gate evaluation:**
  1. **Late-cycle crowding datapoint.** The name surfaced via retail-finfluencer
     distribution (anonymous Instagram Reel, buy-every-dip framing) *after*
     Situational Awareness LP exited LITE ($479M) + COHR ($89M) in Q4 2025.
     Thesis-tier source → smart-money de-grossing → retail arrival is the
     classic late-cycle ordering. This strengthens bear-case weighting on the
     optical sub-bucket generally; it is a sentiment annotation, not a
     membership disqualifier.
  2. **Lowest-trust source class** for the original surfacing; the load-bearing
     numbers were independently verified vault-side 2026-07-05 (the same Reel's
     other claims needed correction — e.g. NVIDIA/Lumentum was $2B, not $1B).
  3. Materially smaller cap than LITE/COHR — cap is recorded but not filtered
     per universe convention; the liquidity floor is the only build gate.

## Candidate 2 — EUV (Corgi Lithography & Semiconductor Photonics ETF)

- **Proposed role:** listed for evaluation per instruction, with a
  **structural-mismatch flag**. If retained, nearest bucket is Networking /
  optical / interconnect; the better framing is sub-bucket *benchmark*, not
  member.
- **Origin:** same vault note pair as AAOI. The verification note confirms the
  fund is real and corrects the Reel's ticker: **EUV** (the Reel's "PHOT" is
  GrowGeneration's old cannabis ticker — do not carry it). Launched May 2026,
  ~$262M AUM; holds TSM / ASML / GLW / LRCX / AMAT + AAOI / LITE / COHR;
  3 more photonics ETFs were in registration as of mid-May 2026.
- **Flags for gate evaluation (expected to be decisive):**
  1. **It is an ETF in a single-name equity universe.** Every current member of
     ai_thematic_pure is an operating company. The origin note's stated use is
     "a liquid basket benchmark for stratified-signal comparison (COHR+LITE
     pair vs basket)" — a benchmark instrument, not a reversal-signal name.
  2. **~2 months of trading history at a Q3 rebuild** vs the 2017–2026 backtest
     window. `tools.backtest.data_cache` cannot produce a usable series; the
     name is untestable under the deployment gate as a member.
  3. **Holdings overlap** heavily with existing members (ASML, LRCX, AMAT,
     LITE, COHR + candidate AAOI) — as a member it double-counts exposure the
     universe already carries.
- **Recommended disposition at rebuild:** evaluate normally per instruction;
  expected outcome is REJECT-as-member on history + instrument-type grounds,
  RETAIN-as-benchmark for the optical sub-bucket stratified-signal comparison.

---

## Timing awareness (rebuild window)

- **June CPI prints 2026-07-14** (verified in the same 2026-07-05 vault batch;
  May CPI came in 4.2 vs 3.8 prior on the oil-shock path). Macro context for
  any rebuild-adjacent decisions, not a gate input.

## Provenance

- Staged 2026-07-05 by the Claude1 session on operator instruction:
  "add AAOI and EUV to the ai_thematic_pure 2026q3 universe-rebuild CANDIDATE
  list only — normal gate evaluation, no direct add."
- Vault refs: `wiki/notes/swing-2026-07-05-optical-interconnect-retail-arrival.md`,
  `wiki/notes/2026-07-05-screen-clip-batch-canonical-verification.md`,
  `wiki/notes/swing-optical-interconnect-tier-stratification-candidate.md`.
