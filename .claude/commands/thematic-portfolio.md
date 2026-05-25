---
description: /thematic-portfolio — orchestrator for the thematic-portfolio subagent stack. Refreshes 13Fs (SA LP + ensemble) + corpus manifest, composes the Loop 1 input bundle, dispatches the Loop 1 reasoning-layer subagent, then dispatches the 5-core + position-gated specialist critic panel, aggregates per the panel-aggregation rules, and presents the final adjusted positioning to Bertrand. Paper-only (refuses to touch live capital until Q3 2026 second calibration cycle). Two trigger modes - `--monthly-base` and `--artifact <path>`. Supports `--dry-run`. NEVER auto-executes trades - all recommendations are advisory until Bertrand explicitly confirms.
---

# /thematic-portfolio — Loop 1 orchestrator

You are running the thematic-portfolio subagent stack end-to-end for one firing.

**Track separation.** Everything written here goes to the parallel thematic track (`ledgers/thematic/*` + `journal/thematic-portfolio/*`). The swing-equity track (`journal/positions.json`) is untouched. The paper-auto track (`ledgers/paper-auto/*`) is untouched.

**Safety properties (invariants):**
- **Paper-only.** All recommendations are advisory; nothing places trades automatically.
- **No Process B modification.** This command operates within Process A only. The kill-switch (Process B) — when it ships — is independent and CANNOT be paused, slowed, or commented on from this flow.
- **Hard refusal on live capital.** If `$ARGUMENTS` contains `--live`, REFUSE and exit immediately. Per the gate-3 doctrine, live capital deployment is gated on Q3 2026 second calibration cycle + manual Bertrand approval, NEVER by this orchestrator.

## $ARGUMENTS parsing

Required (exactly one):
- `--monthly-base` — fire Loop 1 on the monthly base cadence (typically first trading day of month at 9:30 AM ET).
- `--artifact <json-path>` — fire Loop 1 in response to a substantive artifact. The JSON file at `<json-path>` must have the shape: `{source, snippet, url, snippet_length_chars, is_thread, thread_total_words, author_handle, fetched_at}` per the classifier's `ArtifactInput` contract.

Optional:
- `--dry-run` — compose the input bundle + show what WOULD fire, without invoking Loop 1 or any critic. Useful for input-shape verification before a real firing.
- `--allocation-pct <N>` — override the current Loop 5 phase allocation. Defaults: read from `journal/thematic-portfolio/state.json` or fall back to 10 (phase 1). Must be one of `10`, `15`, `25`.
- `--sa-lp-period <YYYY-MM-DD>` — period_of_report for the SA LP 13F. Defaults to the most recent filed period available via edgartools.
- `--prior-sa-lp-period <YYYY-MM-DD>` — period for the prior SA LP 13F (used by critic-trigger context for cross-period drift). Defaults to one quarter before `--sa-lp-period`.

## Hard refusals (exit immediately with the message; do NOT proceed)

- `$ARGUMENTS` contains `--live` → `THEMATIC_REFUSE_LIVE — live capital is gated on Q3 2026 calibration + manual Bertrand approval. Use the paper-only flow.`
- `$ARGUMENTS` contains neither `--monthly-base` nor `--artifact` → `THEMATIC_USAGE — must pass exactly one of --monthly-base / --artifact <path>.`
- `$ARGUMENTS` contains both `--monthly-base` AND `--artifact` → `THEMATIC_USAGE — --monthly-base and --artifact are mutually exclusive.`

## Step 0 — Determine fired_at + read prior firing log

`fired_at` = current ISO-8601 UTC timestamp (use `python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec='seconds'))"` if you cannot derive it otherwise).

Read the firing log + current allocation:

```python
from pathlib import Path
from tools.thematic_portfolio.artifact_classifier import load_firing_log, DEFAULT_FIRING_LOG_PATH
from tools.thematic_portfolio.orchestrator import find_prior_loop1_output

firing_log = load_firing_log(DEFAULT_FIRING_LOG_PATH)
n_firings_in_window = sum(
    1 for f in firing_log.get("firings", [])
    # Count firings in past 7 days; details in artifact_classifier.apply_rate_limit
)
prior_loop1_path = find_prior_loop1_output(Path("ledgers/thematic/loop1"))
```

## Step 1 — Substantive-artifact classifier (event-driven only)

**SKIP this step entirely if `--monthly-base` was passed.**

For `--artifact <path>`:

1. Read the artifact JSON. Construct an `ArtifactInput` dataclass.
2. Run the deterministic pre-filter:

   ```python
   from tools.thematic_portfolio.artifact_classifier import pre_filter, ArtifactInput
   artifact = ArtifactInput(**artifact_json_dict)
   pre_filter_result = pre_filter(artifact)
   ```

3. If `pre_filter_result is None`, invoke the `thematic-artifact-classifier` subagent via the Agent tool with the artifact + pre-filter context. The subagent returns JSON; capture it.

   ```
   Agent({
       subagent_type: "thematic-artifact-classifier",
       prompt: "<the full artifact ArtifactInput dict + pre_filter_context: deterministic_tier=null, pre_filter_notes=...>",
       description: "Classify ambiguous Tier 2/2.5 boundary artifact"
   })
   ```

4. Run the full pipeline (handles rate limit + escalation):

   ```python
   from tools.thematic_portfolio.artifact_classifier import classify_pipeline
   trace = classify_pipeline(
       artifact,
       llm_verdict=<subagent JSON if pre_filter returned None else None>,
       persist=False,   # we'll persist AFTER Loop 1 actually fires
   )
   classification = trace.output["classification"]
   rate_limit_decision = trace.output["rate_limit_decision"]
   ```

5. **If `rate_limit_decision["fired"] is False`**: exit cleanly with a summary noting tier + rationale. Do NOT invoke Loop 1 or critics. Add a one-line entry to the journal so the queued-for-monthly-base case is visible.

6. Capture for later:
   - `triggering_artifact = {source, url, tier, snippet}` from the artifact JSON
   - `mandatory_escalation = rate_limit_decision["mandatory_escalation_applied"]`

## Step 2 — Refresh 13Fs (SA LP + ensemble)

For both monthly-base and event-driven, refresh the 13F data before composing the input bundle. Skip in `--dry-run` if the JSON files already exist (i.e. there's stable data on disk you want to test against).

```bash
uv run python -m tools.thematic_portfolio.corpus.thirteen_f \
    --ensemble \
    --period <args.sa_lp_period or most-recent-filed> \
    --out-dir-root ledgers/thematic/13f
```

If `--prior-sa-lp-period` was passed (or you computed a default), also refresh prior-period long-books for SA LP + each ensemble fund. The orchestrator's `compute_critic_trigger_context()` needs both periods to detect ensemble exits.

```bash
uv run python -m tools.thematic_portfolio.corpus.thirteen_f \
    --ensemble \
    --period <prior_sa_lp_period> \
    --out-dir-root ledgers/thematic/13f
```

(The thirteen_f module writes per-fund subdirectories, so calling twice with different periods produces parallel `{fund}/{cik}-{period}-long.json` files.)

## Step 3 — Refresh corpus manifest

```bash
uv run python -m tools.thematic_portfolio.corpus.manifest \
    --corpus-root ledgers/thematic/corpus \
    --since <prior_loop1_fired_at or "1970-01-01T00:00:00Z" if first ever>
```

Capture the TraceEntry's `output` dict — that's the corpus_snapshot to embed in the Loop 1 input bundle.

## Step 4 — Read portfolio state

Read current thematic-track state. v1 storage location: `journal/thematic-portfolio/state.json` (create the directory if missing). If the state file doesn't exist, treat it as a first-ever firing with empty positions:

```python
from tools.thematic_portfolio.orchestrator import PortfolioState

state_file = Path("journal/thematic-portfolio/state.json")
if state_file.exists():
    state_dict = json.loads(state_file.read_text())
else:
    # First-ever firing — paper-only, no positions placed yet
    state_dict = {
        "thematic_allocation_pct": <args.allocation_pct or 10.0>,
        "current_loop5_phase": "phase1_10pct",
        "current_thematic_positions": []
    }

# Read Tiger paper account NAV for total_portfolio_nav_usd
from tools.broker.tiger import TigerClient
client = TigerClient()      # paper-routed by default; refuses live
summary = client.account_summary().output
nav = summary.get("net_liquidation") or summary.get("cash") or 1_000_000.0  # SDK quirk: paper may report Infinity

portfolio_state = PortfolioState(
    thematic_allocation_pct=state_dict["thematic_allocation_pct"],
    current_loop5_phase=state_dict["current_loop5_phase"],
    total_portfolio_nav_usd=float(nav) if nav != float("inf") else 1_000_000.0,
    current_thematic_positions=state_dict.get("current_thematic_positions", []),
)
```

## Step 5 — Compose the Loop 1 input bundle

Steps 3 + 4 + 5 are wrapped by [`tools.thematic_portfolio.orchestrator.build_live_bundle`](../../tools/thematic_portfolio/orchestrator.py) — it reads the corpus manifest, the thematic-track state file, fetches the live Tiger paper NAV, builds SA LP + ensemble `FilingPaths`, and calls `compose_loop1_input_bundle` for you. The function defaults match the on-disk layout this skill produces in Step 2, so the typical call is short:

```python
from pathlib import Path
from tools.thematic_portfolio.orchestrator import build_live_bundle

bundle = build_live_bundle(
    trigger_type="monthly_base" if monthly_base else "substantive_artifact",
    fired_at=fired_at,
    sa_lp_period=sa_lp_period,
    prior_sa_lp_period=prior_sa_lp_period,
    triggering_artifact=triggering_artifact if not monthly_base else None,
    rate_limit_consumed_this_week_before_firing=n_firings_in_window,
    mandatory_escalation=mandatory_escalation if not monthly_base else False,
    sa_lp_filed_date=<from thirteen_f output for sa_lp>,
    ensemble_filed_dates={fund: <from thirteen_f output> for fund in ("altimeter", "coatue", "light_street")},
    allocation_pct_override=args.allocation_pct,  # None to use state.json or default 10
)
```

If you need lower-level control (different ensemble funds, custom corpus root, mock NAV for a dry-run-without-broker), use `compose_loop1_input_bundle` + `FilingPaths` + `PortfolioState` directly — `build_live_bundle` is just the common-case wrapper.

## Step 6 — `--dry-run` early exit

If `--dry-run` was passed:

1. Pretty-print the bundle.
2. Write it to `ledgers/thematic/loop1/_dryrun/<fired_at>.json` for inspection.
3. Exit with a one-line summary: `THEMATIC_DRY_RUN — bundle composed; would invoke Loop 1 with N positions in SA LP long book, K ensemble funds, M corpus artifacts.`

## Step 7 — Invoke Loop 1 (thematic-portfolio subagent)

```
Agent({
    subagent_type: "thematic-portfolio",
    prompt: "<entire bundle YAML/JSON serialized + 'Compose the Loop 1 output per the Loop 1 prompt's Output contract section.'>",
    description: "Loop 1 reasoning-layer firing"
})
```

The subagent returns the full Loop 1 output JSON. Validate it:

- `meta.state == "ok"` (if "rejected", exit with the validation failures surfaced)
- `validation.paper_only_enforced == true` (hard refusal otherwise)
- `validation.no_put_replication_recommended == true`
- `validation.allocation_within_loop5_phase == true`
- `validation.no_process_b_modification == true`
- Every `positions[].source_artifacts` is non-empty

Write the JSON to `ledgers/thematic/loop1/<fired_at>.json` and the Markdown mirror to `ledgers/thematic/loop1/<fired_at>.md`.

## Step 8 — Dispatch critic panel

For each `position` in `loop1_output.positions`:

1. **Always invoke the 5 core critics** in parallel:
   - `thematic-critic-marcus`
   - `thematic-critic-mechanize-epoch`
   - `thematic-critic-lecun`
   - `thematic-critic-friedman-extended`
   - `thematic-critic-thorstad`

2. **If `position.critic_trigger_context.specialist_gating` contains `"patel"`, also invoke** `thematic-critic-patel`.

3. **If `specialist_gating` contains `"rasgon"`, also invoke** `thematic-critic-rasgon`.

Each critic invocation takes the position dict + `loop1_context: {regime, thesis_state_summary, short_overlay_bias_flag}` + the `critic_trigger_context` block. Returns JSON per the critic template.

**IMPORTANT — dispatcher writes critic JSONs, not the critic.** Critic subagents have `tools: Read, Glob` only (no Write tool by design — keeps the persona prompt cheap to spin up and prevents critics from clobbering each other's output). Each critic emits the JSON inline in its returned message; YOU (this orchestrator) extract the JSON block from the agent's return value and persist it to `ledgers/thematic/loop1/<fired_at>__critic_outputs/<ticker>__<critic>.json` via the `Write` tool. Make critic-input bundles available to each critic at `ledgers/thematic/loop1/<fired_at>__critic_inputs/<ticker>.json` (one per position) and pass the path in the prompt — that's cheaper than embedding the full position+context in every dispatch message.

Dispatch ALL critics for ALL positions in parallel (one Agent() per critic-per-position; this is the right place to use the parallel Agent dispatch pattern). After all return, batch the `Write` calls to persist.

## Step 9 — Aggregate critic outputs

```python
from tools.thematic_portfolio.orchestrator import apply_aggregation_to_positions

critic_outputs_by_ticker = {}
for path in Path(f"ledgers/thematic/loop1/{fired_at}__critic_outputs/").glob("*.json"):
    data = json.loads(path.read_text())
    ticker = data["position_ticker"]
    critic_outputs_by_ticker.setdefault(ticker, []).append(data)

decisions = apply_aggregation_to_positions(
    loop1_positions=loop1_output["positions"],
    critic_outputs_by_ticker=critic_outputs_by_ticker,
)
```

Write the aggregated decisions to `ledgers/thematic/loop1/<fired_at>__aggregated.json`.

## Step 10 — Persist firing-log record

Only NOW (after the firing actually completed end-to-end):

```python
from tools.thematic_portfolio.artifact_classifier import record_firing
record_firing(
    firing_log_path=DEFAULT_FIRING_LOG_PATH,
    fired_at=fired_at,
    trigger_type=("monthly_base" if monthly_base else "substantive_artifact"),
    triggering_artifact=triggering_artifact if not monthly_base else None,
    mandatory_escalation=mandatory_escalation if not monthly_base else False,
    loop1_firing_id=fired_at,
)
```

## Step 11 — Present to Bertrand

Emit a Markdown summary covering:

```
# Thematic-portfolio Loop 1 firing — <fired_at>

**Trigger:** <monthly_base | substantive_artifact (source)>
**Allocation phase:** <current_loop5_phase> (<thematic_allocation_pct>% of portfolio)
**Rate-limit state:** <n_firings_in_window>/3 firings in past 7 days · mandatory escalation: <bool>
**SA LP 13F:** Q<X> 2026 (period <YYYY-MM-DD>, filed <YYYY-MM-DD>)
**Regime classification:** <robust | fragile_high_gamma | fragile_low_gamma | hyper_fragile> (confidence: <high/med/low>)

## Aggregated positions

| Ticker | SA LP wt | Loop 1 target | Adjusted | Action | Critics flagging |
|---|---|---|---|---|---|

For each aggregated decision, one row. Then:

## Structural risks (hold_pending_bertrand_review)

For each position with that recommended_action, list the critic + rationale.

## Drift signals

From loop1_output.drift_signals.

## Source artifacts cited

Deduplicated list from loop1_output.positions[].source_artifacts.

## Files written

- ledgers/thematic/loop1/<fired_at>.json
- ledgers/thematic/loop1/<fired_at>.md
- ledgers/thematic/loop1/<fired_at>__aggregated.json
- ledgers/thematic/loop1/<fired_at>__critic_outputs/ (N files)

## Next action

These recommendations are **advisory only**. No trades have been placed.
To act: review the structural risks (if any), then manually place limit orders
via Tiger paper or escalate to Bertrand. The Loop 5 phasing schedule still
governs total allocation — do NOT exceed the current phase cap.
```

## Notes on subagent location

The subagent prompts live under `.claude/agents/`:

- `thematic-portfolio.md`
- `thematic-artifact-classifier.md`
- `thematic-critics/{marcus, mechanize-epoch, lecun, friedman-extended, thorstad, patel, rasgon}.md`

Promoted out of `_draft/` 2026-05-25 once all dependencies shipped and the Tier 3 layer completed (commit-pair 4b8e8c1... or whichever this commit becomes). Subagent discovery is recursive over `.claude/agents/`, so the names in each file's frontmatter (`thematic-portfolio`, `thematic-artifact-classifier`, `thematic-critic-marcus`, etc.) remain the strings to pass to `subagent_type:` — unchanged by the move.

## Cost target

Per firing (estimated):
- Loop 1 reasoning extraction (Opus 4.7): ~$3-8
- 5 core critics × ~20 positions = ~100 calls (Haiku 4.5): ~$10
- 2 specialist critics × ~3 memory positions (Haiku 4.5): ~$0.30
- Classifier (when event-driven): ~$0.005
- Total: ~$13-19 per firing

At ~3 firings/week soft cap = ~$200/month upper-bound. Well within the design's $50-200/month Phase-1 envelope.

## Operating context

This skill is the operational entry-point for the thematic-portfolio subagent
stack. It does NOT decide whether a firing should happen — that's the
substantive-artifact classifier's job for event-driven firings, and the
Windows Task Scheduler / cron's job for monthly-base firings.

The skill is paper-only by hard refusal. Live capital deployment requires:
1. Q3 2026 SA LP Q2 2026 13F second calibration cycle pass
2. Manual Bertrand approval
3. A separate `--live` plumbing path that does NOT exist in v1

Refuse anything that violates those constraints.