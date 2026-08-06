# PRE-REGISTRATION — 8-position cap audit (Handoff B)

**Date:** 2026-08-06
**Status:** PRE-REGISTERED — committed BEFORE any §3.3 experiment number is
computed. Handoff: `Output/2026-08-06-claude1-handoff-B-position-cap-audit.md`
(vault). Deliverable: `ledgers/improvements/2026-08-06-position-cap-audit.md`.
**Handoff A dependency (reported):** global trials counter stands at 91,
unchanged; repair-vs-search rule adopted with the third clause
(irreversible-by-results). Applied in §C below, per operator instruction,
BEFORE anything that would incur the cost.

---

## A. Frozen experiment design (§3.3 — same money, more names)

**Harness:** `tools.backtest.portfolio_simulator` (psim) — the BINDING
net-of-cost harness that has governed every roster verdict since 2026-06-17 —
with the LIVE fill config used by the fill-recert / lookback-sweep artifacts:
`fill_model=FILL_MARKETABLE_LIMIT, momentum_buffer=0.03,
full_spread_marketable=True` (connors resolves to the passive REVERSION fill
class automatically via `entry_pricing.resolve_kind`). Full-period simulate +
rolling walk-forward per each spec's `walk_forward` block and gate params.
NOTE (honesty): the handoff's §3.2 premise "positions are risk-sized
(`risk_per_trade`)" describes the LEGACY frictionless metrics harness. The
binding psim harness sizes by CAP-WEIGHT: `max_pct_per_position` (5%) × ADV
tilt × equity ([portfolio_simulator.py:417](tools/backtest/portfolio_simulator.py#L417)).
That makes the §2 question sharper, not weaker: 8 × 5% = **40% maximum gross
by construction** vs the 85% the Hard Rules permit.

**Arms:** N ∈ {8, 12, 16, 20}. Constant-gross control:
`max_positions = N`, `max_pct_per_position = 0.05 × 8 / N`
(8→5.00%, 12→3.33%, 16→2.50%, 20→2.00%; N × pct = 40% in every arm).
For ts_momentum the KIND's cross-sectional rank width scales with the cap:
`top_k = N` (its cap fix made top_k ≡ cap; leaving top_k=8 would starve the
wider arms by construction).

**Strategies and pinned combos (no per-arm combo selection — one combo per
strategy across all arms, chosen from the RECORDED baseline artifacts):**

| Strategy | Pinned combo | Why pinned this way |
|---|---|---|
| `connors_rsi2` (parked 2026-06-09, cap named as cause: 47% rejection) | `entry_threshold=15, cooldown_days=3` | the parked row's combo |
| `dual_ma_trend_following` (parked 2026-05-27, "cap reveals weak edge") | `short=20, long=100` | the cap=8 top combo per the 2026-05-27 sweep artifact |
| `ts_momentum_liquid_us` (LIVE; pre-top-K the cap rejected 98.5%) | `lookback_days=252` (deployed) | the deployed config |

Bias disclosure, stated pre-hoc: pinning the BASELINE-selected combo biases
against the wider arms (the 2026-05-27 sweep showed the top combo reorders
with cap). A flat result therefore closes the question only for "spread the
DEPLOYED strategy wider," which is the deployment-relevant form of it.

**Cap-rejection instrumentation:** one extra full-period run per strategy at
`max_positions=4096, max_pct_per_position=0.02` — from which ONLY
`n_signals` / `n_filled` are read (price-fillable count). Per-arm cap
rejections ≈ fillable_uncapped − n_filled_arm (approximation; cash-path
effects noted). **The uncapped runs' Sharpe/MDD are never read, printed, or
stored** — they are undeployable by construction (they void the Hard Rule
under audit) and must not enter any selection set.

**Per-arm report:** OOS-agg net Sharpe, |MDD|, n, per-window clears/total,
cap-rejection count, and REALIZED gross exposure (post-hoc from the
full-period sim's trades + equity curve: average gross while invested,
average over all days, peak, and %-of-days at the position cap) — the §3.3
exposure-control verification.

## B. Stopping rule (pre-registered, before the numbers)

Primary comparison: N=20 arm vs N=8 arm, per strategy, OOS-aggregate
net-of-cost walk-forward.

1. **CAP HARMLESS → CLOSE** if for EVERY strategy: ΔSharpe(20−8) < +0.15
   AND per-window clears(20) ≤ clears(8) AND |MDD| does not improve by
   ≥ 2pp. Verdict: the count cap is not the binding constraint at constant
   gross; this line of work closes; no spec is written. (A clean "the cap is
   fine" is a good outcome — handoff §6.7.)
2. **CAP BINDS → WRITE THE SPEC** if for ≥1 strategy: ΔSharpe(20−8) ≥ +0.15
   AND per-window clears(20) ≥ clears(8) AND |MDD|(20) ≤ |MDD|(8) + 2pp AND
   the exposure control held. Deliverable is a SPECIFICATION only — no change
   to `DEFAULT_MAX_CONCURRENT` or any sizing default this session (scope §5).
3. **MIXED** (any other pattern) → report per-strategy, no spec, operator
   decides. N=12/16 monotonicity is reported as supporting evidence only —
   it does not gate.

Threshold provenance: +0.15 Sharpe ≈ above the re-validation drift band seen
across this repo's re-runs (±0.05–0.10); 2pp MDD is material against the 25%
gate clause.

**Controls and tripwires:**
- **Exposure control:** an arm is VALID only if its realized
  average-gross-while-invested is within ±20% (relative) of the N=8 arm's.
  A violated arm is VOID → fix the scaling once, re-run; the re-run REPLACES
  the void arm (measurement correction of the same trial, not a new variant
  — declared here, pre-hoc).
- **§3.4 tripwire (operator instruction, verbatim):** more positions at
  constant gross must STRENGTHEN the Hard Rules — per-position exposure
  falls, cash buffer unchanged, concentration falls. **If any rule tightens
  instead, STOP and report.** Checked per arm: max realized single-position
  weight, min realized cash fraction, max single-name share of gross; sector
  attribution best-effort via the SEC-SIC map where universe coverage allows
  (psim does not model sector/cluster caps — reported honestly as
  not-modeled where unmeasurable).
- No arms beyond {8,12,16,20}; no per-strategy tuning; no combo re-selection;
  verdict only from the pre-named metrics above.

## C. Trials cost (Handoff A's rule applied BEFORE incurring anything)

**This experiment:** 3 strategies × 4 arms = **12 trials**, registered in
`ledgers/trials.yml` in this commit, BEFORE the first run (91 → **103**).
Counting note: the N=8 baseline arms re-measure the standard config
(psim defaults max_positions=8 / 5%) and could arguably ride the
re-measurement exemption, but the constant-gross frame makes them arms of a
selection sweep — counted, conservative direction, per the floor philosophy.
Post-run, the manual pre-registration component is REPLACED by the recorded
sweep JSON (12 sharpe-bearing elements) so `derive --write` reproduces the
same total; the swap is documented in the deliverable. Known cost, stated
up front: raising the floor 91→103 lowers every future DSR, including the
live deployable's (exact number disclosed in the deliverable).

**A roster-wide re-evaluation under a changed cap (the §4 blast radius):**
- **ZERO new trials** iff ALL THREE clauses of Handoff A's rule hold:
  (a) the change is applied uniformly to EVERY strategy — deployed, parked,
  and retired alike; (b) the FULL re-evaluated roster is published, survivors
  and casualties; (c) the change is adopted in its own commit, on its own
  correctness justification, BEFORE any roster re-run result is seen, and is
  irreversible-by-results.
- The cap VALUE itself is outcome-selected by THIS audit — that selection is
  priced by the 12 trials above. What clause (c) then forbids is a second
  selection layer: adopting the cap, seeing the roster, and rolling back (or
  re-running only the strategies one hopes revive — fails (a)).
- **If run any other way:** every re-evaluated setup×combo is a new priced
  trial — ~20 best-combos ≈ +20 trials (up to +91 if full grids are re-run),
  permanently raising the DSR bar for everything. That is the price of doing
  it as a search instead of a repair.
- **Un-retiring note:** even under a clean zero-trial repair, a retired
  strategy that flips to pass is SURFACED, not auto-revived — deployment
  decisions stay with the operator and the standing gates (Handoff A prereg
  C1 symmetry: results published for winners and losers alike).
- Not run this session either way — the operator decides whether the
  re-evaluation is worth its cost AFTER seeing this audit's verdict
  (handoff §4).

## D. Scope restated (handoff §5)

No change to `DEFAULT_MAX_CONCURRENT` or any sizing default; no connors top-K
remediation; no `event_earnings_drift` re-test (retired, candidate freeze D1);
no `hold: true` removals; no deployment; no un-retiring. Provenance (§3.1) and
realized-gross measurement (§3.2) precede the experiment; neither can alter
this stopping rule after this commit.
