# Swing-trade adversarial-critic prompts — template + invocation contract

This directory holds the per-pick adversarial-critic personas for the **swing-trade** track. They are the Phase 3 multi-rater panel that fires on every quant-scanner-produced paper-auto candidate (and is also available for human-discretionary picks via the same contract). Modeled on `.claude/agents/thematic-critics/_template.md` — that critic stack handles the thematic-portfolio track; this one handles swing-trade picks.

The panel runs **in tandem with** (not as a replacement for) the existing `trade-skeptic` subagent (single-bear, Phase 2) and Gate 6 `debate_synthesis` (bull/bear → SwingVerdict). It produces an additional set of votes that the aggregator (`tools.auto_paper.critic_panel`) composes into a sizing recommendation — never a binary block.

## Critic panel

| File | Critic | Role | Fires on |
|---|---|---|---|
| [`risk-manager.md`](risk-manager.md) | The Risk Manager | Core — concentration / correlation / gap risk / liquidity stress | Every candidate |
| [`setup-quality-hawk.md`](setup-quality-hawk.md) | The Setup-Quality Hawk | Core — Minervini-style chart confluence; distribution character; premature breakouts | Every candidate |
| [`macro-skeptic.md`](macro-skeptic.md) | The Macro Skeptic | Core — Fed/yield curve; sector rotation; broad-market Stage 3/4 risks | Every candidate |
| [`quant-insight.md`](quant-insight.md) | The Quant Insight | Specialist — signal rank within rebalance + historical percentile vs backtest | Quant-scanner candidates only |

**Reuse of existing thematic critics on swing semi picks** (per Phase 3 scope decision 2026-05-27):

When a swing candidate's `sector_etf ∈ {XLK, XSD}` AND `industry` contains "Semiconductor", the swing orchestrator ALSO dispatches `thematic-critic-patel` + `thematic-critic-rasgon` against the same swing-context input dict. Those critics' prompts are unchanged — they emit the same JSON output shape this template defines, and their votes feed the same aggregator. No new agent files required for the reuse.

Future-optional (post-MVP, after one calibration cycle): Fundamentalist + Position Critic (deferred per Phase 3 plan).

## Common invocation contract (every critic)

### Input (orchestrator passes to each critic)

```yaml
candidate:
  # Derived from tools.auto_paper.pipeline.CandidateInput + screener + shell-ledger
  ticker: <string>
  setup_type: <string>            # e.g. "xs_short_term_reversal", "clenow_momentum_liquid_us", "VCPBreakout"
  setup_grade: <string | null>    # A+/A/B/C if discretionary; null for quant picks
  pivot_price: <float>
  stop_price: <float>
  stop_distance_pct: <float>      # (pivot - stop) / pivot
  sector_etf: <string>            # corrected by screener if mismatched
  sector_industry: <string>       # yfinance industry string from screener sector_lookup
  shares: <int>                   # pre-sized by quant_scanner or position_sizer
  source: "quant_scanner" | "morning_scan"

ledger_context:
  # From shell_ledger or trade-researcher candidate ledger
  ledger_path: <string>           # ledgers/candidates/YYYY-MM-DD/<TICKER>.yml
  bull_report_path: <string>      # ledgers/candidates/YYYY-MM-DD/<TICKER>.md (stub or full)
  bear_report_path: <string | null>  # ledgers/candidates/YYYY-MM-DD/<TICKER>-bear.md if trade-skeptic already ran
  regime_summary:
    broad_market_stage_class: <string>     # e.g. "stage_2_confirmed"
    sector_stage_class: <string>
    candidate_trend_template_passes: <int> # 0-8
    candidate_stage: <int>                 # Weinstein 1-4
    regime_multiplier: <float>             # lever-D multiplier
  atr_14: <float | null>                   # in dollars
  next_earnings_date: <string | null>      # ISO date, from screener
  screener_summary:
    blocked: <bool>                         # always False if we got here
    corrected_sector_etf: <string | null>
    blocking_checks: []
  signal_rank: <int | null>                # quant-scanner picks only: rank within rebalance
  signal_percentile: <float | null>        # 0-1; quant-scanner only
  signal_score: <float | null>             # raw strategy score (Clenow slope*R², etc.)

portfolio_context:
  # State of paper-auto track at panel time
  existing_positions:
    - { ticker, sector_etf, shares, entry_price, stage }
  total_at_cost: <float>
  cash_buffer_pct: <float>
  position_count: <int>
  net_liquidation: <float>

panel_metadata:
  panel_call_id: <string>                  # YYYY-MM-DDTHH-MM__<TICKER>__<CRITIC>
  panel_firing_date: <ISO date>            # for the per-day ledger directory
  shadow_mode: <bool>                      # True until calibration data accumulates
```

### Output (every critic emits this exact shape)

```json
{
  "critic": "risk_manager | setup_quality_hawk | macro_skeptic | quant_insight | patel | rasgon",
  "candidate_ticker": "<string>",
  "panel_call_id": "<string>",
  "panel_firing_date": "<string>",
  "risks": [
    {
      "risk": "<one sentence, candidate-specific>",
      "grounding_evidence": "<exact ledger field path OR tool output OR portfolio_context fact that grounds this risk>",
      "severity": "low | medium | high"
    }
  ],
  "confidence_adjustment": "hold | minus_20 | minus_50 | structural_risk",
  "adjustment_rationale": "<one paragraph synthesizing the risks into the recommendation, in the critic's voice>",
  "estimated_cost_usd": <float>
}
```

**Note on grounding:** swing critics are role-based personas (Risk Manager, Setup-Quality Hawk, etc.), not real-person-grounded like the thematic critics (Marcus, LeCun, Patel). So the field is `grounding_evidence` rather than `grounding_citation` — point to a ledger field (`regime.candidate_trend_template_passes=2`), a tool output (`vcp_detect: detected=False`), or a portfolio fact (`portfolio_context.existing_positions[3].ticker=VRT, total VRT exposure compounds`). Patel + Rasgon (reused thematic critics) keep their original `grounding_citation` to published views — the aggregator accepts either field name.

### Aggregation rules (orchestrator runs after collecting all critic outputs)

Mirroring `tools/thematic_portfolio/orchestrator.aggregate_critic_outputs` priority order. Implemented in `tools/auto_paper/critic_panel.aggregate_panel`:

1. **Any single `structural_risk`** → `action="defer"`, `sizing_multiplier=0.0`. The candidate is NOT placed today; surfaces for manual review tomorrow.
2. **Any single `minus_50`** → `action="half_size_review"`, `sizing_multiplier=0.5`. Place at half size and flag for review.
3. **≥ 2 critics output `minus_20`** → `action="reduce_20"`, `sizing_multiplier=0.8`. Place at 80% size.
4. **Otherwise (≤ 1 critic at `minus_20`, none worse, all rest `hold`)** → `action="preserve"`, `sizing_multiplier=1.0`. Place at full size; log concerns.

**Shadow mode** (default `True` until 2026-06-10 or until calibration data supports lifting): the aggregator still computes `action` + `sizing_multiplier`, but `pipeline.place_candidate` ignores `sizing_multiplier` and places at unmodified size. The verdict surfaces in the Telegram summary and in `ledgers/swing-critics/_calibration/` for offline analysis.

## Model + cost

- **Model:** `claude-haiku-4-5-20251001` (per Phase 3 scope decision — same model as thematic critics).
- **Per-critic cost:** estimated $0.05-$0.15 per pick (Haiku 4.5; persona prompts are ~120 lines + candidate context is small).
- **Per-cron-firing cost:** at 3 core critics + occasional specialists × 4-8 candidates ≈ 12-30 critic calls × ~$0.10 ≈ $1-3 per cron firing. At 22 trading days/month: **~$40-60/month** total.

## Hard constraints (every critic file enforces these)

1. **You are the assigned critic, full stop.** Do not soften the persona to "balance" the panel. The aggregator handles balance; your job is sharp adversarial review from a specific lens.
2. **Cite specific ledger fields or tool outputs.** "The setup looks weak" is not grounding. The `grounding_evidence` field requires a specific ledger field path (`setup_classification.confluence_checklist[2].status=FAIL`), a tool output identifier (`tools.vcp_detect: detected=False`), or a portfolio fact (`portfolio_context.position_count=7`). Fabricated facts are a fatal error.
3. **Engage the SPECIFIC candidate, not the strategy in general.** If the candidate is VRT via xs_short_term_reversal, your risks must engage VRT specifically — its pullback character, its sector overlap with existing positions, its ATR-vs-stop relationship. Generic "mean-reversion has tail risk" attacks every reversal pick equally and provides no signal.
4. **Engage the `portfolio_context`.** A candidate that adds correlated exposure to an existing position deserves sharper critique than a clean-diversification add. The orchestrator passes you the live portfolio state — use it.
5. **Output JSON, period.** No preamble, no explanation outside the JSON envelope.
6. **Severity calibration:**
   - `low`: the risk exists but doesn't change the trade economics materially.
   - `medium`: the risk would cause this trade to underperform the strategy's backtest mean.
   - `high`: the risk would cause this trade to be a meaningful loser if it materialises.
   - Map to `confidence_adjustment`: 0 high + ≤1 medium → `hold`; 1 high OR 2+ medium → `minus_20`; 2+ high OR clear thesis-contradiction → `minus_50`; setup-fundamentally-broken-as-presented → `structural_risk`.

## Versioning

Per-critic prompts are static until the calibration data suggests a change in priority weights or persona scope. The first calibration review lands after 1-2 weeks of shadow-mode data accumulation (~target 2026-06-10).

Track persona-prompt versions in each file's frontmatter `persona_anchor_version` field (currently `2026-05-27-v1` for the swing panel).