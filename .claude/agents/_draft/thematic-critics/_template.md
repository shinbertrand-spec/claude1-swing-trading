# Adversarial-critic prompts — template + invocation contract

This directory holds the 7 adversarial-critic persona prompts that downstream consumers of `thematic-portfolio` Loop 1 output dispatch. **All prompts are DRAFT (2026-05-25).** They will move out of `_draft/` when the `/thematic-portfolio` slash command + critic-dispatch orchestrator ship (Weeks 3-4 of the gate-3 build).

## Critic panel

| File | Critic | Role | Fires on |
|---|---|---|---|
| [`marcus.md`](marcus.md) | Gary Marcus | Core — timeline-deflater | Every position |
| [`mechanize-epoch.md`](mechanize-epoch.md) | Mechanize / Epoch alumni | Core — share-the-math reject-the-doom | Every position |
| [`lecun.md`](lecun.md) | Yann LeCun | Core — architecture skeptic | Every position |
| [`friedman-extended.md`](friedman-extended.md) | Dave Friedman (extended per session-2 #8) | Core — physical-infra + small-cap displacement | Every position |
| [`thorstad.md`](thorstad.md) | David Thorstad | Core — formal-theory inside critique | Every position; high-weight on frontier-AI-capability longs |
| [`patel.md`](patel.md) | Dylan Patel (SemiAnalysis) | Specialist | Memory / storage positions (SNDK, MU, any added) |
| [`rasgon.md`](rasgon.md) | Stacy Rasgon (Bernstein) | Specialist | Memory / storage positions (same trigger as Patel) |

Deferred: miner-pivot execution critic. Anchor persona TBD; lands after first material miner-pivot drawdown surfaces empirical critique frame. See [[swing-thematic-portfolio-session-2-design-changes]] § 9 + § Deferred-critic post-hoc contract.

Future-optional (post-v1): Yudkowsky (7th alignment-skeptic), BG2/Gerstner (real-time articulator).

## Common invocation contract (every critic)

### Input (orchestrator passes to each critic)

```yaml
position:
  # Single entry from Loop 1 output's positions[] array — see thematic-portfolio.md output contract
  ticker: <string>
  name: <string>
  sector: <string>
  sa_lp_weight_pct_of_long_book: <float>
  target_weight_pct_of_total: <float>
  conviction_tier: boost | sa_lp_only
  ensemble_holds: [funds]
  ensemble_exits: [funds]
  thorstad_frame_check: { risk_channel, structural_risk_adjustment_applicable, ... }
  regime_position_logic: <string>
  rationale: <string>
  source_artifacts: [...]

loop1_context:
  regime: { classification, confidence, implication_for_book }
  thesis_state_summary: <string>                       # Pass 1 output condensed
  short_overlay_bias_flag: { fired, loop3_recommendation }

critic_trigger_context:
  trigger_rule: ensemble_disagreement | sa_lp_doubling_down_vs_consensus_exit | non_consensus_sa_lp_solo | none
  context_summary: <string>                            # from Loop 1 Pass 4

caller_metadata:
  loop1_firing_id: <string>                            # for downstream tracing
  critic_call_id: <string>
```

### Output (every critic emits this exact shape)

```json
{
  "critic": "marcus | mechanize_epoch | lecun | friedman_extended | thorstad | patel | rasgon",
  "position_ticker": "<string>",
  "loop1_firing_id": "<string>",
  "critic_call_id": "<string>",
  "risks": [
    {
      "risk": "<one sentence>",
      "grounding_citation": "<exact claim from the critic's published views that grounds this risk; cite source>",
      "severity": "low | medium | high"
    }
  ],
  "confidence_adjustment": "hold | minus_20 | minus_50 | structural_risk",
  "adjustment_rationale": "<one paragraph synthesizing the risks into the recommendation>",
  "estimated_cost_usd": <float>
}
```

### Aggregation rules (orchestrator runs after collecting all critic outputs)

Per Q5 ([[swing-thematic-portfolio-subagent-research]]) + session-2 design change #2:

1. **Any single `structural_risk`** → position goes to `hold_pending_bertrand_review`. Skip the rest of aggregation for this position.
2. **Any single `minus_50` reduction** → position goes to `hold_pending_bertrand_review`. Skip the rest.
3. **≥ 2 critics output `minus_20` or worse** → apply weighted reduction:
   `adjusted_target_pct = loop1_target_pct × (1 - average_of_minus_20s)`
   where each `minus_20` contributes 0.20 to the average and each `minus_50` contributes 0.50.
4. **Otherwise (≤ 1 critic at minus_20, none worse)** → preserve Loop 1 target weight; log critic concerns but do not size-adjust.

The aggregated result feeds back into the final `thematic-portfolio` output that Bertrand reviews.

## Model + cost

- **Model:** `claude-haiku-4-5-20251001` (per kickoff doc Weeks 2-3 cost model — Haiku for per-position critic passes; Opus for Loop 1 reasoning extraction).
- **Per-critic cost:** estimated $0.05-$0.15 per position pass (Haiku is cheap; persona prompts are ~150-250 lines + position context is small).
- **Per-Loop-1 cost:** at 5 core critics × ~20 positions + 2 specialists × ~3 memory positions ≈ 106 critic calls × ~$0.10 ≈ $11/firing. Add Loop 1's $3-8 reasoning extraction → ~$14-19/firing total. Within the $5-15 target band most firings; high-tail is acceptable on full-corpus refreshes.

## Hard constraints (every critic file enforces these)

1. **You are the assigned critic, full stop.** Do not soften the persona to "balance" the panel. The aggregation step in the orchestrator handles balance; your job is sharp adversarial review from a specific frame.
2. **Cite specific published views.** "Marcus would say X" is not a citation. The grounding_citation field requires an actual claim from the source URL anchor in the persona's prompt.
3. **No fabricated quotes.** If you don't have a real published statement that grounds a risk, OMIT that risk. A 1-risk output is fine; a 3-risk output with one fabricated quote is a fatal error.
4. **Adjust to the SPECIFIC position, not the SA LP thesis in general.** If the Loop 1 position is BE (Bloom Energy) at 5% of portfolio, your risks must engage BE specifically — distributed-power-generation economics, fuel-cell tech curves, etc. Generic "AI capex might disappoint" attacks every position equally and provide no signal.
5. **Engage the conviction_tier + ensemble_holds context.** A `non_consensus_sa_lp_solo` position deserves sharper critique than a `boost`-tier multi-fund consensus position. The trigger_rule field tells you which.
6. **Output JSON, period.** No preamble, no explanation outside the JSON envelope.

## Versioning

Per [[swing-thematic-portfolio-adversarial-critics]] § operational notes: persona prompts are static until the critic publishes materially new positions. Quarterly review for prompt updates is sufficient. **Do not blend old + new statements** — when a critic publishes a substantive update, re-derive the persona prompt cleanly.

Track persona-prompt versions in each file's frontmatter `persona_anchor_version` field (currently `2026-05-25-v1` for all 7 prompts).
