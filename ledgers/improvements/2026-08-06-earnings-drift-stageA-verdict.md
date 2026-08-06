# event_earnings_drift — Stage A VERDICT: RETIRE (0/8 survive the full chain)

**2026-08-06. Executes the build handoff's §5 report-back.** Authority spec:
`plans/2026-07-25-event-earnings-drift-candidate-spec.md`. Build committed at
`6914561`; grid report `journal/backtest/event_earnings_drift-stageA.md`; chain
detail `2026-08-06-earnings-drift-chain-steps2-6.md`.

## 1. PIT-CIK resolution (handoff §2.1)

**Option 1 — CIKs frozen into a universe sidecar**
(`tools/quant_strategies/_universes/liquid_us_2026q2_cik.yml`, commit `5cf9ddd`).
Resolution: today's `company_tickers.json` validated by earliest-filing-date ≤ window
end; failures chased **deterministically via EDGAR's classic browse endpoint** (its own
ticker table retains delisted mappings: AEP→4904, XOM→34088, VSCO, IAC, GTLS…), FTS
autocomplete as fallback. **1,177/1,178 resolved**; only NVRI carries `cik: null`
(no mapping anywhere; excluded, disclosed).
**Acceptance: coverage rose 96.5% → 97.7%** (1,151/1,178 on 2025 Q2) — the map does its
job. (The FTS-only chase was insufficient — the FTS index *reassigns* tickers to
successor entities, so "XOM" only reaches the 2026 holdco; the browse endpoint was the
deterministic fix.)

## 2. Frozen filter (handoff §2.2)

**Strict item-2.02** (8-K / 8-K/A, `items` ∋ 2.02, parseable `acceptanceDateTime`;
BMO < 09:30 ET / AMC ≥ 16:00 / else unknown→AMC; 3-day amendment collapse). Frozen and
**committed at `fffe05f` BEFORE any event extraction or grid run** — confirmed by commit
order (`fffe05f` → build `6914561` → results). The zero-dof choice: the wider
8.01/9.01 net was conceived after seeing which names it recovers, so it was not used.
Disclosed cost: ~1.7% of names/quarter (untagged-2.02 filing agents) never generate
events. Events built: **45,057** (2016–2025, ~4,500/yr — matches the spec's power
estimate), 98.3% with computed EAR, **unknown-timing share 6.5%** (< the 10%
escalation bar).

## 3. Stage A results per combo

**Step 1 — run_spec two-clause gate + enforced DSR (N=91):** 2 of 8 passed.

| Rank | ear_top_pct | history | hold | OOS Sharpe | |MDD|% | n | Gate |
|---|---|---|---|---|---|---|---|
| 1 | 20 | off | 21 | **1.21** | 23.1 | 517 | ✅ (DSR **0.997**) |
| 2 | 10 | on | 10 | **1.10** | 17.8 | 654 | ✅ |
| 3–8 | — | — | — | 0.50–0.98 | 15–25 | 428–902 | ❌ |

**Steps 2–6 — retirement-grade net-of-cost machinery (both passers):**

| | rank 1 | rank 2 |
|---|---|---|
| 2a net split replica (FULL / OOS) | 0.65 / 0.46 FAIL | 0.21 / 0.12 FAIL |
| 2b net walk-forward, LIVE fills | **0.60**, per-window **0/5** FAIL | **0.49**, 0/5 FAIL |
| 3 DSR on net curve (N=91) | **0.027** FAIL | **0.011** FAIL |
| 4 corr vs ts_momentum | +0.42 | +0.38 |
| 6 fill guard | fill **11%**; filled fwd **+1.57%** vs missed **+10.03%** → **MISMATCH** | fill 41%; +0.52% vs +6.91% → **MISMATCH** |

**Kill rule applied: RETIRE. No tuning, no grid-widening.** Step 5 (CPCV) not reached.
Stage B does **not** trigger (spec: "only if Stage A survives"). Rank 1 is *not* a
near-miss worth an operator carry-forward vote: 0/5 windows + the mismatch guard is a
structural fail, not a marginal one. No `deployable_setups.yml` row was ever written.

**The diagnosis — the pre-registered §2 guard fired, and it's the insider autopsy again,
worse.** The spec's §2 thesis was that EAR *inverts* the insider mismatch (the jump is
the filter, not the thing chased). **The data falsified the inversion**: the drift is
itself front-loaded into the overnight gap after the reaction session. The next-open
marketable entry (+3% chase cap) fills only 11% of top-quintile events — and the missed
ones ran **+10.0%** forward while the filled laggards did **+1.6%**. The anomaly is
real and large; it is not capturable by this framework's execution model. Same verdict
class as `event_insider_buying`: **execution-model mismatch → retire/redesign, not a
tuning target.** (The redesign — same-session/at-open entry — is the standing separate
doctrine item, which now has TWO event KINDs pointing at it.)

## 4. Survivorship statement (handoff §2.3, in its terms)

`liquid_us_2026q2` is current-extant membership. This trial is an **event study
conditioned on earnings-reaction history** — and the firms missing from a
current-extant universe are disproportionately those whose earnings history went badly
enough to delist or be acquired, **so the missing names are correlated with the
conditioning variable itself**. That cuts hardest at exactly the leg the trial
measures. Concretely observed: VRE and VSCO (delisted-ticker class) have no fetchable
price history, so their events carry no EAR and silently exit the conditioning pool.
Had Stage A survived, this caveat would have bounded the claim; as a RETIRE it works in
the verdict's favor (survivorship *flatters* results, and they still failed), but it is
stated here because the handoff requires it in the verdict, not as a footnote.

## 5. Surprises / method findings

1. **Step 1 passing was the surprise** — 2/8 cleared the two-clause gate with an
   enforced DSR of 0.997 against 91 trials, against the spec's own honest prior. It
   then evaporated at realistic fills. **Process lesson: run_spec's step-1 gate
   certifies a frictionless next-open fill; the binding net-of-cost/live-fill harness
   halved the Sharpe (1.21 → 0.60) and zeroed the windows (4/5 → 0/5).** Future event
   specs should treat step 1 as a screen, never a headline — or gate at step-2
   machinery directly.
2. **The missed-vs-filled spread (+10.0% vs +1.6%) is enormous** — larger than the
   insider KIND's (+40% vs +7.7% over 6 months; this is 21 days). The EAR drift
   anomaly looks *alive* in this universe — it just lives in the first overnight gap,
   which the framework's marketable-limit entry structurally cannot buy. This is the
   strongest evidence yet for the acceptance-time / at-open-entry doctrine review.
3. The trials registry was NOT re-incremented for chain steps 2–6: they re-evaluate
   the same 8 registered combos under the binding cost model, not new variants.
4. Pipeline numbers matched the spec's estimates almost exactly (~4,500 events/yr vs
   ~4,800 predicted; ~480 top-decile/yr as argued). The power fix worked — n=517–654
   per combo vs insider's n=59. The idea still failed. **Better-powered test, clean
   negative, family closed** — which the handoff pre-declared a good outcome.

## Disposition

- `tools/quant_strategies/event_earnings_drift.yml` → `status: retired` (verdict inline).
- Infrastructure retained (events pipeline, PIT-CIK sidecar + browse-edgar resolver,
  alignment tests) — reusable; the PIT-CIK lesson applies to every future EDGAR study.
- Next decision that is genuinely open: the **same-session entry doctrine review**, now
  motivated by two independent event families. Operator-owned; not started here.
