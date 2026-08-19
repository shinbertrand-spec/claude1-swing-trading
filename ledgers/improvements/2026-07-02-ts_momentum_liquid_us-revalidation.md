# Proposal — re-validate `ts_momentum_liquid_us` (SUGGEST-ONLY)

- **Pilot:** Claude1 paper-research self-improvement pilot (proposal #2, operator-selected target)
- **Role:** proposal-drafter · suggest-only · no trade, no live edit
- **Date:** 2026-07-02
- **Target setup:** `ts_momentum_liquid_us` (time-series momentum, long-only; `liquid_us_2026q2` 1178-ticker S&P 1500 ∩ ADV>500K + SPY; monthly rebalance; deployed `lookback_days=252`, `top_k=8`)
- **Exists in `deployable_setups.yml`?** YES — and it is the **only live generic deployable** (all other generic rows carry `hold: true`). `net_gate_cleared_at: 2026-06-17` (KEEP) + `fill_model_recert_at: 2026-06-20` (DEPLOY — MARGINAL). This is a re-validation of a **live, capital-eligible** setup — the highest-stakes target the pilot can take.

---

## 1. Hypothesis

The 2026-06-20 v2 fill-recert deployed this setup as **MARGINAL / FRAGILE** — it clears the gate but its per-window OOS profile sits at exactly the 3/6 pass-rate floor under realistic full-spread cost. Re-validation hypothesis: **the DEPLOY-MARGINAL verdict is correct and reproducible, and no in-grid parameter change de-fragilises it.** Prediction: (a) the realistic-fill recert reproduces FULL Sharpe ~1.35 / OOS-agg ~1.23 / per-window 3/6; (b) the single-split net gate reproduces OOS ~1.04 (KEEP); (c) the alternative in-grid `lookback_days=126` does **not** improve per-window robustness. Corollary: the fragility is a property of the ts_momentum *signal* on this universe, not of the lookback choice — so the honest recommendation is **keep 252, keep the fragile-pass discipline (conservative sizing + pre-committed kill criterion), change nothing.**

## 2. The change

**NO CHANGE — re-validation only.** Recommendation: **keep `ts_momentum_liquid_us` deployed at `lookback_days=252` / `top_k=8`, retaining its DEPLOY-MARGINAL/FRAGILE status and the operator's conservative-sizing + pre-committed-kill discipline.** Do NOT switch to `lookback_days=126` (§3c shows it is strictly worse — RETIRE/REBUILD). A well-evidenced "confirm baseline / no improvement" per the drafter brief. **This is not a green light to upsize** — the fragility flag is reaffirmed, not lifted.

## 3. Evidence — every number below is reproduced from an attached artifact I ran on 2026-07-02

### 3a. Realistic fill-recert (the BINDING v2 harness — full-spread cost + per-window clause)
Artifact: [`2026-07-02-ts_momentum-fill-recert-groundtruth.md`](2026-07-02-ts_momentum-fill-recert-groundtruth.md) — reproduced via `uv run python scripts/momentum_fill_recert.py --setup ts_momentum_liquid_us` (LIVE = marketable limit prior_close×1.03, FULL effective spread on the cross, OHLC fills, 8-concurrent cap; rolling 3y-IS / 1y-OOS / 1y-step walk-forward).

| variant | scope | Sharpe | \|MDD\|% | n | fill% | gate |
|---|---|---|---|---|---|---|
| LIVE mkt-limit 3% (full-spread) | FULL | **1.35** | 16.9 | 736 | 92 | PASS |
| LIVE mkt-limit 3% (full-spread) | OOS-agg | **1.23** | 16.9 | 525 | 91 | PASS |
| STRESS (+5bps slip, 85% fill) | OOS-agg | 1.26 | 15.2 | 450 | 78 | PASS |
| pure_moo (diagnostic, NOT live) | FULL | 1.27 | 17.5 | 802 | 100 | PASS |

- **Per-window OOS (LIVE):** `2020:0.64✓ 2021:0.54✓ 2022:0.29✗ 2023:0.41✗ 2024:0.63✓ 2025:0.50✗` → **3/6 clear Sharpe>0.5 = EXACTLY the 50% pass-rate floor.** Weakest passing window 0.54 (floor 0.5). **One window flipping sign fails the clause.**
- Verdict reproduces to the decimal: **DEPLOY — MARGINAL (fragile per-window robustness).** The edge is carried by 2020/2021/2024 and is weak (<0.5) in 2022/2023/2025. Full-spread cost barely moved FULL Sharpe (→1.35), so the fragility is **signal robustness, not execution cost.**

### 3b. Single-split net-of-cost gate (the 2026-06-17 KEEP harness — second independent view)
Artifact: [`2026-07-02-ts_momentum-netgate-groundtruth.md`](2026-07-02-ts_momentum-netgate-groundtruth.md) — reproduced via `uv run python scripts/net_gate_rerun.py --setup ts_momentum_liquid_us` (hardened portfolio sim, last-30% OOS holdout).

| metric | FULL | OOS (last 30%) | gate |
|---|---|---|---|
| net Sharpe | **1.35** | **1.04** | > 1.0 |
| \|MDD\| | 16.9% | — | < 25% |
| n trades | 736 | — | ≥ 30 |
| fill rate | 92% | — | — |
| net return | +450.8% | — | — |
| **VERDICT** | | | **KEEP** |

- OOS single-split Sharpe **1.04** — clears 1.0 but *barely*. As the spec's own gate comment notes, a single OOS Sharpe near 1.0 has SE ~0.6 over ~2.8y — **statistically indistinguishable from 1.0.** This is exactly why the per-window clause (§3a), not this point estimate, carries the deploy decision.

### 3c. `lookback_days` grid sweep, SAME realistic-fill harness (does the other grid point improve robustness?)
Artifacts: [`2026-07-02-ts_momentum-lookback-sweep.md`](2026-07-02-ts_momentum-lookback-sweep.md) + `.json`; reproduction script [`2026-07-02-ts_momentum-lookback-sweep.py`](2026-07-02-ts_momentum-lookback-sweep.py) (imports the *same* `portfolio_simulator.simulate_walk_forward` + LIVE fill config + kind + universe as the canonical recert; only `lookback_days` varies).

| lookback | FULL Sharpe | FULL \|MDD\|% | OOS-agg | per-window (clears/6) | weakest pass | VERDICT |
|---|---|---|---|---|---|---|
| **252 (deployed)** | 1.35 | 16.9 | 1.23 | **3/6** (2020:.64✓ 21:.54✓ 22:.29✗ 23:.41✗ 24:.63✓ 25:.50✗) | 0.54 | DEPLOY — MARGINAL/FRAGILE |
| 126 | 0.69 | 41.6 | 0.58 | 1/6 (2020:.58✓ 21:.31✗ 22:.09✗ 23:.07✗ 24:.18✗ 25:.49✗) | 0.58 | RETIRE / REBUILD |

- **The shorter lookback is strictly worse on every axis** — it fails the FULL gate (0.69 < 1.0), blows the DD ceiling (41.6% > 25%), and clears only 1/6 windows. It is **not** a rescue; switching to it would retire the only live edge. **The deployed 252 is decisively the best grid point, and no in-grid change de-fragilises the strategy.** The fragility is therefore a property of the ts_momentum signal on this universe — confirming the recommendation to keep 252 as-is under the existing fragile-pass discipline.

## 4. Risk self-check

- **Overfitting:** zero new parameters invented. The sweep touched **one** existing grid axis (`lookback_days ∈ {126, 252}`, already in the spec). The recommendation is "no change," which has **zero overfit surface.** Note the deployed edge itself is a top-K-ranked signal (the 2026-06-07 fix that made the deployed strategy reproducibly TSMOM rather than a simulator tie-break artefact) — I re-ran on that fixed code, not the old non-reproducible version.
- **Lookahead / data-leakage:** the recert's LIVE fill uses a *marketable limit at prior_close×1.03 filling at the next-bar open* — no future bar informs the fill; the ATR stop is anchored to the same signal-day next-open as the entry (no post-gap stop-placement artefact). Costs are ADV-keyed effective-spread + sqrt impact, point-in-time. No leakage found.
- **Regime-dependence:** THIS is the honest weak spot, surfaced not hidden. The edge is **regime-concentrated** — strong in 2020/2021/2024 (trending/rebound years), weak (<0.5 OOS) in 2022/2023/2025. The per-window clause clears only because 3 of the 6 windows happen to be the strong ones. A forward regime resembling 2022–23 would very plausibly flip the clause to FAIL.
- **Cost-sensitivity:** NOT the binding risk here (unlike xs_reversal). Full-spread vs half-spread barely moves FULL Sharpe (1.35), and the adverse-fill STRESS row (+5bps slip, 85% fill) still clears (OOS-agg 1.26). The strategy is robust to cost; it is fragile to *regime*.
- **Sample-size:** ample — n=736 full / 525 OOS-agg (realistic fill), 802 (pure-MOO). Per-window n≈85–95 each. The fragility is dispersion across regimes, not small-sample noise.

## 5. Proposal card

| field | value |
|---|---|
| **Target setup** | `ts_momentum_liquid_us` (deployed `lookback_days=252`, `top_k=8`, `liquid_us_2026q2` 1178-ticker + SPY, monthly rebalance) |
| **The change** | **None — re-validation. Keep deployed 252 / top_k=8, DEPLOY-MARGINAL/FRAGILE status retained.** No in-grid param change improves robustness; `lookback_days=126` is strictly worse (RETIRE/REBUILD). |
| **Backtest config** | Realistic recert: LIVE marketable-limit prior_close×1.03 + FULL effective spread + OHLC fills + 8-cap; rolling 3y-IS/1y-OOS/1y-step WF (6 windows, 2020–2025) + FULL period + adverse-fill STRESS. Net gate: same cost model, last-30% OOS split. Universe: `liquid_us_2026q2` + SPY. Period 2017-01-01→2026-05-25. |
| **Before → after** | 2026-06-20 recert = FULL 1.35 / OOS-agg 1.23 / per-window 3/6 → DEPLOY-MARGINAL. Independent re-run today = FULL 1.35 / OOS-agg 1.23 / per-window 3/6 → DEPLOY-MARGINAL. **Verdict reproduces to the decimal.** Net gate KEEP (OOS 1.04) also reproduces. |
| **Artifact references** | §3a `2026-07-02-ts_momentum-fill-recert-groundtruth.md`; §3b `2026-07-02-ts_momentum-netgate-groundtruth.md`; §3c `2026-07-02-ts_momentum-lookback-sweep.md`/`.json`/`.py` |
| **Self-assessment** | **Confident on the verdict, scrutinize the edge.** Three harnesses reproduce the DEPLOY-MARGINAL verdict exactly; the recommendation to keep-as-is is well-evidenced. But the underlying edge is genuinely marginal + regime-concentrated — this is a "keep, do not upsize, keep the kill criterion armed" confirm, not a vote of confidence. |
| **Honesty check** | *Is every figure here reproducible from an attached artifact?* **Yes** — every number traces to a `ledgers/improvements/` artifact I ran on 2026-07-02. Nothing is asserted from memory. |

### Operator-relevant caveats (surfaced deliberately)
1. **This is the only live generic edge, and it is a fragile pass at the floor.** 3/6 windows clear by the slimmest margin (weakest passing window 0.54 vs 0.50 floor). Retain the 2026-06-20 operator guidance verbatim: conservative sizing on the $10k live-validation sleeve + a pre-committed kill criterion (retire if forward realized quarterly Sharpe tracks the weak windows). **Re-run this exact recert on every data / universe refresh** — the per-window clause is now the spec gate and must keep clearing.
2. **Same single-harness limitation as proposal #1, but less binding here:** the fill-recert (`simulate_walk_forward`) *does* carry both cost AND the per-window clause for this setup — so ts_momentum is actually validated more rigorously than xs_reversal was. The residual gap is only that the net-gate single-split (§3b) and the rolling recert (§3a) are two different splitters; they agree (KEEP / DEPLOY-MARGINAL), which is reassuring.
3. **The `pure_moo` FULL Sharpe (1.27) is a diagnostic only** — it enters names that gapped >3% above prior close, which the live marketable-limit cannot fill. Do NOT cite it as a live-achievable number; whether the >3% exclusion is a beneficial selection filter is an untested single-config hypothesis (flagged in the recert's FIX 4).

---
*Suggest-only. Nothing here is applied. Operator merges manually.*
