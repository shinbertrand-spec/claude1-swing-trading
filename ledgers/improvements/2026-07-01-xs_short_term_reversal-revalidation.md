# Proposal — re-validate `xs_short_term_reversal` (SUGGEST-ONLY)

- **Pilot:** Claude1 paper-research self-improvement pilot (first proposal)
- **Role:** proposal-drafter · suggest-only · no trade, no live edit
- **Date:** 2026-07-01
- **Target setup:** `xs_short_term_reversal` (cross-sectional 5-day return reversal, long-only; 88-ticker SP500-leaning universe; weekly rebalance; deployed `bottom_n=5`)
- **Exists in `deployable_setups.yml`?** YES — but already `hold: true`, `net_gate_retired_at: 2026-06-17`. This is a re-validation of a **currently-retired** row, not of a live setup. (Flagged to operator: the pilot brief assumed a *deployed* setup; the real baseline is retired.)

---

## 1. Hypothesis

The 2026-06-17 net-of-cost gate retired this setup (net OOS Sharpe ≈ 0.52). The re-validation hypothesis: **the retirement is correct and reproducible, and no small parameter change inside the existing grid rescues the edge net-of-cost.** Prediction: gross zero-cost walk-forward still looks good (~1.6 Sharpe), but net-of-cost OOS stays well under the 1.0 gate across the whole `bottom_n` grid, driven by an adverse-selection fill signature intrinsic to a resting-limit mean-reversion book.

## 2. The change

**NO CHANGE — re-validation only.** Recommendation: **keep `xs_short_term_reversal` retired (`hold: true`).** Do not re-deploy, do not re-parameterise. A well-evidenced "confirm baseline / no improvement" per the drafter brief. (See §3 for why `bottom_n=10/15`, which nudge *full* Sharpe over 1.0, are **not** a rescue.)

## 3. Evidence — every number below is reproduced from an attached artifact I ran today

All artifacts live under `ledgers/improvements/` (this run) except one existing repo artifact noted inline.

### 3a. GROSS zero-cost, 6-window rolling walk-forward (the number that DEPLOYED it, 2026-05-25)
Artifact: [`2026-07-01-xs_short_term_reversal-grosswf-zerocost.md`](2026-07-01-xs_short_term_reversal-grosswf-zerocost.md) — reproduced via `tools.quant_strategies.runner`.

- Deployed `bottom_n=5`: aggregate **OOS Sharpe 1.60**, |MDD| 24.19%, n=1497, CAGR +21.95% → composite gate **PASSED**.
- Per-window OOS Sharpe `[2.73, 1.18, -1.43, 2.98, 2.09, 1.85]` (2020…2025) → **5/6 windows** clear Sharpe > 0.5 (2022 rate-hike fails at -1.43). Per-window clause PASSED.
- This is the zero-cost, fill-every-signal harness. It models **no costs and no slippage** — that is exactly the defect the net gate corrects.

### 3b. NET-of-cost gate — deployed `bottom_n=5` (the binding retire verdict)
Artifact: [`2026-07-01-xs_short_term_reversal-netgate-deployed.md`](2026-07-01-xs_short_term_reversal-netgate-deployed.md) — reproduced via `scripts/net_gate_rerun.py` (hardened portfolio sim: `cost_model` effective-spread + sqrt impact, OHLC-based fills, 8-concurrent cap, cap-weight; OOS = last-30% holdout).

| metric | FULL | OOS (last 30%) | gate |
|---|---|---|---|
| net Sharpe | **0.82** | **0.50** | > 1.0 |
| |MDD| | 12.5% | 9.3% | < 25% |
| n trades | 1894 | 573 | ≥ 30 |
| fill rate | 81% | — | — |
| net return | +79.3% | — | — |
| **VERDICT** | | | **RETIRE** |

- Adverse-selection signature (full): **filled fwd-return 0.19% vs missed fwd-return 3.99%.** The resting limit fills the losers that keep falling and misses the bouncers that gap up — the mean-reversion "bounce" is precisely what the limit-fill mechanics select *against*.
- **before → after (same config, same harness, two dates):** prior net gate 2026-06-17 (existing repo artifact [`journal/backtest/2026-06-17-net-gate-rerun.md`](../../journal/backtest/2026-06-17-net-gate-rerun.md)) = FULL 0.85 / OOS 0.52 → RETIRE. Today's re-run = FULL 0.82 / OOS 0.50 → RETIRE. **Verdict is stable across the 14-day data refresh.**

### 3c. `bottom_n` grid sweep, NET-of-cost (does any small param change rescue it?)
Artifacts: [`2026-07-01-xs_short_term_reversal-bottom_n-sweep.md`](2026-07-01-xs_short_term_reversal-bottom_n-sweep.md) + `.json`; reproduction script [`2026-07-01-xs_short_term_reversal-bottom_n-sweep.py`](2026-07-01-xs_short_term_reversal-bottom_n-sweep.py) (imports the *same* simulator/kind/universe as the canonical gate; only `bottom_n` varies).

| bottom_n | FULL net Sharpe | **OOS net Sharpe** | fill% | filled_fwd% | missed_fwd% | VERDICT |
|---|---|---|---|---|---|---|
| 5 (deployed) | 0.82 | **0.50** | 81 | 0.19 | 3.99 | RETIRE |
| 10 | 1.04 | **0.67** | 72 | 0.28 | 3.52 | RETIRE |
| 15 | 1.08 | **0.65** | 51 | 0.27 | 3.39 | RETIRE |

- **Every grid point RETIREs.** `bottom_n=10/15` lift *full* Sharpe just above 1.0, but **OOS peaks at 0.67 — still a third below the 1.0 gate**, and the gate requires FULL **and** OOS to clear. As `bottom_n` widens, fill-rate collapses (81%→51%) because the 8-concurrent cap binds harder — the extra signals are the same weak edge, not new alpha. The adverse-selection gap is invariant to `bottom_n`. **No parameter rescue exists in the grid.**

## 4. Risk self-check

- **Overfitting:** zero new parameters invented. The sweep touched **one** existing grid axis (`bottom_n ∈ {5,10,15}`, already in the spec). The recommendation is "no change," which has **zero overfit surface**. No knob was tuned to a target.
- **Lookahead / data-leakage:** costs use ADV-keyed spread tiers (`security_master`, keyed on ADV not a cap snapshot, to avoid look-ahead); OHLC fills use *same-bar* open/low only (marketable pivot fills at open iff gap ≤ 3%; resting limit fills iff bar low ≤ pivot) — no future bar informs a fill. Signal is trailing 5-day return. No leakage found.
- **Regime-dependence:** gross per-window already shows regime fragility (2022 = -1.43 even at zero cost). The **net** OOS holdout (last-30% ≈ 2023-H2…2026, a *mostly benign* regime) still returns only 0.50 — i.e. it fails net-of-cost even without sampling the worst regime. Fragile on both axes.
- **Cost-sensitivity:** this IS the finding. Gross OOS 1.60 → net OOS 0.50. The edge does not survive realistic retail cost + fill for one bar. Mechanism is structural (limit-fill adverse selection), not a cost-level knife-edge — it would not be saved by slightly gentler cost assumptions.
- **Sample-size:** ample — n=1894 full / 573 OOS (net), 1497 (gross). The failure is signal, not small-sample noise.

## 5. Proposal card

| field | value |
|---|---|
| **Target setup** | `xs_short_term_reversal` (deployed `bottom_n=5`, 88-ticker SP500-leaning, weekly) |
| **The change** | **None — re-validation. Keep retired (`hold: true`).** No grid param clears the net OOS gate. |
| **Backtest config** | Windows: gross = 6-window rolling WF (3y IS / 1y OOS / 1y step, 2017→2026); net = full + last-30% OOS holdout. Costs: `cost_model` effective-spread + Almgren/Bouchaud sqrt impact (100bps @ 1×ADV); OHLC fills; 8-concurrent cap; cap-weight. Universe: `sp500_leaning_88` + SPY. |
| **Before → after** | GROSS deploy verdict 1.60 (PASS, 2026-05-25) → NET verdict FULL 0.82 / OOS 0.50 (RETIRE, 2026-06-17) → re-run today FULL 0.82 / OOS 0.50 (RETIRE, stable). Grid: all RETIRE, best OOS 0.67. |
| **Artifact references** | §3a `…-grosswf-zerocost.md`; §3b `…-netgate-deployed.md` (+ existing `journal/backtest/2026-06-17-net-gate-rerun.md`); §3c `…-bottom_n-sweep.md`/`.json`/`.py` |
| **Self-assessment** | **Confident.** Multiple independent harnesses + a full grid sweep converge on the same RETIRE; the retirement reproduces within data-refresh drift. |
| **Honesty check** | *Is every figure here reproducible from an attached artifact?* **Yes** — every number traces to a `ledgers/improvements/` artifact from this run (one prior figure cites the existing repo net-gate report). Nothing is asserted from memory. |

### Methodological limitation (surfaced deliberately)
No single repo harness gives **both** the 6-window per-window clause **and** cost+slippage: the rolling WF runner is zero-cost, and the net gate uses a single 30% OOS split. This proposal presents both and is transparent about it. The conclusion is robust to the gap (the setup fails the net gate on the *benign* recent holdout, and fails the per-window clause's worst regime even gross). **Highest-value follow-up (separate proposal): a cost-aware rolling-6-window WF harness**, so future net verdicts carry the per-window clause directly. That is a *tooling* improvement and out of scope for this suggest-only strategy re-validation.

---
*Suggest-only. Nothing here is applied. Operator merges manually.*
