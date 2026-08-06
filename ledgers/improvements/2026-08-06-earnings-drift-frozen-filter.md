# FROZEN event filter + PIT-CIK choice — event_earnings_drift (2026-08-06)

Tracked mirror of the addendum in `plans/2026-07-25-event-earnings-drift-candidate-spec.md`
(plans/ is gitignored; build-handoff SS2.2 requires the freeze COMMITTED before the grid).
This commit predates any event extraction, kind build, or grid run.

## ADDENDUM (2026-08-06, committed BEFORE any event extraction or grid run) — FROZEN EVENT FILTER + PIT-CIK choice

Per build-handoff §2.2, the event-filter rule is frozen here, first, with explicit
criteria. **Frozen for the trial's life; any later change = new trial, trials.yml
re-derived.**

**Frozen filter — STRICT item-2.02:** an earnings event is an EDGAR filing of form
`8-K` or `8-K/A` whose `items` field contains `2.02`, with a parseable
`acceptanceDateTime`. Timing classification from `acceptanceDateTime` (UTC in the
submissions JSON, converted to US/Eastern): **BMO** if before 09:30, **AMC** if at/after
16:00, **unknown** if between (treated as AMC per §3b; if the unknown share exceeds 10%
that is a data-quality escalation, not a silent default). Multiple qualifying filings
for one ticker within 3 calendar days collapse to the earliest (amendment/duplicate
suppression). No other form types, no item-8.01/9.01 widening, no exhibit inspection.

**Why strict, stated before running:** the coverage probe found genuine earnings 8-Ks
tagged `9.01`/`8.01` without `2.02` (PTC/MOS/URBN verified). The handoff itself flags
that a widened filter was conceived after seeing which names it recovers — the exact
researcher-degrees-of-freedom leak the 91-trial DSR count prices. Strict-2.02 is the
zero-dof choice. Disclosed cost: ~1.7% of universe names per quarter whose filing agents
habitually omit the 2.02 tag never generate events (they drop out of the universe's
event stream entirely; no bias is introduced into measured `ear` for names that do tag).
`prior_pos_ear_count` is defined over the ticker's **last 4 observed events** — a
tag-gap quarter widens the calendar span of that window rather than biasing it.

**PIT ticker→CIK choice (handoff §2.1): Option 1 — CIKs frozen into a universe sidecar**
at `tools/quant_strategies/_universes/liquid_us_2026q2_cik.yml`, resolved once
(today's map → in-window validation via each CIK's earliest-filing date → predecessor
chase for successor-CIK/unmapped names → unresolved names carried as `cik: null`,
excluded and disclosed). Acceptance test: the probe's 2025-Q2 coverage count re-run
with the PIT map must EXCEED the probe's 96.5% floor.
