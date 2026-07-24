# Live-validation-sleeve cluster — cherry-pick Batch B (B1–B4)

- asof: 2026-07-24
- implemented by: operator-carried cherry-pick session (vault spec `Output/2026-07-24-claude1-cherrypick-implementation-spec.md`, Batch B)
- branch: `cherrypick-batch-bc` (stacked on the re-based Batch A commit; merge = operator's call)
- provenance: 2026-07-24 GitHub trawl; per-item provenance in module docstrings
- dependencies added: **none** (PSR math hand-rolled on stdlib `math.erf` + Acklam inverse-CDF; zipline/timeseriescv semantics re-implemented, zero code copied from AGPL/all-rights-reserved sources)
- full suite at implementation: **2058 passed**

## B1 — deterministic gate chain (`tools/auto_paper/gate_chain.py`)

Five pure gates between conviction and order, every verdict logged to
`ledgers/paper-auto/_gates/YYYY-MM-DD.jsonl` (append-only, cluster-calibration
sink convention):

1. `sizing_bounds` — fractional Kelly (≤0.5×) on stated probability, clamped to
   the operator band (paper-auto default = the 5% per-position cap). Stated-vs-
   realized calibration inputs logged per candidate. Long-only by construction
   (the NFLX lesson, encoded).
2. `liquidity` — cost ≤ 5% of 20-day dollar ADV (median Close×Volume,
   point-in-time); reduces to the cap; unknown ADV fails conservative.
3. `correlation` — max pairwise 60-day return corr vs open book ≤ 0.75; open-
   theme duplication rejected (cluster map).
4. `concentration` — count/per-position/sector caps + the cross-track cluster
   cap via `cluster_concentration.compute_from_books` (same code as pipeline).
5. `circuit_breaker` — sleeve −20% from high-water trips to no-new-entries;
   state in `journal/paper-auto/sleeve_breaker.json`; reset ONLY via
   `python -m tools.auto_paper.gate_chain reset --operator X --note "..."`
   (logged; refuses when not tripped; HWM re-bases to tripped equity).

- DoD: 33 unit tests incl. boundary cases (`tests/test_auto_paper_gate_chain.py`);
  synthetic 5-row demo run (`... gate_chain demo`) verified — Kelly clamp
  visibly reduced 400→100 shares; breaker trip + manual-reset path tested.
- **Wiring DEFERRED (deliberate):** the one-line `pipeline.place_candidate`
  call-site edit waits until the NFLX short-guard Step-5 remediation completes
  and the cron gate clears. The chain runs standalone until then. This is the
  only open DoD item in Batch B.

## B2 — signal-vs-execution attribution (`tools/auto_paper/execution_attribution.py`)

Per entry fill: total slippage vs signal pivot = delay cost (pivot → fill-day
open) + execution residual (open → fill), identity-checked in tests. Series:
`journal/paper-auto/execution_drag.jsonl` (append-only, keyed dedup,
idempotent). Threshold: monthly notional-weighted drag > 25 bps flags to
operator (basis: mega/large half-spreads are 1.5–5 bps; 25 bps/month means the
execution layer leaks multiples of the spread). Suspect rows (|total| > 500 bps
= corrupted ledger) surface separately, never averaged.

Backfill over the existing paper history (14 rows, 0 suspect):

| month | n | notional $ | total bps | delay bps | exec bps | verdict |
|---|---|---|---|---|---|---|
| 2026-05 | 8 | 302,665 | **−85.6** | +129.9 | −215.5 | ok (favorable) |
| 2026-06 | 6 | 225,052 | **+4.5** | +179.4 | −174.9 | ok |

Reading: entries have actually filled BETTER than signal on net — large
positive delay cost (multi-day gap between signal and fill on momentum names)
offset by strongly favorable intraday execution (marketable limits filling at
opens below pivot-derived limits). The execution layer is not the leak at
current size; the delay component is worth watching as size grows.

## B3 — live-vs-backtest tracking (`tools/auto_paper/live_vs_backtest.py` + `tools/backtest/sharpe_stats.py`)

PSR re-implemented from Bailey & López de Prado (2012/2014), no new deps;
formula spot-check = the paper's own worked example pinned in
`tests/test_sharpe_stats.py` (SR0 0.1132; DSR 0.9004/0.9505 fixtures).
Report: reconstructed daily sleeve curve (no NAV series is persisted —
documented), per-setup PSR vs roster benchmark, expectation cone (backtest
Sharpe shape × live realized vol — hybrid documented in the module), corrupt-
ledger guard (>10% of base equity excluded loudly).

- `tools/deployable_setups.yml` ts_momentum row gained
  `live_benchmark_sharpe: 1.23` (its own 2026-06-20 fill-recert OOS-agg) — the
  PSR benchmark; the zero-cost 2.13 is labeled and never used for this.
- First real report: `2026-07-24-live-vs-backtest-report.md` (same folder).
  Sleeve T=27d, PSR 30.5% vs blended benchmark — weak-sample, honest.
- **Surfaced gap (pre-existing, now load-bearing):** 7 of 14 ledgers are
  `closed` WITHOUT `position_state.exit_price` (the automated close path writes
  exits only into `notes`) — ALL three live ts_momentum trades are invisible to
  both this report and `performance.py`. Fixing the close-out writer (and
  back-filling the 7) is the highest-value follow-up in this batch.

## B4 — volume-share slippage mode (`tools/backtest/portfolio_simulator.py`)

Zipline-reloaded `VolumeShareSlippage` semantics (verified against source
2026-07-24; Apache-2.0; re-implemented): entry fills capped at
`volume_limit`=10% of bar volume (sub-1-share ⇒ MISS; remainder CANCELS — DAY
orders), quadratic impact `0.1 × (shares/bar_vol)²` on liquidity-demanding
transactions (entries + stop/gap/max-hold exits; passive target limit-sells
exempt; exits complete in one bar — stop discipline is never deferred, it just
pays for size). Impact is deliberately double-counted vs the sqrt-law cost
model — stress gate, not best-estimate. Off by default; bit-identical baseline
when off (tested). New result fields: `n_volume_capped` / `n_volume_blocked`.

Roster re-run (`scripts/volume_share_slippage_rerun.py` →
`journal/backtest/2026-07-24-volume-share-slippage-rerun.md`): see that report
for the per-window diff table. Headline: **ts_momentum_liquid_us is unchanged
under the mode (agg Sharpe 1.23 → 1.23, 0 capped / 0 blocked)** — top-8
mega/large-cap positions never approach 10% of bar volume; the live setup is
not a liquidity mirage. Retired setups show capping (residual_momentum: 4
capped / 2 blocked) but were already failing on their merits.

## Follow-ups (operator)

1. Wire the B1 chain into `pipeline.place_candidate` after NFLX Step-5 (one
   call + constants pass-through; the adapter `build_snapshot` is ready).
2. Fix the auto-close writer to populate `position_state.exit_price/exit_date`
   (+ schema) and backfill the 7 gap ledgers — unblocks B3/performance for the
   live setup.
3. Add `execution_attribution update` to the nightly cadence once crons are
   re-enabled (NOT scheduled by this session — crons are disabled under the
   incident gate).
