# Post-mortem — paper-auto live trades: what went wrong / right + 2 mechanics fixes (SUGGEST-ONLY)

- **Pilot:** Claude1 paper-research self-improvement pilot (proposal #4, operator-requested)
- **Role:** proposal-drafter · suggest-only · no trade, no live edit, no code edit
- **Date:** 2026-07-26
- **Type:** **operational / forensic** post-mortem (NOT a strategy-parameter re-validation like #1–#3). Evidence is on-disk paper-auto ledgers + a live read-only account pull, not a strategy backtest. Every deterministic number is reproducible from the attached script; the one live-account number carries its command.
- **Scope note:** the two fixes below are **code/mechanics changes I cannot and do not apply** (the wall denies `tools/**`, `.claude/agents/**`, order paths). They are recommendations for the operator / main agent to implement.

---

## 0. Context — account reconciliation (live, read-only)
Pulled from Tiger paper account `...3806` on 2026-07-25 (command: `TigerClient().account_summary()` / `.positions()` / `.open_orders()`; paper-only, `allow_live=False`):
- **Net liquidation $1,137,735**; cash $1,109,687 (~97% idle); one open position **GO** (3,113 sh, +$3,014 unrealized) + one resting **STP SELL** stop. `is_paper: true`.
- Bot's `journal/paper-auto/positions.json` agrees on live holdings: GO = `starter`; AMD/CAT/GOOGL = `closed_unfilled` (they carry `unfilled: true` — DAY-expired, never entered). **No live position drift, no orphan orders.**
- ⚠ One **stale index tag** (itself a Finding-C symptom): positions.json marks **SO** `closed_unfilled`, but SO actually filled (broker fill $91.40 vs entry $89.21 → **+$918**, reconciled 2026-06-07). The ledger's broker-fill record is authoritative over the stale index. The account is healthy; the losses below are small vs the book.

## 1. What happened — the realized trade record
Artifact: [`2026-07-26-paper-auto-postmortem-forensics.md`](2026-07-26-paper-auto-postmortem-forensics.md) (regenerate: `uv run python ledgers/improvements/2026-07-26-paper-auto-postmortem-forensics.py`).

**9 filled closed trades — 3 wins / 6 losses — NET realized −$11,176.** Biggest losers MXL −$5,759 (clenow), VRT −$2,652, COIN −$2,109, VAL −$1,901; winners SO +$918, XOM +$580, WMT +$132.

## 2. Finding A (the core finding) — every loss came from a since-RETIRED strategy
- **9 of 9 filled trades were on strategies since retired / parked** (residual_momentum, xs_short_term_reversal[_liquid_us], clenow retired by the 2026-06-17 net-of-cost gate; connors_rsi2 parked 2026-06-09 by the concurrency-cap recheck). All 5 carry `hold: true` in `deployable_setups.yml`. Net −$11,176, 100% of it on now-held setups.
- **The one surviving live strategy (`ts_momentum_liquid_us`) filled ZERO trades** — its 3 orders (AMD/CAT/GOOGL) never filled, so it contributed **$0** and lost nothing.
- **Read:** this is not an execution failure — it is confirmation that the retired strategies genuinely didn't work, and the live paper P&L **corroborates the retirement decision**. All entries pre-dated the 2026-06-17 retirement, so the bot traded then-approved setups; the framework then correctly shut them off.
- **Recommendation A (verify, not change):** confirm the live scanner now excludes all `hold: true` setups (they should not place). Decide GO (the one open position, on retired `residual_momentum`): it is +$3,014 and protected by a stop at $8.43 (price $9.01) — let the stop ride or close, but do not add.

## 3. Finding B (mechanics fix #1) — a ratcheted protective stop was overridden into a loss
Evidence: `ledgers/paper-auto/COIN.yml` `notes` + `position_state`; COIN OHLC (verified); reconstructed in the forensic artifact §COIN.
- COIN entered $173.52 (213 sh), ran to ~+10%, and the ratchet lifted the protective stop to **$182.20 (= +5% locked in)** on 2026-05-29/30. It closed at **$163.62 (−5.7%) → −$2,109** on 2026-06-04 via the composer's `sell_50` (violations=2).
- **Two things went wrong, and the second is worse:**
  1. **The stop provided no protection.** COIN gapped below the $182.20 stop on 06-01 (5/29 close $189.03 → **6/01 open $179.21**, verified from COIN's OHLC) and traded sub-$182 for three sessions (6/01–6/03 lows $176 / $172 / $163), yet the position stayed open until the composer closed it 06-04. A working stop should have exited on the 06-01 gap. **Either the ratchet's cancel-then-place left the position unprotected, or Tiger-paper STP SELLs don't fire on gap-downs.**
  2. **The composer sells below the stop.** Per CLAUDE.md Session-3 design, the composer *"cancels resting stop"* and sells at bid−0.1% on any non-hold action, with **no floor at the current protective stop** — so a violations exit executes below a stop ratcheted to lock in gains.
- **Magnitude (critic-corrected, gap-aware):** a stop firing on the 06-01 gap fills ~$179.21 → ~**+$1,212**, so the realistic swing vs the actual −$2,109 is **~$3,321**. *(An earlier draft said ~$3,958 by assuming a fill at the $182.20 trigger despite the gap — corrected. Even ~$179 is optimistic, since the stop apparently didn't fire at all.)*
- **Second, stronger instance — MXL (the biggest loss):** MXL shows the same exit-mechanics failure twice. (i) On 2026-05-28 the composer marked the ledger closed at $98.91 but **the sell order never reached Tiger** — the position stayed live and *unprotected* (ledger said flat, broker said held). (ii) After being reopened + re-stopped, it was composer-closed at $90.69 on 05-29, but the **actual broker fill didn't land until 06-05 at $86.91** (a `zombie_flatten`). MXL's recorded −$5,759 is real cash, but **~$1,900 of it is execution drag from that buggy/delayed exit** (7 extra days exposed as MXL fell $90.69→$86.91), not the strategy. This is the clearest evidence for the fix below: **a ledger must never be marked closed until the broker confirms the fill.**
- **Recommendation B:** (i) **floor composer exits at the current protective stop** — if a composer sell would execute below the resting stop, keep the stop (smallest change: in `tools.auto_paper.exits.evaluate_exits`, gate the cancel-then-limit-sell on `limit_price >= current_stop`); (ii) **verify the ratcheted STP SELL is actually resting and fires on gap-downs** — a stop that silently doesn't fire is the bigger bug, and Rec B(i) alone won't fix it; AND (iii) **never mark a ledger closed until the broker confirms the fill** (the MXL 05-28 phantom close left a live, unprotected position).

## 4. Finding C (mechanics fix #2) — the P&L books don't agree; the live scoreboard isn't trustworthy
Three independent views of the same track disagree (forensic artifact §"Three P&L views"):
| View | Number | Why it differs |
|---|---|---|
| Direct-ledger (filled entry↔exit) | **−$11,176 / 9 trades** | the clean realized number |
| Performance module `n_realized` | **1 trade** | excludes every ledger missing `exit_price` |
| Critic-panel calibration | **−$26,750 / 10 joined** | intended-size / R-based; includes NFLX |
- Several closed ledgers are missing `exit_price` (AMD/CAT/GOOGL are `closed_unfilled` = never entered, correctly $0 — but the performance module can't tell "never filled" from "filled, exit unrecorded"). **NFLX** — the resolved runaway-short incident (fixed 2026-07-24) — carries a corrupted entry ($81.59 for a ~$1,000 name, 465 sh) and no clean exit, so its loss is unrecorded here.
- **MXL exit-price ambiguity compounds this:** the recorded −$5,759 uses the actual (delayed) broker fill $86.91; the composer-intended 05-29 close was $90.69 (≈ −$3,857). The net −$11,176 is real cash, but had MXL exited cleanly on 05-29 it would be ~−$9,274. Which fill is authoritative is exactly the Rec-C reconciliation.
- **Because of this, no live "how much are we earning" figure can be trusted yet** — the same reason `/auto-paper-perf` reports "+0.68%, 1 trade" while the real filled result is −$11,176.
- **Recommendation C:** (i) backfill `exit_price` on filled-and-closed ledgers from broker fill history; (ii) mark `closed_unfilled` distinctly so the performance module excludes them as "no trade" rather than "missing data"; (iii) re-derive NFLX's realized P&L from broker history. Then the three views should reconcile to one number.

## 5. Risk self-check
- **Overfitting:** none — this is a forensic accounting of realized trades, no parameters fit, no strategy tuned.
- **Lookahead / data-leakage:** N/A to a post-mortem; the reproducer reads only recorded fills/exits already on disk.
- **Sample-size:** small (9 filled trades) — Finding A's *direction* (all losses on retired setups; survivor filled nothing) is unambiguous, but 9 trades is too few to quantify a live edge. Do not over-read the −$11,176 as a strategy verdict; the strategy verdicts are the net-gate backtests (#1–#3).
- **Cost-sensitivity:** N/A (realized broker fills already include cost).
- **Overclaim check:** COIN's root cause (composer overrides stop) is confirmed from the ledger notes, not inferred. GO's status and the account totals are live-pulled, not assumed.

## 6. Proposal card
| field | value |
|---|---|
| **Target** | paper-auto track operational review (not a single strategy) |
| **The change** | **A** — verify retired setups are excluded live + decide GO (no code change, operator call). **B** — floor composer exits at the current protective stop AND verify the ratcheted STP fires on gap-downs (`tools.auto_paper.exits`). **C** — backfill/reconcile `exit_price` + distinguish `closed_unfilled` in the performance module. |
| **Evidence** | 9 filled trades, net −$11,176, 9/9 on now-held setups; COIN +5%-stop provided no protection (gapped through on 06-01, closed 06-04 at −5.7%), gap-aware swing ~$3,321; 3 disagreeing P&L views (−11,176 / 1 / −26,750). All from the attached deterministic reproducer + live account pull. |
| **Self-assessment** | **Confident** on the facts (reproducible); **B and C are the actionable fixes.** A is a confirm-and-decide, not a change. |
| **Honesty check** | Every deterministic figure regenerates from `2026-07-26-paper-auto-postmortem-forensics.py`; the account figures carry their live command. Nothing asserted from memory. |

### Artifact references
- Forensic reproducer: [`2026-07-26-paper-auto-postmortem-forensics.py`](2026-07-26-paper-auto-postmortem-forensics.py) + [`.md`](2026-07-26-paper-auto-postmortem-forensics.md)
- Source ledgers (in-repo): `ledgers/paper-auto/*.yml` (COIN.yml for Finding B); `journal/paper-auto/positions.json`
- Live account: `TigerClient().account_summary()/.positions()/.open_orders()` (read-only, paper)

## 7. Evaluation record + drafter corrections (2026-07-26)
Ran through the pilot's blind Judge + Critic (blind to each other).
- **Judge: SUGGEST** (4/4 critical met; 2 minor gaps — the reproducer's console-echo Unicode crash, and the §0 SO wording).
- **Critic: DEFECT-FOUND** — re-ran the reproducer + re-derived from raw ledgers (all core numbers reproduced), then caught that **Finding B's magnitude was overstated ~16%**: COIN gapped *through* the $182.20 stop (6/01 open $179.21), so the honored-stop counterfactual is ~+$1,212 / swing ~$3,321, not +$1,849 / $3,958. It also surfaced the stronger point that the stop appears not to have fired at all, and flagged the §0 SO inconsistency + the connors parking-date imprecision.
- **Drafter corrections applied (this file + the reproducer):** gap-aware COIN counterfactual (~$3,321) + the "stop provided no protection" finding + Rec B(ii); §0 SO clarified as a stale-index Finding-C symptom; §2 connors parked-date corrected; reproducer console-crash fixed (UTF-8) and its COIN block made gap-aware.
- **Fresh blind Critic re-check (corrected version): CLEAN.** A new instance re-ran the reproducer (clean, no crash) and independently re-derived every figure to the cent — net −$11,176 (3W/6L); COIN gap-aware +$1,212 / swing $3,321 (and confirmed $179.21 is *conservative*, since a stop could have fired even earlier at the 5/29 low $178.85). No defect. It surfaced one new residual — **MXL's exit-price ambiguity** ($86.91 recorded broker fill vs $90.69 composer-intended; net could be ~−$9,274) — now folded into Findings B (a second, clearer instance of the exit-mechanics bug) and C.
- **Status: Judge SUGGEST + fresh Critic CLEAN on the corrected version → eligible to surface; PENDING-OPERATOR** (merge/implementation is manual). Both Judge nits (reproducer crash, §0 SO) are fixed. The original DEFECT-FOUND is retained above for the audit trail — the pilot's value here is that the blind critic caught a real ~16% overstatement *before* it reached the operator, and a fresh critic confirmed the fix.

---

## 8. External-review corrections + implementation record (2026-07-27, operator-approved)

An operator-commissioned independent review (vault-side, adversarial) found **three substantive defects** that survived both blind critics, then the operator approved implementation. Retained here for the audit trail; the sections above are unedited.

1. **SO's +$918 was a phantom → net is −$12,094 over 8 trades (2W/6L), not −$11,176 over 9.** SO's own ledger notes said "expired unfilled" (agreeing with positions.json — the §0 "stale index" dismissal was backwards); only the 2026-06-07 *manual* backfill claimed a fill, with no order ID, and the entry carried the fill=limit phantom signature. A read-only broker-history pull (2026-06-01..11) settled it: the paper-auto BUY #43452026991822848 never filled; the $91.40 SELL belonged to a **framework-external 558-sh round trip** (order IDs absent from this repo — manual app trade on the shared account). Ledger corrected (`scripts/so_unfilled_correction.py`), forensics regenerated. *Critic lesson: re-derivation verifies arithmetic, not truth.*
2. **Finding C mis-caused the "1 trade" view; Rec C(i) recommended already-done work.** All 9 closed ledgers had `exit_price` (the 2026-07-24 backfill); the real cause was `performance.py` enumerating positions.json — which prunes closed rows — so its single counted trade was SO, the phantom. **Fixed 2026-07-27**: performance.py now enumerates the ledger directory, honors `unfilled: true` (new `n_unfilled` field), and counts `pending_close` as open. The three views now reconcile at −$12,094.
3. **Rec C(iii) was already done, and its premise was wrong twice.** NFLX's realized flatten P&L is recorded in `2026-07-24-nflx-naked-short-incident.md` (same directory): **+$129,393**, a *gain*, ruled a bug artifact that must never enter realized stats. The −$26,750 "critic-panel view" violates that ruling by construction and is retired as a broken artifact, not reconciled. ("~$1,000 name" was also wrong — NFLX traded ~$69–73 at the flatten.)
4. **Rec B(iii) was already implemented** (the `pending_close` machinery in exits.py/reconcile.py, with the MXL failure cited in its own docstring) — but reviewing it exposed a **worse live regression neither critic saw**: the Bug-2 idempotency guard counted the resting protective STP as an open SELL, so under the pending_close design (stop stays resting) **every composer exit on every stop-protected position was silently skipped as `sell_pending_duplicate` — sell-discipline was disabled track-wide**. Fixed 2026-07-27: `_open_sell_tickers` excludes STP orders. **Rec B(ii) implemented as a stop-breach watchdog**: last close below a broker-confirmed resting stop → forced `sell_100` via the working limit path + zombie-stop cancel. **Rec B(i) (floor composer exits at the stop) is REJECTED as specified**: in the COIN case it cites, the composer's below-stop sell was the only exit that worked — flooring it would have left the position captive to the broken stop. The watchdog implements B(i)'s intent with correct semantics.
5. **MXL "which fill is authoritative" is a category error, withdrawn**: the broker fill ($86.91, −$5,759) is the realized P&L, full stop; the composer-intended $90.69 belongs to execution-drag *attribution* (tools/auto_paper/execution_attribution.py, shipped 2026-07-24).
6. **Rec A**: hold:true exclusion re-confirmed (deployable filter + 2026-07-24 gate chain). **GO: operator decision = flatten** — pending manual execution (order placement sits behind a session permission rail; see review handoff).
7. **Pilot-meta applied**: the critic prompt gains a mandatory **provenance pass** (conflicting records = defect; broker record authoritative; fill=limit = phantom signature) and **prior-art pass** (recommended fixes checked against current code + sibling docs) — closing the two blind-spot classes this proposal demonstrated.

*Suggest-only at drafting time; items above were implemented only on explicit operator approval ("go", 2026-07-27).*
