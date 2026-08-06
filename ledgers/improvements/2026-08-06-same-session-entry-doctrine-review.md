# Same-session entry doctrine review — Task 0 (2026-08-06)

Executes `Obsidian/Output/2026-08-06-claude1-same-session-entry-doctrine-review-handoff.md`.
Scope per §5: measurement + verdict only — no KIND re-test, no trials.yml entry, no
order-placement code changes, nothing un-retired.

## Pre-registered stopping rule (handoff §2, WRITTEN BEFORE ANY NUMBER WAS COMPUTED)

**Task 0:** re-measure the missed-event cohort's forward return from a **realistically
achievable fill price** — (i) the actual opening print of the entry session, and
(ii) open + a realistic slippage assumption — instead of the signal-date reference
(`pivot` = reaction-session close), which is confirmed at
`tools/backtest/portfolio_simulator.py:387` to be the reference behind the verdicts'
+10.0% / +40% missed-cohort figures (i.e., those figures INCLUDE the overnight gap).

| Result | Decision |
|---|---|
| Gap-adjusted missed-cohort return **collapses toward the filled cohort** (under **~2× filled**) | **STOP. Doctrine review closes NO-GO.** The anomaly was never reachable; the +3% chase cap was correctly declining a move it could not buy. Both KINDs closed permanently. |
| Gap-adjusted return **remains ≥ ~2× filled AND economically meaningful net of a realistic opening spread** | Proceed to the §3 doctrine analysis (mechanism / broker capability / cost model / blast radius / failure symmetry). |

Method commitments, pre-registered with the rule:
- **Cohorts:** the retired `event_earnings_drift` rank-1 combo (ear_top_pct=20,
  history_condition=off, max_hold_days=21) — primary, best-powered; and the retired
  `event_insider_buying` cohort (its events file is reconstructible) — secondary.
  Signal sets regenerated from the frozen events files + retired specs; this is a
  MEASUREMENT of the existing cohorts, not a re-test (no simulator, no equity curve,
  no gate arithmetic).
- **Missed vs filled split:** exactly the live rule — `_compute_fill` under
  FILL_MARKETABLE_LIMIT with the +3% momentum buffer, pivot = close on/before
  entry_date. Missed = no fill on the entry bar.
- **Forward return, all cohorts, apples-to-apples:** unconditional H-bar
  close-to-reference return over the KIND's max_hold (21 bars / 126 bars), no stop,
  gross. References compared: (a) pivot (replicates the verdicts' pre-gap number),
  (b) **entry-session actual open** (achievable via a market-on-open order),
  (c) open + **20 bps** slippage (liquid-universe opening-auction assumption),
  (d) open + **50 bps** (stress). Filled cohort measured from its modeled fill
  (= the open, when open ≤ limit) for the same H.
- The ~2× comparison uses (c) — open + 20 bps — vs the filled cohort's mean.
- Slippage figures are assumptions for THIS measurement only; if the review proceeds,
  §3 item 3 re-parameterises the cost model properly.

## RESULTS — pending (nothing below this line existed before the measurement ran)

### event_earnings_drift rank1 (ear20/off/21)

- signals 8125 -> filled 7880 / missed 245 (fill rate 97%) · H=21 bars, unconditional, gross
- missed-cohort overnight gap paid at open: mean **+4.63%** (median +3.86%)

| cohort / reference | n | mean fwd % | median fwd % |
|---|---|---|---|
| FILLED (from modeled fill) | 7880 | +1.75 | +1.30 |
| missed, from **pivot** (pre-gap — the verdicts' number) | 245 | +10.03 | +6.42 |
| missed, from **open** (achievable, MOO) | 245 | +5.10 | +2.44 |
| missed, from **open + 20 bps** (pre-registered comparator) | 245 | +4.89 | +2.23 |
| missed, from open + 50 bps (stress) | 245 | +4.58 | +1.93 |

- **Stopping-rule ratio: gap-adjusted missed (open+20bps) / filled = +4.89% / +1.75% = 2.79x** (rule: <~2x -> collapse)

### event_insider_buying (retired params)

- signals 843 -> filled 714 / missed 129 (fill rate 85%) · H=126 bars, unconditional, gross
- missed-cohort overnight gap paid at open: mean **+5.65%** (median +4.65%)

| cohort / reference | n | mean fwd % | median fwd % |
|---|---|---|---|
| FILLED (from modeled fill) | 714 | +11.82 | +4.04 |
| missed, from **pivot** (pre-gap — the verdicts' number) | 129 | +40.55 | +17.89 |
| missed, from **open** (achievable, MOO) | 129 | +32.58 | +12.68 |
| missed, from **open + 20 bps** (pre-registered comparator) | 129 | +32.32 | +12.46 |
| missed, from open + 50 bps (stress) | 129 | +31.92 | +12.12 |

- **Stopping-rule ratio: gap-adjusted missed (open+20bps) / filled = +32.32% / +11.82% = 2.73x** (rule: <~2x -> collapse)

## Decision produced by the pre-registered rule

**PROCEED to §3.** Both cohorts clear the bar: earnings-drift **2.79×**, insider
**2.73×** (≥ ~2×), and the gap-adjusted returns are economically meaningful net of the
opening spread (+4.89%/21d, +32.3%/126d; both survive the 50 bps stress). Roughly half
the headline "missed" return was gap illusion (+10.0% → +4.9%; +40.6% → +32.3%) — but
what remains is real, reachable money the +3% chase cap declines.

## The reframe Task 0 exposed (correction-of-emphasis to the Stage A verdict, row 9)

**The price-missed cohort is small: 245 of 8,125 earnings-drift signals (3%); 129 of
843 insider signals (15%).** The simulator's "11% fill rate" was dominated by the
**8-position capacity cap**, not the chase cap — most signals were declined for lack of
a slot, not price. Two consequences, stated plainly:

1. **An entry-model change could NOT have rescued either retired KIND.** They died
   primarily of capacity + cost on a modest filled-cohort edge (+1.75% gross/21d),
   with price-adverse-selection only at the margin. Both stay retired on their own
   terms — independent of §4's resurrection guard, the economics wouldn't have
   flipped. (The Stage A verdict's "fills only 11%" phrasing conflated the two decline
   reasons; this artifact is the correction of record.)
2. **The doctrine's real beneficiary is not the dead events — it is `ts_momentum`
   live-fill fidelity.** Its backtest certifies unconditional next-open fills; the live
   path approximates them with a 9:35 DAY marketable limit + 3% cap — already flagged
   as fill-path risks R1 (9:35 vs open print) and R2 (cap skips the strongest gappers)
   in `2026-08-04-ts_momentum-fill-path-diagnostic.md`. An opening-auction entry makes
   the live fill EQUAL the certified fill.

## §3 analysis

**1. Mechanism: LOO — auction limit order** (limit-on-open participating in the opening
auction; fills at the opening print when open ≤ limit). NOT market-on-open — a MOO is a
market order and CLAUDE.md's "never place a market order" hard rule stands. The LOO
limit retains a chase cap (limit = pivot × (1+cap)); what changes is WHERE the fill
happens (the auction print, i.e., exactly the "open" reference Task 0 measured) rather
than the 9:35 continuous book.

**2. Broker capability: SDK-verified; account test RUN (operator-authorized, post-review)
— near-confirmed, session-window gated.** The installed `tigeropen` SDK exposes
`auction_limit_order` (`common/util/order_utils.py:104`) and `auction_market_order`
(`:119`). `TigerClient` does not wrap them (today: LMT + STP only).

Capability test 2026-08-06 (`2026-08-06-auction-order-capability-test.py`; paper account,
1 share F, limit 20% below market = unfillable-by-design, cancel-after-accept):
- Order constructed as **type AL, TIF DAY** — the SDK produces a well-formed auction order.
- API response (verbatim): `ApiException(1200, 'standard account response error
  (bad_request:Only limit orders can be placed during pre market or post market)')` —
  a **session-window rejection, not an unsupported-order-type rejection**. The account
  plumbing recognized the AL order and objected to WHEN, not WHAT.
- Side note discovered: the device currently lacks the US **quote** entitlement
  (`get_briefs` → code=4 permission denied) — a separate permission domain from trading;
  the probe uses cached daily data for its reference price.

**Remaining verification: re-run the identical probe during US regular trading hours**
(21:30–04:00 SGT). ACCEPTED → capability confirmed, cancel fires; a type-level rejection
then → not supported, doctrine falls back to a plain wide-limit DAY order placed at
09:30:00 (inferior — misses the auction print — but closes most of the R1 gap).

**3. Cost model (specification only, per §5):** for auction entries, replace the
marketable full-spread cross with an opening-auction slippage parameter on the open
print: **base 20 bps** (liquid universe) / **35 bps** on earnings-morning names /
**50 bps** stress band; ADV-tilt sizing unchanged; no intrabar chase component. To be
implemented as a `fill_model="auction_limit"` mode in the portfolio simulator ONLY when
a future trial needs it — any such trial registers fresh in `trials.yml` at the
then-current count (§4).

**4. Blast radius:** `ts_momentum_liquid_us` is the only live deployable and the only
setup whose certified fill is the open — it is the sole (and positive) blast-radius
member: LOO would REMOVE the known R1/R2 fidelity gaps rather than degrade anything.
Reversion kinds rest at the pivot by design and are untouched. No currently-passing
strategy is made worse.

**5. Failure symmetry:** entering the missed cohort at the open pays the gap on losers
too — visible in the median-vs-mean spread (median +2.23% vs mean +4.89%: the tail is
right-skewed but the median stays positive net of gap+slippage). For `ts_momentum` the
symmetric cost is bounded by the retained LOO cap — the change moves fill TIMING to the
auction, it does not widen the chase (widening the cap is a separate decision, gated on
the R2 skip-rate evidence from live rebalances).

## VERDICT: **GO — narrow**

The stopping rule passed, so the review does not close NO-GO — but the honest scope is
narrow: **adopt same-session (opening-auction LOO) entry as a framework capability
SPEC, motivated by live-fill fidelity for open-certified setups (`ts_momentum`), not as
an event-strategy revival.** Both retired KINDs stay retired; any future re-test under
the new entry model is a new priced trial (§4). Operator-gated next steps, in order:

1. **One paper test `auction_limit_order`** to verify account support (operator
   authorizes; ~2 minutes at any market open).
2. If supported: wrap in `TigerClient` + expose as an opt-in entry mode in the
   placement path (code change — separate authorization; touches the live order path).
3. Decide whether `ts_momentum` switches to LOO — informed by the ~2026-08-13 rebalance
   at-bat (if it under-fills per R1, this spec is the ready remedy; if it fills clean,
   the switch is optional fidelity, not a fix).

Scope confirmation: no KIND re-tested, no trials registered, no order code changed,
nothing un-retired. Measurement script: `2026-08-06-same-session-entry-task0.py`.

