# Candidate-sourcing triage — one non-covered candidate (2026-08-18)

**Per operator redirect (2026-08-18):** the binding constraint is now candidate
sourcing, not rules — the book holds nothing and its only candidate (FIX) is
covered-tier with no documented edge. Task: ONE non-covered candidate from the
two deferred questions (ownership layer, water), clearing §2.1–2.3 (R1 cold
entry, R2 sell cadence, R3 coverage tier) like anything else. This note is the
triage; the thesis build is the next unit of work — a rushed thesis is a
story, the exact failure mode the schema exists to prevent.

## Screens run (2026-08-18, script + web verification)

| Name | Question | Index member | Analysts | 6-mo rel vs SPY | ~18-mo rel | R3 tier | R1 cold |
|---|---|---|---|---|---|---|---|
| **ERII** (Energy Recovery) | water | **No** (small-cap, ~$700M–1B) | **3–4** | **−62.9pp** | −74.3pp | **neglected** | **cold** |
| MWA (Mueller Water) | water | S&P MidCap 400 | ~8 | −30.1pp | −20.3pp | mid | cold-ish |
| KBR | ownership/eng. | **unresolved** (likely S&P 400; verify at build) | ~10–12 | −19.9pp | −57.8pp | mid | cold |
| XYL (Xylem) | water | S&P 500 | high | −22.9pp | −35.5pp | covered | cold |
| VLTO (Veralto) | water | S&P 500 | high | −6.9pp | −33.5pp | covered | not cold |
| DLR / EQIX | ownership | S&P 500 | high | — | — | covered | — |
| NXT.AX / GMG.AX / Keppel DC | ownership | non-US listings | — | — | — | neglected but foreign | — |

## Verdict

**Selected for the build: ERII (Energy Recovery) — the water question's purest
listed expression.** It is the only shortlist name that passes BOTH screens
decisively: 3–4 analysts and no index membership (the exact tier where the
Ivković edge is documented), and −63pp relative over six months (cold by any
reading of R1 — attention proxy to be defined at build). The audit called
water "the cleanest unpriced item found anywhere in the audit" (PHO −1.14% /
FIW −0.78% vs thermal +119.8% on identical physics).

**The ownership-layer question stays a question.** Its US-listed true owners
are covered-tier (DLR/EQIX — exactly where R3 finds no edge); its non-covered
expressions are foreign-listed (NXT.AX/GMG.AX/Keppel DC), where the local
component of the documented edge is structurally absent for this operator and
tooling reach is weaker; KBR is a diluted engineering expression with
unresolved index status. Revisit if a US-listed, non-covered pure expression
appears.

## What the ERII build must survive (pre-registered before the build, so the
refutation search cannot soften)

1. **The audit's own counter:** the VOLUMES say water may be correctly priced
   — Arizona's whole DC fleet <0.1% of state water, Texas's 464 DCs 0.4%,
   six state laws all disclosure-not-restriction, Ireland's moratorium ended,
   Chile revived. The thesis must show why the ~12x relocation mechanism
   (closed-loop cooling moves water to the power plant: indirect 4.52 L/kWh
   vs direct 0.36; LBNL projects fleet intensity RISING to 0.45–0.48 by 2028)
   converts into REVENUE for ERII specifically, not just a true physical fact.
2. **Value capture:** ERII's core is pressure-exchanger energy recovery for
   desalination (SWRO). The DC link runs through water-stressed-region
   desal/reuse capex — an indirect chain with several hands between the DC
   and ERII's P&L. Who pays, why ERII, why not competed away.
3. **Why is it down 50%?** A −63pp relative move has a reason. The refutation
   search goes FIRST: guidance history, backlog, the CO2-segment bet,
   customer concentration, cash burn. If the drop is thesis-relevant damage,
   candidacy dies honestly.
4. **Keystone exposure:** a water thesis keyed to DC buildout inherits the
   same keystone as everything else — the monitor's unresolved channel-C
   firing applies to it from birth.

**Not built this session** — scope discipline: the triage is committed, the
build is queued with the 2026-08-31 clearing pass. trials.yml untouched (103).

---

## Post-triage gate (2026-08-18, operator) — ERII FAILS requirement (1). No build.

### Requirement 1 (ranked first): the causal chain, one sentence with a quantity

Attempted, with ERII's actual disclosures (web-verified this session):

> US datacentre load growth (+34–92 GW by 2028) adds power-plant
> cooling-water consumption at ~12× the direct draw (4.52 vs 0.36 L/kWh) —
> but that water is fresh cooling-tower makeup at US thermoelectric plants,
> while ERII's revenue is pressure exchangers for MENA seawater-RO
> megaprojects (Q2-2026 desal revenue $11.5M, **−57% YoY** on Gulf
> project delays, per the Q2-2026 earnings call) — **zero US SWRO projects
> in the public pipeline are datacentre-attributed, so the DC-attributable
> quantity in ERII's served market is ~$0 of revenue.**

The only chain that connects them is "datacentres stress municipal water →
(someday) US desal" — exactly the shape the gate pre-refutes with the
audit's own volumes (<0.1% of Arizona state water, 0.4% Texas). Said, and
stopped: **no chain, no build.** The audit's ~12× relocation mechanism is
real physics at the power plant; the water it moves is not the water ERII
monetizes — different molecules, different continents, different buyers
(Gulf sovereign water authorities, not US utilities or hyperscalers).

### Requirement 5: the water question CLOSES — as a result

**"Real mechanism, no neglected listed expression."** The relocation
mechanism stands (LBNL fleet intensity rising to 0.45–0.48 by 2028); the
neglected-tier listed names examined (ERII decisively, MWA thinly) do not
monetize it; the names that plausibly touch DC water economics (XYL, VLTO,
ECL/CoolIT) are covered-tier where R3 documents no edge. This is a result
and is recorded as one. Re-open only on a NEW mechanism-bearing listed
name, not on price action.

### Cold-screen justification: WITHDRAWN

R1 is **theme-attention timing, not single-stock drawdown** — a −63pp
relative move is an EVENT, not coldness, and the event is now identified:
the Q2-2026 revenue collapse (−57% YoY) on MENA megaproject delays — a
thesis-relevant business deterioration, precisely what the pre-registered
"why is it down 50%" refutation existed to catch. Re-derived separately,
on attention: the water THEME plausibly IS cold (the audit: water appears
in no hyperscaler capex-guidance bottleneck discussion; Ecolab FELL on its
CoolIT acquisition; six state laws all disclosure-only) — but theme
coldness without a mechanism nominates nothing (rules.yml
sourcing.screens_do_not_nominate, added this date).

### Precision corrections

- **Index-membership measurement, stated:** the Ivković criterion is
  **non-S&P-500 membership**, specifically. ERII is not an S&P 500 member
  (small-cap; Russell 2000 constituent; S&P 600 status not determined this
  session and irrelevant to the criterion — sub-index membership is
  context, not a disqualifier). The triage table's "Index member: No"
  meant, and now reads, "not S&P 500."
- **Return reconciliation:** the audit's −42.6% is trailing-12-month
  ABSOLUTE (≈ our −43.4% computed 2026-08-18, window a few days apart —
  consistent). The triage's −63pp was 6-month RELATIVE (−49.7% absolute
  vs SPY +13.1%). No contradiction: different windows, absolute vs
  relative — and the fact that the 6-month leg is steeper than the
  12-month locates the damage in the recent half, i.e., the Q2 event.

### Standing outcome

The quarterly sourcing outcome for 2026-Q3, as of this date: **no
qualified candidate** — a valid outcome per rules.yml. FIX remains the
book's only candidate (covered-tier, ~5% ceiling, entry blocked). The
ownership-layer question remains open but unexpressed; the water question
is closed as above.
