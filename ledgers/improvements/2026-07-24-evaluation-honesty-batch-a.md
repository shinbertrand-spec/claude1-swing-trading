# Evaluation-honesty policies — Batch A implementation record + audits

- date: 2026-07-24
- branch: `cherrypick-batch-a` (operator-carried spec, NOT an autonomous proposal —
  authorization = Bertrand pointing the session at the vault spec
  `Output/2026-07-24-claude1-cherrypick-implementation-spec.md`; disposition
  therefore APPLIED-BY-OPERATOR-DIRECTION, listed here for the audit trail only)
- provenance: 2026-07-24 GitHub cherry-pick trawl (DeepFund MIT / stockbench
  Apache-2.0 / TradingAgents Apache-2.0 / INVESTOR-BENCH MIT — patterns only,
  zero code copied, zero new dependencies installed → no malicious-code pass needed)
- tests: 88 passing across the touched files; full suite 1939 passed with 6
  pre-existing environment failures (operator-set NFLX cron gate +
  concurrent uncommitted NFLX-guard edits — verified identical on base, see §4)

## 1. What shipped

| Policy | Code | Eval-artifact change |
|---|---|---|
| A1 post-cutoff eval of LLM judgment | `tools/model_cutoffs.py` (registry + `cutoff_for` + `eval_window_contaminated`); `aggregate_panel(model=)` | panel schema + verdict + calibration-log records carry `model` + `model_cutoff` (optional/nullable — old artifacts validate) |
| A2 baseline honesty | `tools/auto_paper/benchmarks.py`; `compute_performance(include_baselines=True)`; `/auto-paper-perf` template | report carries `baselines` block (SPY B&H, equal-weight traded B&H, SPY 12-1 TS-momentum) + Sortino/MDD headline line |
| A3 as-of data-layer contract | `tools/asof.py` (shared helper + `LIVE_ONLY_ADAPTERS` + `assert_backtest_safe`); `earnings_calendar(as_of=)`; `data_cache.load(end=)` | n/a (adapter layer) |
| A4 warmup/test split | `calibration_analysis.compute(warmup_start=, scored_start=)` + CLI flags | report carries `warmup_start`/`scored_start` + warmup exclusion counts |

Doctrine: CLAUDE.md § "Evaluation-honesty policies (adopted 2026-07-24)".

Model-cutoff values verified against the vendor model docs (platform.claude.com
models overview, fetched 2026-07-24) — training-data cutoff chosen as the
conservative gate over "reliable knowledge" cutoff.

## 2. A1 contamination audit of existing eval artifacts

Rule: LLM-judgment evals scored on pre-cutoff windows get `contaminated: true`.
Sweep of every existing LLM-judgment eval artifact (2026-07-24):

| Artifact family | Window | Verdict |
|---|---|---|
| `ledgers/swing-critics/_calibration/*.jsonl` (20 files, 105 records) | live 2026-05-28 → 2026-06-26 | CLEAN — live windows post-date every in-use model's training cutoff |
| `ledgers/swing-critics/<date>/<TICKER>/` panel envelopes | live 2026-05-28 → 2026-06-15 | CLEAN — same reasoning |
| `ledgers/debate/*.yml` (8 ledgers) | live 2026-06-07 → 2026-06-19 | CLEAN — same reasoning |
| `ledgers/_auto_paper_runs/<ts>/` skeptic/panel envelopes | live 2026-05-28 → 2026-07-23 | CLEAN — same reasoning |
| `backtest_results/` + `journal/backtest*/` (2017–2025 windows) | historical | EXEMPT — deterministic rules only, no LLM judgment scored; A1 explicitly permits |

**Zero `contaminated: true` flags required.** The risk A1 guards against is
FUTURE: any proposal to "backtest" a critic/debate/news-agent judgment over
2017–2025 would score memorization — the policy + `tools/model_cutoffs.py`
now block that structurally.

Scoping note: `model`/`model_cutoff` fields were added to the CRITIC-PANEL
schema + calibration log (the LLM-judgment eval artifact with a scoring loop).
The debate ledger was deliberately NOT extended: it is a per-decision artifact
whose synthesis block is composed by a deterministic tool
(`tools/debate_synthesis.py`) from LLM-written bull/bear reports — the model
identity belongs to the researcher/skeptic sessions, not the composer. If
debate outcomes ever get a scored evaluation loop, add the fields to that
eval's artifact at that point.

## 3. A3 grep-audit — adapter → as-of enforcement

| Adapter | as-of enforced? | Status |
|---|---|---|
| `tools/backtest/data_cache.py` | Y — `fetch(start,end)`; **`load(end=)` added this batch** (was unbounded) | FIXED |
| `tools/fundamentals/pit_fundamentals.py` | Y — strict PIT `filed<=asof` | already correct |
| `tools/fundamentals/insider_track_record.py` | Y — forward window closes `<=asof` | already correct |
| `tools/fundamentals/insider_events.py` | Y — asof threaded via injected fns | already correct |
| `tools/fundamentals/form345_bulk.py` | Y — FILING_DATE availability key | already correct |
| `tools/backtest/security_master.py` | Y — `idx<=asof` | already correct |
| `tools/earnings_calendar.py` | **Y — `as_of=` anchor added this batch** (was `now()`-anchored). Honest caveat: yfinance serves the current calendar; historical as_of re-anchors within today's known dates (second-order look-ahead for far-past windows) | FIXED |
| `tools/fundamentals/edgar_eps.py` | N — latest-TTM only | registered LIVE-ONLY; PIT alternative = `pit_fundamentals` |
| `tools/news_research/x_scanner.py` | N — 60-min recency floor, live search | registered LIVE-ONLY; dated evals replay from stored corpus/snapshots |
| `tools/x_common/twitterapi_client.py` | N — no as-of on search | registered LIVE-ONLY |
| `tools/auto_paper/screener.py` (finviz) | N — live scrape | registered LIVE-ONLY |
| `tools/news_research/market_temperature.py` | N — live gauges | registered LIVE-ONLY |

LIVE-ONLY entries are enumerated in `tools.asof.LIVE_ONLY_ADAPTERS`; pipelines
gate with `tools.asof.assert_backtest_safe(<module>)`. A live-only adapter in a
backtest is now a deterministic RuntimeError, not a prompt-discipline hope.

## 4. Full-suite failure disposition (pre-existing, NOT this batch)

`uv run pytest`: 1939 passed, 6 failed. All 6 reproduce on the base tree with
this batch stashed:

- `test_auto_paper_run_entry*` (3): halt on the operator-set NFLX cron gate
  (`journal/paper-auto/cron_gate.json`, `nflx_naked_short_guard_bug`) — the
  tests read real repo state; expected while the gate is set.
- `test_auto_paper_orphan_check` (3): fail against CONCURRENT uncommitted
  working-tree edits to `tools/auto_paper/orphan_check.py` +
  `tools/auto_paper/reconcile.py` — the signed-held NFLX-guard fix in progress
  in another session (observed mid-edit 2026-07-24; verified: the reconcile
  diff replaces the `abs()` naked-short read per the operator runbook). Those
  files are untouched by and excluded from this batch's commit.

## 5. DoD checklist vs the spec

- A1: doctrine line ✅ · eval schema carries model_cutoff ✅ · audit pass over
  existing artifacts (this doc §2, zero flags) ✅
- A2: baselines in the report generator (code, not prompts) ✅ · regenerated
  recent paper-run report with baseline columns → sibling file
  `2026-07-24-auto-paper-perf-with-baselines.md` ✅
- A3: grep-audit table (§3) ✅ · shared helper not per-tool copies ✅ · unit
  test per fixed adapter (earnings as-of anchor; data_cache end-trim) ✅
- A4: schema carries warmup_start/scored_start ✅ · doctrine line ✅
