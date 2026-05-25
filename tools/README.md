# Phase 2 — Deterministic-arithmetic tools

Per [`swing-risk-compliance-doctrine.md`](../ledgers/README.md#related) Requirement 2: **the LLM never does arithmetic that affects decisions.** Every quantitative input to a setup classification, sizing decision, or sell trigger is computed by a Python tool in this directory and recorded in the ledger's `reasoning_trace`.

All four Phase 2 slices shipped: SEPA-VCP (2.a) + EP (2.b) + pyramiding + sell discipline (v1-preliminary) + secondary setups (2.c).

---

## Tools shipped

### Phase 2.a — SEPA-VCP pathway

| Tool | Returns | Used by |
|---|---|---|
| [`compute_yoy.py`](compute_yoy.py) | YoY growth as decimal + percent | EPS YoY, revenue YoY checks |
| [`atr_compute.py`](atr_compute.py) | Wilder ATR(n) for a ticker | `stop_sizer`, `position_sizer` |
| [`trend_template.py`](trend_template.py) | Minervini 8-point check + stage | `regime_check`, setup classification |
| [`regime_check.py`](regime_check.py) | 3-level regime + multiplier + qualify flag | `position_sizer`, gating new entries |
| [`vcp_detect.py`](vcp_detect.py) | VCP detection + contractions + pivot + breakout | SEPA-VCP setup classification |
| [`stop_sizer.py`](stop_sizer.py) | min(ATR×mult, ADR%, 8% Minervini cap) | `position_sizer` |
| [`position_sizer.py`](position_sizer.py) | Shares / capital / stop / binding constraint | risk-and-compliance APPROVE math |

### Phase 2.b — EP (Episodic Pivot) pathway

| Tool | Returns | Used by |
|---|---|---|
| [`prior_rally_pct.py`](prior_rally_pct.py) | 3m + 6m returns + neglected flag | `magna_score`, EP filter |
| [`magna_score.py`](magna_score.py) | MAGNA 0-5 + per-letter breakdown | `ep_grade` |
| [`ep_grade.py`](ep_grade.py) | SuperSwan / Swan / Duck / Chicken / GoldenEP | `position_sizer` (passes through grade) |
| [`earnings_calendar.py`](earnings_calendar.py) | Next earnings date + trading days + blackout flag | EP mandatory-exit, 10-day-blackout hard rule |
| [`ep_detect.py`](ep_detect.py) | gap % + band + intraday expansion + premarket/30min vol | `ep_grade`, EP eligibility |
| [`day7_milestone_check.py`](day7_milestone_check.py) | survives_day7 bool + breach detail | Pyramiding ADD-ON #2 trigger, EP hold extension |

### Phase 2.c.1 — Pyramiding (Anchor-and-Pyramid)

| Tool | Returns | Used by |
|---|---|---|
| [`sltb_scan.py`](sltb_scan.py) | Stockbee Low-Threshold Breakout 6-criterion check | STARTER (Stage-1) trigger |
| [`momentum_burst_detect.py`](momentum_burst_detect.py) | 4%+ on 40%+ volume (or 4%+ gap) trigger | ADD-ON #1 (Stage-2) trigger |
| [`combined_breakeven.py`](combined_breakeven.py) | Weighted-average entry across legs | `position_state`, `add_on_evaluator` stop migration |
| [`position_state.py`](position_state.py) | Lifecycle stage (STARTER / Stage-2 / Stage-3 / closed) | risk-and-compliance verification, journal |
| [`add_on_evaluator.py`](add_on_evaluator.py) | add/skip/no_op + add_shares + new stop | Pyramiding decision composer |

### Phase 2.c.2 — Sell discipline (v1-preliminary)

> ⚠️ All tools in this slice are tagged `v1-preliminary`. Revisit after Minervini book v2 ingestion of [`swing-sell-discipline.md`](https://wiki...) in the vault.

| Tool | Returns | Used by |
|---|---|---|
| [`climax_top_detect.py`](climax_top_detect.py) | Count of 6 climax-top patterns firing | `sell_decision` |
| [`violations_detect.py`](violations_detect.py) | Count of 5 violations + standalone violation-5 flag | `sell_decision` |
| [`base_stage_detect.py`](base_stage_detect.py) | Base count 1-5 + new-high-today flag (heuristic) | `sell_decision` |
| [`pe_expansion_check.py`](pe_expansion_check.py) | P/E doubled? + warning flag | `sell_decision` |
| [`sell_into_strength.py`](sell_into_strength.py) | 10-15% in 2-3 days check + recommended fraction | `sell_decision` |
| [`sell_decision.py`](sell_decision.py) | hold / tighten / sell_1_3 / sell_50 / sell_75 / sell_100 + confidence | Daily position-evaluation composer |

### Phase 2.c.3 — Secondary setups

| Tool | Returns | Used by |
|---|---|---|
| [`pullback_detect.py`](pullback_detect.py) | Pullback-to-20-SMA + hammer/engulfing trigger | Secondary 1 classification |
| [`rsi_divergence.py`](rsi_divergence.py) | Price LL + RSI HL at support + volume confirm | Secondary 2 classification |
| [`resistance_break.py`](resistance_break.py) | Resistance level + decisive break + volume confirm | Secondary 3 classification |

### Phase 3 — Staleness enforcement (Requirement 4)

| Tool | Returns | Used by |
|---|---|---|
| [`freshness.py`](freshness.py) | Per-section fresh/stale + `StalenessError`; market-hours-aware | `risk-and-compliance` pre-APPROVE gate |
| [`stale_phrase_detector.py`](stale_phrase_detector.py) | BLOCK-list scan over agent prose ("as of my training cutoff" etc.) | `risk-and-compliance` output gate |
| [`ledger_freshness_audit.py`](ledger_freshness_audit.py) | Full-ledger sweep; CLI + library | Daily audit, journal trail |

### Phase 4 — Reasoning-trace verification (Requirement 3)

| Tool | Returns | Used by |
|---|---|---|
| [`trace_validate.py`](trace_validate.py) | Structural completeness + targeting; `TraceValidationError` on BLOCK | `risk-and-compliance` pre-APPROVE gate |
| [`trace_rerun.py`](trace_rerun.py) | Re-runs pure-arithmetic tools; shape-checks OHLCV tools; `TraceRerunError` on divergence | `risk-and-compliance` pre-APPROVE gate |
| [`claim_extract.py`](claim_extract.py) | Extracts numeric claims from prose; cross-references against ledger | `risk-and-compliance` output gate (WARN-level) |
| [`trace_audit.py`](trace_audit.py) | Composite APPROVE/BLOCK verdict over all three above; CLI + library | `risk-and-compliance` single entry point |

### Phase 6 — Bias audit (Type 4, periodic ritual)

| Tool | Returns | Used by |
|---|---|---|
| [`bias_audit.py`](bias_audit.py) | Sector + market-cap distribution vs S&P 500 baseline; per-bucket z-scores; flagged buckets at \|z\| >= 2.0; CLI emits TraceEntry JSON or Markdown report | `/bias-audit` slash command; monthly cadence + on-demand |

Addresses Type 4 from `[[llm-financial-hallucination]]` — the only doctrine requirement not covered by the per-trade 5-gate sequence. Bias is structural and persists across trades; per-trade gates can't see systematic skew. The audit walks `ledgers/candidates/YYYY-MM-DD/<TICKER>.yml` over a date window, buckets candidates by sector (via `regime.sector_etf` → GICS) and market cap (from `fundamentals.market_cap_usd`), and flags buckets that deviate >= 2σ from the [`data/universe_baseline.yml`](data/universe_baseline.yml) expected proportions. Informational — never blocks trades.

### Phase 5 — Walk-forward backtest harness

Lives in [`backtest/`](backtest/) — see [`backtest/README.md`](backtest/README.md). Gates setup deployment to live capital on **OOS Sharpe > 1.0 AND |OOS DD| < 25% AND OOS n ≥ 30**.

| Module | Returns |
|---|---|
| [`backtest/data_cache.py`](backtest/data_cache.py) | yfinance fetch + parquet cache; CLI for fetch/info/clear |
| [`backtest/setup_replay.py`](backtest/setup_replay.py) | Walks historical OHLCV day-by-day, fires setup detectors as of each bar; emits `TradeSignal` |
| [`backtest/simulator.py`](backtest/simulator.py) | `TradeSignal` + post-entry OHLCV → `TradeOutcome` (stop/target/max-hold) |
| [`backtest/metrics.py`](backtest/metrics.py) | Sharpe, Sortino, Calmar, max DD, win rate, profit factor, per-grade breakdown |
| [`backtest/walk_forward.py`](backtest/walk_forward.py) | IS/OOS windowing — single-split + rolling-splits + trade partitioning |
| [`backtest/runner.py`](backtest/runner.py) | End-to-end CLI orchestrator; emits Markdown report |

Phase 5.a + 5.b + 5.c ship: 4 active setups (SEPA-VCP, EP, RSI-Divergence, Resistance-Breakout — Pullback-20SMA retired 2026-05-24 per rolling walk-forward sweep; replay function preserved, registration commented out), 3 trail modes (`fixed`, `ratchet`, `ma_trail`), **pyramiding** (`pyramid_simulator` — STARTER + Momentum-Burst ADD-ON #1 + Day-7 ADD-ON #2 with grade/regime gates, combined-BE stop migration), **sell-aware exits** (`sell_aware` — per-bar `sell_decision` composer over OHLCV-derivable detectors), single + rolling walk-forward. Portfolio-equity simulator (concurrent positions + cash + sector caps), pyramid + sell-aware combined, real fundamentals for EP MAGNA, and HTML reports remain Phase 5.d.

Replayability classes for trace re-run:

* **PURE** — pure-arithmetic tools (compute_yoy, stop_sizer, position_sizer, magna_score, ep_grade, combined_breakeven, position_state, add_on_evaluator, sell_into_strength, sell_decision, pe_expansion_check). Re-runs replay the recorded inputs and compare outputs to floating-point tolerance.
* **OHLCV** — tools that consume market-data DataFrames. Re-run does a shape check (required keys present + correct types); full value verification deferred to Phase 5 walk-forward harness because underlying bars advance.
* **MANUAL** — `manual:*` tagged steps (broker_api, sec_filing, etc.) have no programmatic re-run path; verified only as well-formed.

Per-section staleness windows enforced (per doctrine's max-staleness table):

| Section | Window |
|---|---|
| `quote` | 4 h during market hours; until next open if session=closed (weekend-aware) |
| `fundamentals` | Bespoke — warns on filing-date drift + 10-trading-day earnings-blackout proximity + missing secondary source |
| `technical` / `regime` | 24 h |
| `catalyst` | 7 days |
| `earnings_calendar` | 24 h |

### Thematic-portfolio (gate-3 build, parallel to swing-equity stack)

Lives in [`thematic_portfolio/`](thematic_portfolio/). Sibling axis to the swing-equity tools — operates on quarterly-rebalance + event-driven inputs (SA LP 13F + ensemble 13Fs + Loop 1 reasoning output) instead of per-trade OHLCV. Per [[swing-thematic-portfolio-session-2-design-changes]] revisions.

| Tool | Returns | Used by |
|---|---|---|
| [`thematic_portfolio/sizer.py`](thematic_portfolio/sizer.py) | Unified mirror weights: `1.0 × sa_lp_weight × thematic_allocation`, capped at 5% per Q7 | `thematic-portfolio` subagent Loop 1 Pass 3 |
| [`thematic_portfolio/ensemble_overlap.py`](thematic_portfolio/ensemble_overlap.py) | M1 Jaccard (≥0.85 pass) + M3 rank-based ensemble triangulation (≥0.5 consensus health) + per-position critic-trigger context per session-2 #5 pseudocode | Loop 1 Pass 4 + Loop 2 calibration |
| [`thematic_portfolio/corpus/thirteen_f.py`](thematic_portfolio/corpus/thirteen_f.py) | edgartools-wrapped 13F-HR fetcher; normalizes infotable to long-book / put-complex / call-book JSON files (long-book output is directly loadable by the sizer) | Loop 1 input bundle prep + Loop 2 calibration |
| [`thematic_portfolio/corpus/manifest.py`](thematic_portfolio/corpus/manifest.py) | corpus_snapshot composer — walks `ledgers/thematic/corpus/` and packages per-slot paths + recent-artifacts list since prior Loop 1 firing | Loop 1 input bundle prep |
| [`thematic_portfolio/artifact_classifier.py`](thematic_portfolio/artifact_classifier.py) | Substantive-artifact pre-filter (Tier 1 auto-trigger + Tier 3 hard-excludes) + LLM-verdict finalizer + 3/wk rate limit + mandatory-escalation override + firing-log state I/O. Paired with the `thematic-artifact-classifier` Haiku subagent for ambiguous Tier 2/2.5 boundary cases. | `/thematic-portfolio` orchestrator — decides whether incoming artifacts fire Loop 1 |
| [`thematic_portfolio/orchestrator.py`](thematic_portfolio/orchestrator.py) | Loop 1 input-bundle composer + critic-panel aggregator. `compose_loop1_input_bundle()` builds the dict the Loop 1 prompt's "Input contract" expects; `aggregate_critic_outputs()` applies panel rules (structural_risk OR minus_50 → hold; ≥2 minus_20 → weighted reduction; else preserve); `apply_aggregation_to_positions()` walks both. | `/thematic-portfolio` slash command — composition + post-critic aggregation |

Per session-2 design change #6: specific position-fund pairs in design notes are illustrative-only — these tools accept live 13F data per cycle, no constant encodes a specific pair. Per #4: ensemble triangulation is rank-based, NOT notional (Light Street $0.50B vs Coatue $29.06B would drown otherwise).

M2 (critic-outcome alignment over rolling 4q) deferred — requires 4 quarters of accumulated Loop 1 critic decision history; lands in Weeks 5-8 paper-trade phase.

Not yet built: X-timeline fetcher (twitterapi.io, blocked on Bertrand account creation), podcast RSS + Whisper transcription, press feed RSS parsers, Tier 3 real-world signal compilers, put-overlay tracker, kill-switch Process B monitor.

## I/O contract

Every tool exports a pure `compute(...)` (or `compute_from_ohlcv(...)`) returning a [`TraceEntry`](contract.py):

```python
@dataclass
class TraceEntry:
    tool: str          # e.g. "tools/atr_compute.py"
    inputs: dict       # call arguments (re-runnable)
    output: Any        # JSON-serialisable result
    fetched_at: str    # ISO-8601 UTC at moment of compute
    id: int | None     # set when appended to a ledger reasoning_trace
```

Tools that fetch market data also expose `compute_from_ticker(ticker, ...)` which augments `inputs` with provenance (source URL, `data_fetched_at`).

The agent never re-derives output values. It:

1. Calls the tool (CLI or import).
2. Appends the returned `TraceEntry` to the relevant ledger's `reasoning_trace`.
3. Cites the trace step ID in the ledger's `setup_classification.confluence_checklist[].trace_refs`, `position_state.entry_leg.trace_refs`, etc.

Phase 4 verification will re-run each cited tool against the recorded inputs and BLOCK on divergence.

## CLI invocation

Every tool is runnable via `python -m`:

```powershell
# SEPA-VCP pathway
uv run python -m tools.compute_yoy 1.87 1.55
uv run python -m tools.atr_compute AAPL --period 14
uv run python -m tools.trend_template AAPL --rs-rating 87
uv run python -m tools.regime_check AAPL --sector XLK --rs 87
uv run python -m tools.vcp_detect AAPL --weeks 12
uv run python -m tools.stop_sizer --entry 192.74 --atr 4.57
uv run python -m tools.position_sizer --account 150000 --entry 192.74 --atr 4.57 \
    --setup-grade A+ --regime stage_2_confirmed

# EP pathway
uv run python -m tools.prior_rally_pct SMCI --threshold 0.20
uv run python -m tools.earnings_calendar SMCI
uv run python -m tools.ep_detect SMCI
uv run python -m tools.magna_score --eps-yoy 3.92 --sales-yoy 1.74 \
    --after-hours-gap-pct 0.07 --premarket-vol 380000 \
    --gap-confirmed --neglected --analyst-upgrades
uv run python -m tools.ep_grade --magna 5 --gap-pct 0.142 \
    --intraday-expansion-pct 0.062 --earnings-beat --neglected
uv run python -m tools.day7_milestone_check SMCI --entry-date 2026-05-17 --entry-low 405.20

# Pyramiding
uv run python -m tools.sltb_scan NVDA
uv run python -m tools.momentum_burst_detect NVDA
uv run python -m tools.combined_breakeven --leg 20:415.80 --leg 40:448.20 --leg 30:465.40
uv run python -m tools.position_state --starter-shares 20 --starter-price 415.80 \
    --addon1-shares 40 --addon1-price 448.20 --current-price 478.20
uv run python -m tools.add_on_evaluator --stage STARTER \
    --starter-shares 20 --starter-price 415.80 --intended-shares 60 \
    --triggered --setup-grade GoldenEP --regime stage_2_confirmed \
    --current-price 430.00

# Sell discipline (v1-preliminary)
uv run python -m tools.climax_top_detect NVDA
uv run python -m tools.violations_detect NVDA --entry-date 2026-05-17
uv run python -m tools.base_stage_detect NVDA
uv run python -m tools.pe_expansion_check --baseline 18.0 --current 38.0
uv run python -m tools.sell_into_strength --gain-pct 0.12 --days 2 --grade GoldenEP
# sell_decision: library import only (no CLI; orchestrator-level composer)

# Secondary setups
uv run python -m tools.pullback_detect MSFT
uv run python -m tools.rsi_divergence NVDA
uv run python -m tools.resistance_break AVGO

# Phase 3 — staleness enforcement
uv run python -m tools.ledger_freshness_audit ledgers/_examples/sepa-vcp-candidate.yml
uv run python -m tools.ledger_freshness_audit ledgers/positions/AAPL.yml --asof 2026-05-18T14:30:00Z
"As of late 2024, NVDA was at $850." | uv run python -m tools.stale_phrase_detector -
uv run python -m tools.stale_phrase_detector journal/2026-05-18.md
# freshness.py: library import only — used inside risk-and-compliance verification

# Phase 4 — reasoning-trace verification
uv run python -m tools.trace_audit ledgers/_examples/sepa-vcp-candidate.yml
uv run python -m tools.trace_audit ledgers/positions/SMCI.yml --report researcher-report.md
uv run python -m tools.claim_extract --report researcher-report.md --ledger ledgers/positions/SMCI.yml
# trace_validate.py + trace_rerun.py: library imports only — call from risk-and-compliance
```

stdout is the `TraceEntry` as indented JSON — slottable directly into a ledger.

## Library invocation

```python
from tools.atr_compute import compute_from_ticker
from tools.position_sizer import compute as size_position

atr_entry = compute_from_ticker("AAPL", period=14)
size_entry = size_position(
    account=150_000.0,
    entry_price=192.74,
    atr=atr_entry.output["atr"],
    setup_grade="A+",
    regime_class="stage_2_confirmed",
)
print(size_entry.output["shares"], size_entry.output["binding_constraint"])
```

## Data source

[`data.py`](data.py) wraps yfinance. This is the Phase 2 default — free, no API key, OHLCV + fundamentals + earnings dates. The wrapper is the only abstraction point if we later add Alpaca / IBKR.

## Testing

```powershell
uv run pytest
```

376 tests pass in ~1.5 s. All synthetic — no network. Fixtures in [`tests/conftest.py`](../tests/conftest.py). Both curated example ledgers (`sepa-vcp-candidate.yml`, `pyramided-position.yml`) audit clean through `trace_audit`. Phase 5 backtest modules tested end-to-end against synthetic OHLCV — runner CLI requires network for real backtests.

### Red-team regression harness (added 2026-05-23)

[`tests/test_red_team_gates.py`](../tests/test_red_team_gates.py) — 27 adversarial tests over the 5-gate hallucination-prevention sequence. Probes each gate with deliberately-malformed inputs that should BLOCK; passes when the gate catches them. A failing red-team test = the gate has a regressed leak.

The harness uncovered and closed 19 leaks in its first run:

- **Gate 1 (`ledger_freshness_audit`)** — missing sections and missing timestamps were silently treated as fresh. Patched via `freshness.REQUIRED_SECTIONS` + the new "fresh iff every section is fresh" verdict rule.
- **Gate 2 (`trace_audit`)** — ledgers with no load-bearing section, all-UNKNOWN confluence checklists, and wrong-type checklists passed silently. Patched via new `no_load_bearing_section` BLOCK, `confluence_checklist_wrong_type` BLOCK, and `all_unknown_confluence` WARN codes in `trace_validate`.
- **Gate 3 (`stale_phrase_detector`)** — 11 paraphrase families plus NBSP/newline escapes defeated the original 6-pattern catalog. Patched by expanding to 15 BLOCK + 2 WARN patterns and using `\s+` instead of literal space.

Convention: `test_hit_<gate>_<vector>` for caught attacks; `test_leak_<gate>_<vector>` for known-open attacks (none currently). When a new attack vector is discovered, add it as `test_leak_*` to track it; flip to `test_hit_*` when closed.

Real-data correctness is validated by running the CLI against live tickers and comparing against the worked examples in the operational notes:

- AAPL A+ example in [`sepa-vcp-candidate.yml`](../ledgers/_examples/sepa-vcp-candidate.yml) → `position_sizer` reproduces 194 shares / $1,773 risk / concentration cap binds.
- SMCI Golden EP example in [`ep-golden-candidate.yml`](../ledgers/_examples/ep-golden-candidate.yml) → `magna_score` returns 5/5; `ep_grade` returns `GoldenEP` with rationale "Swan/SuperSwan + gap in 10-19% sweet spot + expansion ≥ 5%".

## Phases 1–5.c complete — what's next

44 modules (31 tools + 13 backtest modules) + 322 tests. Phases 1 (fact ledger), 2 (arithmetic), 3 (staleness), 4 (reasoning-trace verification), 5.a + 5.b (5-setup backtest with trail modes + rolling walk-forward), 5.c (pyramiding + sell-aware exits) shipped. Ahead:

- **First real-data backtest runs.** Fetch 5y OHLCV and run all combinations: 5 setups × 3 trail modes × {plain, pyramid, sell-aware}. Iterate on the deployment gate.
- **Phase 5.d — portfolio-equity simulator + real fundamentals for EP MAGNA + pyramid×sell-aware combined + HTML reports.** Concurrent-position cash tracking with sector caps is the highest-leverage Phase 5.d piece; without it the backtest assumes infinite capital.
- **v2 sell-discipline upgrade.** Trigger: Minervini book ingest into the vault.

## Phase 2 caveats flagged in code

- `Phase 2 baseline: refine after walk-forward calibration` — `vcp_detect.py` thresholds (swing-window, breakout-volume ratio). Final tuning belongs to Phase 5 walk-forward validation.
- `v1-preliminary: revisit after Minervini book v2 ingestion` — every sell-discipline tool will carry this marker when shipped.

## Related

- [`../ledgers/README.md`](../ledgers/README.md) — Phase 1 fact-ledger schema (what these tools write into)
- [`../CLAUDE.md`](../CLAUDE.md) § Tools — operating contract for subagents
- Vault: `swing-risk-compliance-doctrine.md` (4 requirements), `swing-position-sizing.md`, `swing-regime-playbook.md`, `swing-setup-library.md`
