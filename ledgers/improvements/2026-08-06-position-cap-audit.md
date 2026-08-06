# The 8-position cap audit (Handoff B)

**Date:** 2026-08-06
**Pre-registration:** `2026-08-06-position-cap-audit-prereg.md` (commit
`f0a3c6d` — stopping rule, frozen arms/combos, 12 trials registered 91→103,
Handoff A's repair-vs-search rule applied to the blast radius, ALL before any
experiment number).
**Handoff:** `Output/2026-08-06-claude1-handoff-B-position-cap-audit.md` (vault).
**Data:** `2026-08-06-position-cap-audit-sweep.(py/md/json)` +
`...-fixup.py` + `...-hardrule.json`.

**Verdict up front:** the "four deaths, one cause" hypothesis is **REJECTED**.
At constant gross, widening the count cap does not revive any dead strategy
(connors 0.27→0.32 vs a 1.0 gate; dual_ma 0.50→0.72, still failing), and it
actively **hurts the one live edge** (ts_momentum 1.23→1.05 — its top-K=8
concentration is load-bearing). The count cap is not suppressing any
deployable edge. The audit's real findings are elsewhere: the cap constant is
**undocumented** (§1), the deployed gross is **25–40% against an 85% written
ceiling** (§2), and the N=8 **baseline itself drifts past the written
per-name and sector lines in-sim** because the simulator models no trim or
sector rules (§5). A narrow spec is written per the stopping rule's
obligation (§7); it changes nothing today.

---

## 1. Provenance — where did 8 come from? (§3.1)

**Undocumented, at every layer.** The handoff predicted this and it is the
truth:

- **CLAUDE.md "Maximum 8 concurrent open positions"** — present verbatim in
  the **initial commit** (`e02f7e7`), beside the ORIGINAL trio: 5%
  per-position, 20% sector, 15% cash. No derivation exists in the repo, the
  commit message, or any note. It was born a framework axiom.
- **The founding arithmetic implies 40%, not 95%.** The handoff's
  `8 × 10% + 15% = 95%` reading uses the 2026-06-20 per-position
  reconciliation (5%→10% written). At birth the trio implied
  **8 × 5% = 40% max gross** — and that is exactly what the binding
  simulator enforces today. The "consistent with a gross-exposure
  constraint" coincidence is real but post-hoc.
- **`DEFAULT_MAX_CONCURRENT = 8`** (`tools/backtest/metrics.py`) — born
  2026-05-26 in commit `77ad485` as part of the equity-curve overlap bug fix
  (dual_ma Sharpe 3.02 / DD −70.6% nonsense on 503 tickers). The value was
  copied from the CLAUDE.md rule ("the CLAUDE.md hard rule" is the whole
  annotation). No independent derivation.
- **psim's `max_positions=8` + `max_pct_per_position=0.05`** — born
  2026-06-17 (`bbaba7a`, net-of-cost gate foundation), both annotated
  "CLAUDE.md". The 40% gross ceiling this creates is implemented but nowhere
  stated as intent.
- **Prior art:** ONE cap sweep exists
  (`backtest_results/dual_ma_trend_following_sp500_2026q2_capped.md`,
  2026-05-27, frictionless metrics harness) — it varied the cap while
  HOLDING per-trade risk constant, i.e. it raised gross with the cap. The
  constant-gross question — the one the arithmetic gap poses — had never
  been run before today.

An undocumented constant, copied twice, governing four strategy verdicts:
that part of the handoff's charge is confirmed. What is NOT confirmed is that
it caused the deaths (§4).

## 2. What actually binds — measured (§3.2)

A correction first: the handoff's premise "positions are risk-sized
(`risk_per_trade`)" describes the LEGACY frictionless harness. The **binding**
harness (psim, every roster verdict since 2026-06-17) sizes by cap-weight —
5% × ADV-tilt × equity — so the count cap and the sizing rule jointly cap
gross at ~40% by construction. Measured at N=8 (full-period sims, LIVE
fills):

| Strategy | avg gross while invested | peak gross | % of invested days AT the 8-cap | price-fillable signals lost to the cap (est) |
|---|---|---|---|---|
| connors_rsi2 | 25.8% | 41.8% | 20.7% | 1,415 / 3,338 (42%) |
| dual_ma_trend_following | 39.9% | 45.6% | **84.5%** | 2,756 / 3,198 (86%) |
| ts_momentum_liquid_us | 29.4% | 48.8% | 29.1% | rare (top-K pre-limits flow) |

So: **realized gross is 26–40% against the 85% the written rules permit** —
the handoff's suspicion confirmed, and then some (it guessed 40–60%). The
count cap binds brutally on signal-dense dual_ma (84.5% of invested days),
moderately elsewhere. Whether that lost breadth was worth anything is §4's
question — measured, not assumed.

## 3. The stopping rule (§3.3) — own commit, before the numbers

Committed in `f0a3c6d` (prereg §B), verbatim summary: N=20-vs-N=8 on the
OOS-aggregate; **CLOSE** if ΔSharpe < +0.15 everywhere with no window/MDD
gain; **WRITE-THE-SPEC** if ≥1 strategy shows ΔSharpe ≥ +0.15 with windows
and MDD not degraded and the exposure control held; **MIXED** otherwise.
Exposure-control validity band ±20% relative; void arms get ONE scaling fix
and the re-run replaces them. §3.4 tripwire: any Hard Rule tightening ⇒ STOP.

## 4. Constant-gross results (§3.3) — control verified

Full tables: `2026-08-06-position-cap-audit-sweep.md`. OOS-aggregate,
net-of-cost, LIVE fills; N × pct = 40% by design in every arm:

| Strategy | N=8 | N=12 | N=16 | N=20 | Δ(20−8) | rule branch |
|---|---|---|---|---|---|---|
| connors_rsi2 — Sharpe | 0.27 | 0.29 | 0.30† | 0.32† | **+0.05** | cap harmless (dead at every width) |
| — windows / \|MDD\| | 1/6 / 7.9 | 0/6 / 8.1 | 0/6 / 9.3 | 0/6 / 10.5 | windows worse | |
| dual_ma — Sharpe | 0.50 | 0.69 | 0.68 | 0.72 | **+0.22** | **cap binds** (but see below) |
| — windows / \|MDD\| | 1/6 / 11.1 | 2/6 / 12.0 | 2/6 / 11.3 | 2/6 / 11.4 | windows better | |
| ts_momentum — Sharpe | 1.23 | 1.13 | 1.08 | 1.05 | **−0.18** | widening HURTS the live edge |
| — windows / \|MDD\| | 4/6 / 16.9 | 3/6 / 15.5 | 2/6 / 14.2 | 3/6 / 13.6 | MDD better, Sharpe/windows worse | |

† occupancy-corrected re-runs replacing the two VOID arms (connors' bursty
occupancy grows sublinearly with the cap; realized gross had fallen to
18.3%/15.7% vs the 25.8% baseline — the prereg's one permitted scaling fix
brought them back to 25.8%/25.9%). Control held everywhere else by design:
dual_ma 39.9→38.9%, ts_momentum 29.4→29.7% across arms.

**Reading, per strategy:**
- **connors_rsi2 — the parked diagnosis was wrong in its emphasis.** "The
  cap halves the realised edge" implied removing the cap restores it. At
  constant gross, un-truncating 42% of its signal flow moves net OOS Sharpe
  0.27→0.32 against a 1.0 gate, and the 2022 window stays catastrophic
  (−0.88). The cap was never the killer; the net-of-cost edge is simply too
  weak. The prescribed-but-never-run top-K remediation would rank WITHIN the
  same weak flow — this result lowers its prior sharply.
- **dual_ma — the only genuine cap victim, and it still fails.** +0.22
  Sharpe and a window gained from pure diversification at constant gross —
  the free-lunch mechanism is real where signal flow is wide and unranked.
  But 0.72 remains far under the 1.0 gate (and FULL-period 0.88 likewise).
  Diversification recovered value the cap was destroying; there wasn't
  enough edge underneath.
- **ts_momentum — concentration IS the edge.** Monotone degradation
  1.23→1.05 as top-K widens at constant gross: ranks 9–20 dilute rank 1–8
  quality faster than diversification pays. Third independent confirmation
  of the concentrated-trend conclusion (after the strategy-search
  conclusion and the +3%-chase-cap cohort measurement). **Keep top_k=8 /
  cap 8 for the live deployable — measured, not assumed.**
  Baseline fidelity note: the N=8 arm reproduces the recorded OOS-agg 1.23
  exactly; per-window shows 4/6 vs the recorded 3/6 — the 2021 window sits
  at 0.54 vs 0.49 recorded, floor-edge drift after this week's price-cache
  refresh. Consistent with the standing critic note that the per-window
  pass is contingent on the window pool.

## 5. Hard-Rule interaction (§3.4) — verified, not assumed

Measured on dual_ma full-sims (the branch-2 qualifier), N=8 vs N=20
(`...-hardrule.json`):

| Quantity | N=8 | N=20 | direction |
|---|---|---|---|
| max single-name weight | **10.7%** | 6.1% | falls — strengthens |
| max sector share of equity | **31.6%** | 25.2% | falls — strengthens |
| min cash fraction | 54.4% | 54.1% | unchanged, ≫ 15% floor |

Plus connors' corrected arms: worst-burst cash ~32.5%, above the floor. **No
rule tightens at higher N — the tripwire did not fire.** More positions at
constant gross strengthened every measurable rule, monotonically.

**The unexpected finding is in the BASELINE:** at N=8, the current config
drifts past the written 10% per-name line (10.7% via winner appreciation —
psim sizes at entry and never trims) and far past the 20% sector line
(31.6%; psim models no sector caps at all). The written concentration rules
are enforced pre-trade in the live gate chain, but the BACKTESTS that
certify strategies do not enforce them post-entry. Surfaced for the
operator; fixing it would be a harness change priced under the
repair-vs-search rule (zero trials iff uniform + fully published +
pre-committed).

## 6. Trials cost (§4, stated pre-run and settled)

- **This audit: 12 trials**, registered pre-run (`f0a3c6d`), registry
  91→**103**. The manual pre-registration component has been swapped for the
  recorded sweep JSON (12 sharpe-bearing elements) — same total under
  `derive --write`. Known, disclosed cost: ts_momentum's recorded DSR moves
  0.616 (@91) → **0.593 (@103)**. The floor philosophy accepts this: asking
  the question honestly raises the bar for everyone, including the incumbent.
- **A roster-wide re-evaluation under a changed cap: ZERO new trials** iff
  Handoff A's three clauses hold — (a) uniform over every strategy
  (deployed, parked, retired), (b) full roster published, survivors and
  casualties, (c) the change adopted in its own commit on its own
  justification BEFORE any roster result is seen, irreversible-by-results.
  Run any other way it is a search: ~20 best-combo re-evaluations ≈ +20
  trials (up to +91 for full grids), permanently. The cap VALUE selection
  itself is already priced by this audit's 12.
- **Given §4's verdict, the operator likely never pays either price:** no
  cap change is warranted on the evidence, so no roster re-evaluation is
  proposed.

## 7. The spec the stopping rule obliges (specification ONLY — nothing applied)

Branch 2 fired (dual_ma), so per prereg a specification is written. Its
honest content is narrow:

1. **Reformulate the Hard Rule as what it arithmetically always was on the
   quant track:** "gross exposure ≤ 40% of sleeve equity; per-position
   entry weight ≤ 5%; concurrent count is a per-strategy parameter in
   [8, 20] chosen by that strategy's own constant-gross evidence." Count
   stops being a universal constant; capital stays exactly where it is.
2. **Per-strategy assignments under that rule, from today's evidence:**
   ts_momentum_liquid_us **stays at 8** (widening measured harmful);
   connors_rsi2 parked regardless (dead at every width); dual_ma parked
   regardless (0.72 < 1.0 at its best width). **Net operative change today:
   NONE.**
3. **Adoption path if ever exercised:** the repair-vs-search rule's three
   clauses, verbatim (§6). Plus Handoff A prereg C1 symmetry — a retired
   strategy that flips under a re-run is surfaced, never auto-revived.
4. **Not in this spec:** any change to `DEFAULT_MAX_CONCURRENT`,
   `PortfolioConfig` defaults, `deployable_setups.yml`, or CLAUDE.md — scope
   §5 forbids them this session, and the evidence does not motivate them.

## 8. What was surprising (report-back honesty)

1. The four-deaths table attributed to the cap what net-of-cost weakness
   explains: at constant gross, the two "cap-killed" parked strategies stay
   dead by wide margins. The cap made their backtests worse; it did not
   destroy deployable edges.
2. The live edge runs the OTHER way — the cap (as top-K) is doing quality
   selection worth ~0.18 Sharpe. "Diversification at constant gross is the
   closest thing to a free lunch" is TRUE (dual_ma +0.22, every Hard-Rule
   metric strengthened) — but only where signal flow is wide and unranked,
   and lunch was worth less than the gate demands.
3. The baseline concentration drift (§5) — the written 10%/20% lines are
   breached in-sim by the CURRENT config, not the wider arms. The audit
   aimed at the cap and found the un-modeled trim/sector rules instead.
4. connors' sublinear occupancy voided two arms exactly as the prereg's
   control anticipated — the constant-gross control is not a formality;
   without it the N=16/20 arms would have silently under-deployed 29–39%
   and the comparison would have been noise.
