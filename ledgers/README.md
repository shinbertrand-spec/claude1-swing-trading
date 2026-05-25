# Fact Ledger — Schema Spec

**Phase 1 of the swing-risk-compliance-doctrine 4-phase path.** Per-ticker structured fact storage that subagents read and write instead of re-deriving values in prose. The doctrine's premise: LLM arithmetic and prose-checking fail systematically; deterministic ledgers + scripted arithmetic do not.

This file is the source of truth for the schema. The machine-checkable form lives at [`_schema/ledger.schema.json`](_schema/ledger.schema.json) (JSON Schema 2020-12). Five worked examples live in [`_examples/`](_examples/).

> **What Phase 1 does:** define the schema, ship examples, update CLAUDE.md and the journal template to reference ledgers.
> **What Phase 1 does NOT do:** ship Python tools (Phase 2), enforce staleness (Phase 3), enforce reasoning-trace verification (Phase 4). Those phases will add code; this phase only adds structure for that code to bind to.

---

## Directory layout

```
ledgers/
  candidates/
    YYYY-MM-DD/<TICKER>.yml     # pre-trade research output, built by trade-researcher
                                 # trade-skeptic appends bear-side trace_refs in-place
    YYYY-MM-DD/<TICKER>.md      # bull (trade-researcher) Markdown report
    YYYY-MM-DD/<TICKER>-bear.md # bear (trade-skeptic) Markdown report, with terminal ```json fragment
  positions/
    <TICKER>.yml                 # live position, evolves over time, one file per open ticker
                                 # archived to closed/ on exit (TBD when first position closes)
  debate/                        # Phase 7 (H1) — per-decision bull/bear debate state
    <TICKER>-<DATE>.yml          # one file per debated candidate, written by tools.debate_synthesis
    _schema/debate.schema.json   # JSON Schema for the debate ledger
    _examples/                   # 3 worked examples (strong-bull, strong-bear, balanced-watch)
  _schema/
    ledger.schema.json           # JSON Schema for structural validation
  _examples/
    sepa-vcp-candidate.yml       # A+ SEPA-VCP candidate
    ep-golden-candidate.yml      # Golden EP candidate
    pullback-20sma-candidate.yml # Secondary setup #1
    rsi-divergence-candidate.yml # Secondary setup #2
    resistance-break-candidate.yml # Secondary setup #3
    pyramided-position.yml       # Open position showing STARTER → Stage-2 → Stage-3
  README.md                      # this file
```

Candidate ledgers are dated; position ledgers are not (one file per ticker, mutated in place until close).

---

## Section reference

Every ledger is a YAML document (or JSON — the schema accepts both) with these top-level sections. All sections except `meta` are optional in the schema, but particular setups require particular sections — see "Required sections by lifecycle state" below.

### `meta` (required)
Identity + lifecycle.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | const `"1.0"` | Bump on structural change; old ledgers stay on their version |
| `ticker` | uppercase string | Pattern `^[A-Z][A-Z0-9.\-]{0,9}$` |
| `company_name` | string | optional |
| `asof` | ISO-8601 timestamp | Point in time this snapshot represents |
| `state` | enum | `candidate` / `rejected` / `starter` / `stage-2` / `stage-3` / `trailing` / `closed` |
| `ledger_path` | string | Self-reference relative path |
| `created_by` | string | Agent or skill name |
| `created_at` | ISO-8601 | |
| `updated_by` | string | optional |
| `updated_at` | ISO-8601 | optional |

### `quote`
Live price snapshot. Per Requirement 4 staleness rule: max 4 hours old during market hours.

| Field | Type | Notes |
|---|---|---|
| `last`, `bid`, `ask` | number > 0 | |
| `session` | enum | `premarket` / `regular` / `afterhours` / `closed` |
| `source` | source_tag | `broker_api`, `web:domain.com`, etc. |
| `source_version` | string | optional |
| `fetched_at` | ISO-8601 | **load-bearing for staleness check** |

### `fundamentals`
EPS, revenue, growth, earnings calendar. Per trade-researcher working principle: **next earnings date is verified against TWO independent sources** — both go into the ledger (`next_earnings_source` + `next_earnings_source_secondary`).

| Field | Type | Notes |
|---|---|---|
| `eps_last_q`, `eps_prior_year_q` | number | Raw values for derivation |
| `eps_yoy_growth` | decimal | 0.21 = 21%. Computed by `tools/compute_yoy.py` in Phase 2 |
| `revenue_last_q`, `revenue_prior_year_q` | number | |
| `revenue_yoy_growth` | decimal | |
| `eps_surprise_pct` | decimal | Beat/miss vs consensus |
| `guidance_change` | enum | `raised` / `maintained` / `lowered` / `none` / `unknown` |
| `filing_date` | ISO-date | Last earnings release date |
| `next_earnings_date` | ISO-date | **Hard rule:** no entries within 10 trading days (unless EP) |
| `next_earnings_source` | source_tag | Primary source |
| `next_earnings_source_secondary` | source_tag | Required second source |
| `market_cap_usd` | number | CLAUDE.md hard rule: > $2B |
| `avg_daily_volume_shares` | integer | CLAUDE.md hard rule: > 500K |
| `source` | source_tag | |
| `fetched_at` | ISO-8601 | |

### `technical`
Indicators and Trend Template state. Drives setup classification and stage.

| Field | Type | Notes |
|---|---|---|
| `price_above_50dma`, `_150dma`, `_200dma` | boolean | Trend Template criteria 1, 5 |
| `ma_alignment_50_150_200` | boolean | Trend Template criterion 4: 50 > 150 > 200 |
| `ma200_rising` | boolean | Trend Template criterion 3 |
| `ma200_rising_months` | integer | Minervini prefers ≥ 4-5 |
| `price_pct_above_52w_low` | number | Trend Template criterion 6: ≥ 30% |
| `price_pct_below_52w_high` | number | Trend Template criterion 7: ≤ 25% |
| `rs_rating` | 1-99 | IBD-style. Trend Template prefers ≥ 70 |
| `trend_template_passes` | 0-8 | All 8 must pass for Stage 2 candidate per swing-regime-playbook Level 3 |
| `stage` | 1-4 | Weinstein stage; only Stage 2 qualifies for longs |
| `weeks_above_200dma` | integer | |
| `atr_14` | number > 0 | Input to `position_sizer.py` |
| `adr_20_pct` | number > 0 | Kullamägi-school sizing input |
| `rsi_14` | 0-100 | |
| `adx_14` | 0-100 | |
| `volume_today_vs_20d_avg` | ratio | 1.5 = 50% above. VCP breakout requires ≥ 1.4 |
| `computed_at` | ISO-8601 | |
| `source` | source_tag | |

### `regime`
Per swing-regime-playbook three-level check.

| Field | Type | Notes |
|---|---|---|
| `broad_market_ticker` | `SPY`/`QQQ` | |
| `broad_market_trend_template_passes` | 0-7 | RS criterion skipped for indices |
| `broad_market_stage_class` | enum | `stage_2_confirmed` (7/7), `stage_2_weakening` (5-6), `stage_3_transitional` (3-4), `stage_4` (0-2) |
| `sector_etf` | string | e.g. `XLK`, `XLE` |
| `sector_trend_template_passes` | 0-7 | |
| `sector_qualifies_for_long` | boolean | True iff sector ≥ 5/7 |
| `regime_multiplier` | 0-1 | Per swing-position-sizing: 1.0 / 0.75 / 0.5 / 0 |
| `computed_at` | ISO-8601 | |

### `setup_classification`
The named-setup tag plus confluence checklist. **Each criterion's `trace_refs` is required** — empty trace_refs = unfaithful by Requirement 3.

| Field | Type | Notes |
|---|---|---|
| `type` | enum | `SEPA-VCP` / `EP` / `Pullback-20SMA` / `RSI-Divergence` / `Resistance-Breakout` |
| `grade` | enum | A+/A/B/C for SEPA-VCP/Pullback/RSI-Div/Resistance-Break; SuperSwan/Swan/Duck/Chicken/GoldenEP for EP |
| `confluence_checklist[]` | array | Each entry: `{criterion, status: PASS/FAIL/PARTIAL/UNKNOWN, evidence, trace_refs[]}` |
| `pivot_price`, `stop_price` | number > 0 | |
| `stop_distance_pct` | decimal | Must be ≤ 0.08 per Minervini cap |
| `trace_refs[]` | int[] | Overall classification trace |

### `catalyst`
The A leg of SEPA. Also drives EP. Vague macro tailwind does **not** qualify per swing-setup-library — be specific.

| Field | Type | Notes |
|---|---|---|
| `type` | enum | `earnings` / `guidance` / `product_launch` / `regulatory_approval` / `biotech_trial` / `contract_win` / `macro_policy` / `industry_rotation` / `none` |
| `description` | string | One sentence |
| `date` | ISO-date | When the catalyst hits |
| `source_url` | URL | Primary source |
| `source_secondary_url` | URL | Strongly recommended for earnings catalysts |
| `verified` | boolean | True iff source URL fetched + confirmed |
| `fetched_at` | ISO-8601 | |

### `ep_specific`
Only present when `setup_classification.type == "EP"`. Per swing-earnings-pivot.

| Field | Type | Notes |
|---|---|---|
| `gap_pct` | decimal | ≥ 0.10 for EP eligibility |
| `premarket_volume_shares` | integer | |
| `first_30min_volume_vs_adv` | ratio | Best EPs ≥ 1.0 (full ADV in first 30 min) |
| `prior_rally_3m_pct`, `prior_rally_6m_pct` | decimal | "Neglected" filter |
| `neglected_qualified` | boolean | Computed from prior-rally thresholds |
| `magna_score` | 0-5 | M/A/G/N/A criteria |
| `ep_grade` | enum | Mirror of `setup_classification.grade` for EP |
| `opening_range_high` | number > 0 | Entry trigger |
| `opening_range_timeframe` | enum | `1m` / `5m` / `15m` / `60m` / `daily` |
| `mandatory_exit_date` | ISO-date | Next earnings — hard exit per swing-earnings-pivot |
| `trace_refs[]` | int[] | |

### `position_state`
Lifecycle state for an open position. Populated when `meta.state ∈ {starter, stage-2, stage-3, trailing, closed}`. Per swing-momentum-execution Anchor-and-Pyramid.

| Field | Type | Notes |
|---|---|---|
| `stage` | enum | `STARTER` (1/3 size) / `Stage-2` (full) / `Stage-3` (1.5x) / `trailing` / `closed` |
| `intended_full_shares` | integer | Target share count this position aims for |
| `intended_full_capital_pct` | decimal | Capital % at full size |
| `risk_budget_pct` | decimal | Per swing-position-sizing table |
| `starter`, `addon_1`, `addon_2` | `entry_leg` | Each leg: trigger, fill_date, shares, fill_price, initial_stop, trace_refs |
| `combined_breakeven` | number | Weighted-average entry across legs. Stop after ADD-ON #1 migrates here |
| `current_stop` | number > 0 | |
| `trail_ma` | enum | `lows_of_day` / `5_day_MA` / `10_day_MA` / `20_day_MA` / `50_day_MA` / `combined_breakeven` |
| `mandatory_exit_date` | ISO-date | EP positions only |
| `trail_state_legacy` | enum | `initial`/`breakeven`/`plus5` — mirrors `journal/positions.json` v1 for `check-positions.ps1` compat |
| `alerts_sent[]` | string[] | |

`entry_leg` shape:

| Field | Type |
|---|---|
| `trigger` | enum: `SLTB` / `MomentumBurst` / `Day7Milestone` / `EPGap` / `VCPBreakout` / `PullbackReversal` / `manual` |
| `fill_date` | ISO-date |
| `fill_time` | HH:MM ET |
| `shares` | integer ≥ 1 |
| `fill_price`, `limit_price_placed`, `initial_stop` | number > 0 |
| `trace_refs[]` | int[] |

### `sell_eval_history`
**v1-preliminary** per swing-sell-discipline. Append-only daily sell-trigger evaluation.

Each entry has the count of climax-top patterns firing (0-6), the count of violations firing (0-5), base stage (1-5), the sell-into-strength flag, the recommended action, confidence, and trace_refs. Items tagged `v1_preliminary_flag: true` should be retrospectively reviewed when the Minervini book ingest produces swing-sell-discipline v2.

### `reasoning_trace`
**The substrate Requirement 3 binds to.** Append-only numbered list. Each entry: `{id, tool, inputs, output, fetched_at}`. Other sections cite these `id`s in `trace_refs[]`.

In Phase 1, traces are populated manually as a contract. In Phases 2-4 the tools emit them directly.

### `notes`
Free-text scratchpad. **Anything load-bearing must move into a structured field** — `notes` is read-only for humans and ignored by tools.

---

## Required sections by lifecycle state

| `meta.state` | Required sections | Notes |
|---|---|---|
| `candidate` | `meta`, `quote`, `fundamentals`, `technical`, `regime`, `setup_classification`, `catalyst` (and `ep_specific` if type=EP), `reasoning_trace` | Built by trade-researcher; consumed by risk-and-compliance |
| `rejected` | Same as `candidate` + `notes` explaining rejection | Risk-and-compliance returned BLOCK; kept for audit |
| `starter` | All `candidate` sections + `position_state` with `starter` leg filled | After the first fill |
| `stage-2` | + `position_state.addon_1` filled, `combined_breakeven` recomputed | After Momentum Burst add |
| `stage-3` | + `position_state.addon_2` filled | After Day 7 milestone add (Super Swan/Golden EP only) |
| `trailing` | Full size, no more adds | Manage stop only |
| `closed` | + exit details in `position_state` (TBD field) + final `sell_eval_history` entry | Archive when closed |

---

## Staleness rules (Phase 3 — enforced)

Per swing-risk-compliance-doctrine Requirement 4 and the table in that doctrine. The schema **stores** `fetched_at`; `tools.ledger_freshness_audit` rejects ledgers whose timestamps exceed these limits.

| Section | Max staleness | Action if stale |
|---|---|---|
| `quote` | 4h during market hours | Re-fetch before APPROVE |
| `fundamentals` | Until next scheduled earnings | Flag if earnings due within 10 trading days |
| `technical` | End-of-day | Recompute on every check |
| `regime` | End-of-day | Recompute |
| `catalyst` | 7 days from publication | Re-verify still relevant |

**Absence-of-evidence rule (added 2026-05-23 red-team patch):** the verdict is `overall: fresh` iff **every** audited section has status `fresh`. The following all flip the verdict to `stale`:

- **`stale`** — section present with a timestamp past its max-staleness window
- **`missing_timestamp`** — section present but no `fetched_at` / `computed_at` populated
- **`missing_section`** — a section in `freshness.REQUIRED_SECTIONS` (currently `quote`) is entirely absent

Pre-patch behavior treated `missing_timestamp` and `missing_section` as "not stale, therefore fresh" — silently passing ledgers that omitted load-bearing sections. The red-team harness catches regressions in this rule.

Phrases like *"as of my training cutoff"* or *"as of late 2024"* MUST NOT appear anywhere in the agent's prose — see doctrine Requirement 4 for the exact BLOCK trigger list. The detector's 15 BLOCK + 2 WARN patterns now cover memory framings ("based on what I recall"), knowledge-cutoff variants ("my knowledge ends around"), probabilistic numerical claims ("probably near $230"), and similar paraphrases.

---

## Provenance discipline (Phase 4 will enforce)

Per Requirement 3: **every conclusion in the ledger must cite reasoning_trace step IDs in `trace_refs[]`**. Specifically:

- Every `setup_classification.confluence_checklist[]` entry's `trace_refs` must be non-empty
- `setup_classification.trace_refs` (overall) must reference the classification-supporting tool outputs
- `position_state.entry_leg.trace_refs` must point to the trigger-detection tool that fired the leg
- `sell_eval_history[].trace_refs` must reference the climax-top / violations / base-stage detector outputs

Empty trace_refs in a load-bearing field = unfaithful by definition; risk-and-compliance will BLOCK in Phase 4.

---

## Caveats flagged in code

Search the codebase for these markers when re-ingesting their source notes:

- `v1-preliminary: revisit after Minervini book v2 ingestion` — all sell-discipline content (sell_eval_entry definition, climax-top/violations enums)
- `Phase 2 will compute` — fields that currently expect human population but will be tool-emitted later (e.g. `eps_yoy_growth`, `combined_breakeven`, `regime_multiplier`)
- `Phase 3 will enforce` — staleness rules currently observed by convention only
- `Phase 4 will enforce` — reasoning-trace verification currently observed by convention only

---

## Examples

Five worked examples live in [`_examples/`](_examples/), one per setup type. Each demonstrates:

- The full `meta` block
- Populated `quote` / `fundamentals` / `technical` / `regime` / `setup_classification` / `catalyst`
- A populated `reasoning_trace` array with manual-source entries (`tool: "manual:broker_api"`, etc.) so the trace_refs in the classification have real targets
- For the EP example: `ep_specific` block
- For the pyramided position example: full `position_state` showing STARTER → Stage-2 → Stage-3 lifecycle plus a `sell_eval_history` with several daily evaluations

Read the SEPA-VCP example first; it's the most heavily annotated.

---

## Related

- [`_schema/ledger.schema.json`](_schema/ledger.schema.json) — machine-checkable schema
- [`../CLAUDE.md`](../CLAUDE.md) § Fact Ledger — where this fits in the operating spec
- [`../journal/_template.md`](../journal/_template.md) — per-candidate journal block now references its ledger path
- Vault: `swing-risk-compliance-doctrine.md` (the 4 requirements), `swing-setup-library.md`, `swing-earnings-pivot.md`, `swing-momentum-execution.md`, `swing-sell-discipline.md`, `swing-regime-playbook.md`, `swing-position-sizing.md`
