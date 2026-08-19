# Proposal — re-validate `xs_short_term_reversal` (SUGGEST-ONLY)

- **Pilot:** Claude1 paper-research self-improvement pilot
- **Role:** proposal-drafter · suggest-only · no trade, no live edit
- **Date:** 2026-07-02
- **Target setup:** `xs_short_term_reversal` (cross-sectional 5-day return reversal, long-only; 88-ticker SP500-leaning universe + SPY benchmark; weekly rebalance; deployed `bottom_n=5`)
- **Exists in `deployable_setups.yml`?** YES — but the row already carries `hold: true` and `net_gate_retired_at: 2026-06-17`. **This is a re-validation of a currently-RETIRED row, not of a live setup.** (Flagged to operator: the pilot brief said "re-validate the *deployed* setup"; the honest baseline is that this setup was retired two weeks ago by the net-of-cost gate.)
- **Relationship to the 2026-07-01 run:** a prior pilot session filed an identical-target proposal on 2026-07-01. This run **independently re-executed all three backtests** (the whole point of the pilot is to not trust reported numbers) and reproduced every figure to the decimal. Same target ⇒ this should count as **ONE** distinct proposal toward the ≥5-proposal wait-condition, not two — operator's call whether to collapse the ledger rows.

---

## 1. Hypothesis

The 2026-06-17 net-of-cost gate retired this setup (net OOS Sharpe ≈ 0.52). Re-validation hypothesis: **the retirement is correct and reproducible, and no small parameter change inside the existing `bottom_n` grid rescues the edge net-of-cost.** Prediction: gross zero-cost walk-forward still clears the deployment gate (~1.6 aggregate OOS Sharpe), but net-of-cost OOS stays well under the 1.0 gate across the whole grid, driven by an adverse-selection fill signature intrinsic to a resting-limit mean-reversion book (limit orders fill the losers that keep falling, miss the bouncers that gap up).

## 2. The change

**NO CHANGE — re-validation only.** Recommendation: **keep `xs_short_term_reversal` retired (`hold: true`).** Do not re-deploy, do not re-parameterise. A well-evidenced "confirm baseline / no improvement" per the drafter brief. (§3c shows `bottom_n=10/15`, which nudge *full* Sharpe just over 1.0, are **not** a rescue — OOS still fails.)

## 3. Evidence — every number below is reproduced from an attached artifact I ran on 2026-07-02

### 3a. GROSS zero-cost, 6-window rolling walk-forward (the number that DEPLOYED it, 2026-05-25)
Artifact: [`2026-07-02-xs_short_term_reversal-grosswf-zerocost.md`](2026-07-02-xs_short_term_reversal-grosswf-zerocost.md) — reproduced via `uv run python -m tools.quant_strategies.runner --spec tools/quant_strategies/xs_short_term_reversal.yml`.

- Deployed `bottom_n=5`: aggregate **OOS Sharpe 1.60**, |MDD| 24.19%, n=1497, win-rate 54%, expectancy +0.08R, PF 1.30 → composite gate **PASSED**.
- Per-window OOS Sharpe `[2.73, 1.18, -1.43, 2.98, 2.09, 1.85]` (2020 / 2021 / 2022 / 2023 / 2024 / 2025) → **5/6 windows** clear Sharpe > 0.5 (2022 rate-hike fails at −1.43). Per-window clause PASSED.
- This is the zero-cost, fill-every-signal harness — it models **no costs and no slippage**. That is precisely the defect the net gate corrects. **This is gross OOS; it is NOT a net-of-cost result.**

### 3b. NET-of-cost gate — deployed `bottom_n=5` (the binding retire verdict)
Artifact: [`2026-07-02-netgate-groundtruth.md`](2026-07-02-netgate-groundtruth.md) — reproduced via `uv run python scripts/net_gate_rerun.py --setup xs_short_term_reversal` (hardened portfolio-equity sim: `cost_model` effective-spread + sqrt impact, OHLC-based fills, 8-concurrent cap, cap-weight; OOS = last-30% holdout).

| metric | FULL | OOS (last 30%) | gate |
|---|---|---|---|
| net Sharpe | **0.82** | **0.50** | > 1.0 |
| \|MDD\| | 12.5% | 9.3% | < 25% |
| n trades | 1894 | 573 | ≥ 30 |
| fill rate | 81% | — | — |
| net return | +79.3% | — | — |
| **VERDICT** | | | **RETIRE** |

- Adverse-selection signature (full period): **filled fwd-return 0.19% vs missed fwd-return 3.99%.** The resting limit fills the losers that keep falling and misses the bouncers that gap up — the mean-reversion "bounce" is exactly what limit-fill mechanics select *against*.
- **before → after (same config, two run dates):** the retiring net gate on 2026-06-17 (existing repo artifact [`journal/backtest/2026-06-17-net-gate-rerun.md`](../../journal/backtest/2026-06-17-net-gate-rerun.md)) = FULL 0.85 / OOS 0.52 → RETIRE. Today's independent re-run = FULL 0.82 / OOS 0.50 → RETIRE. **Verdict is stable across the data refresh (drift < 0.03 Sharpe).**

### 3c. `bottom_n` grid sweep, NET-of-cost (does any small param change rescue it?)
Artifacts: [`2026-07-01-xs_short_term_reversal-bottom_n-sweep.md`](2026-07-01-xs_short_term_reversal-bottom_n-sweep.md) + `.json`; reproduction script [`2026-07-01-xs_short_term_reversal-bottom_n-sweep.py`](2026-07-01-xs_short_term_reversal-bottom_n-sweep.py) (imports the *same* net simulator / kind / universe as the canonical gate; only `bottom_n` varies). **Script filename keeps the 2026-07-01 date; I re-ran it on 2026-07-02 and the output reproduced to the decimal.**

| bottom_n | FULL net Sharpe | **OOS net Sharpe** | fill% | filled_fwd% | missed_fwd% | VERDICT |
|---|---|---|---|---|---|---|
| 5 (deployed) | 0.82 | **0.50** | 81 | 0.19 | 3.99 | RETIRE |
| 10 | 1.04 | **0.67** | 72 | 0.28 | 3.52 | RETIRE |
| 15 | 1.08 | **0.65** | 51 | 0.27 | 3.39 | RETIRE |

- **Every grid point RETIREs.** `bottom_n=10/15` lift *full* Sharpe just above 1.0, but **OOS peaks at 0.67 — still a third below the 1.0 gate**, and the gate requires FULL **and** OOS to clear. As `bottom_n` widens, fill-rate collapses (81%→51%) because the 8-concurrent cap binds harder — the extra signals are the same weak edge, not new alpha. The adverse-selection gap (filled ≪ missed fwd-return) is invariant to `bottom_n`. **No parameter rescue exists in the grid.**

## 4. Risk self-check

- **Overfitting:** zero new parameters invented. The sweep touched **one** existing grid axis (`bottom_n ∈ {5,10,15}`, already in the spec). The recommendation is "no change," which has **zero overfit surface** — no knob was tuned to a target.
- **Lookahead / data-leakage:** costs use ADV-keyed spread tiers (keyed on liquidity, not a forward cap snapshot); OHLC fills use *same-bar* open/low only (marketable pivot fills at open iff gap ≤ 3%; resting limit fills iff bar low ≤ pivot) — no future bar informs a fill. Signal is trailing 5-day return. No leakage found.
- **Regime-dependence:** gross per-window already shows regime fragility (2022 = −1.43 even at zero cost). The **net** OOS holdout (last-30% ≈ mid-2023 → 2026, a *mostly benign* regime) still returns only 0.50 — i.e. it fails net-of-cost even without sampling the worst regime. Fragile on both axes.
- **Cost-sensitivity:** this IS the finding. Gross OOS 1.60 → net OOS 0.50. The edge does not survive one bar of realistic retail cost + fill. The mechanism is structural (limit-fill adverse selection), not a cost-level knife-edge — it would not be saved by slightly gentler cost assumptions (the prior session's cost-toggle stress moved OOS only 0.50→0.52).
- **Sample-size:** ample — n=1894 full / 573 OOS (net), 1497 (gross). The failure is signal, not small-sample noise.

## 5. Proposal card

| field | value |
|---|---|
| **Target setup** | `xs_short_term_reversal` (deployed `bottom_n=5`, 88-ticker SP500-leaning + SPY, weekly rebalance, hold 5d) |
| **The change** | **None — re-validation. Keep retired (`hold: true`).** No grid param clears the net OOS gate. |
| **Backtest config** | Windows: gross = 6-window rolling WF (3y IS / 1y OOS / 1y step, 2017-01-01→2026-05-25); net = full period + last-30% OOS holdout. Costs: `cost_model` effective-spread + sqrt (Almgren/Bouchaud) impact; OHLC fills; 8-concurrent cap; cap-weight. Universe: `sp500_leaning_88` + SPY. |
| **Before → after** | GROSS deploy verdict 1.60 aggregate OOS (PASS, 2026-05-25) → NET verdict FULL 0.82 / OOS 0.50 (RETIRE, 2026-06-17) → independent re-run today FULL 0.82 / OOS 0.50 (RETIRE, stable). Grid: all RETIRE, best OOS 0.67. |
| **Artifact references** | §3a `2026-07-02-…-grosswf-zerocost.md`; §3b `2026-07-02-netgate-groundtruth.md` (+ existing `journal/backtest/2026-06-17-net-gate-rerun.md`); §3c `2026-07-01-…-bottom_n-sweep.md`/`.json`/`.py` (re-run today) |
| **Self-assessment** | **Confident.** Three independent harnesses + a full grid sweep converge on the same RETIRE; the retirement reproduces within data-refresh drift. |
| **Honesty check** | *Is every figure here reproducible from an attached artifact?* **Yes** — every number traces to a `ledgers/improvements/` artifact I ran on 2026-07-02 (one before→after figure cites the existing repo net-gate report). Nothing is asserted from memory. |

### Methodological limitation (surfaced deliberately)
No single repo harness gives **both** the 6-window per-window clause **and** cost+slippage: the rolling-WF runner is zero-cost, and the net gate uses a single last-30% OOS split. This proposal presents both harnesses and is transparent about the gap. The conclusion is robust to it — the setup fails the net gate on the *benign* recent holdout, and fails the per-window clause's worst regime (2022) even gross. **Highest-value follow-up (a separate proposal, out of scope here): a cost-aware rolling-6-window WF harness**, so future net verdicts carry the per-window clause directly. That is a *tooling* improvement, not a strategy change, and outside this suggest-only re-validation.

---
*Suggest-only. Nothing here is applied. Operator merges manually.*
