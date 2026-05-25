---
type: source
created: 2026-05-24
ingested: 2026-05-24
title: "Existential Risk and Growth (2025 revision)"
author: Philip Trammell and Leopold Aschenbrenner
url: https://marginalrevolution.com/marginalrevolution/2025/12/existential-risk-and-growth-2.html
raw_path: web (WebFetch only — not clipped)
kind: paper
tags: [ai, aschenbrenner, trammell, existential-risk, growth-theory, agi-realism]
scope: cross
---

# Trammell & Aschenbrenner — Existential Risk and Growth (December 2025 revision)

> Source: Marginal Revolution post by Tyler Cowen (December 2025) flagging the revised Trammell-Aschenbrenner paper
> PDF: https://philiptrammell.com/static/Existential_Risk_and_Growth.pdf
> Cowen's note (entire substantive comment): *"Self-recommending."*

## TL;DR

- **The formal-economic backbone of Aschenbrenner's accelerationist policy stance.** GPI Working Paper No. 13-2024 (Dec 2025 revision). The math underneath the "AGI realism" manifesto.
- **Central claim:** *"The risk-minimizing technology growth rate is typically positive and may easily be high."*
- **The two mechanisms (corrected from official abstract):**
  1. **Time-at-risk-level mechanism** — *"acceleration decreases the time spent at each technology level."* Faster growth = shorter exposure window at any given hazard rate.
  2. **Existential Kuznets curve** — *"since a richer society is willing to sacrifice more for safety, optimal policy can yield an 'existential risk Kuznets curve', in which acceleration pulls forward periods when risk is low."*
- **Counterintuitive structural result:** acceleration *may* increase the hazard rate (risk per period) in the short run, but *decreases* the total probability the catastrophe ever occurs.
- **Policy implication (downstream):** *"accelerate, then spend the proceeds on safety"* dominates *"pause."* This is the formal-theoretic justification for SA LP's barbell architecture — be long AI capex (accelerate), allocate returns toward downside protection.

## Canonical abstract (Dec 2025 revision verbatim, per Stanford Digital Economy Lab publication page)

> "Technological development raises consumption but may pose existential risk. A growing literature studies this tradeoff in static settings where stagnation is perfectly safe. **But if any risky technology already exists, technological development can also lower risk indirectly in two ways: by speeding (1) technological solutions and/or (2) a 'Kuznets curve' in which wealth increases a planner's willingness to pay for safety.** The risk-minimizing technology growth rate, in light of these dynamics, is typically positive and may easily be high. **Below this rate, technological development poses no tradeoff between consumption and cumulative risk.**"

Publication date: **December 23, 2025.**

## What's NEW in the 2025 revision (vs the 2020 Aschenbrenner-solo version)

Per [[2026-05-24-2pml-trammell-aschenbrenner-review]] explicit contrast:

1. **New scope-condition scaffolding: "if any risky technology already exists."** The 2025 framing makes the result conditional on a "danger has already arrived" world (nuclear weapons + pandemic capability + bioengineering + AI). The 2020 version had this implicitly; the 2025 version foregrounds it as the *load-bearing precondition*. This matters because the result inverts in a no-risky-tech world: there, stagnation IS safe.
2. **"Acceleration reduces risk twice" framing.** Made explicit in 2025: faster growth (a) shortens time-at-each-hazard-level, (b) pulls forward the high-safety-investment regime via the Kuznets mechanism.
3. **"Below this rate, technological development poses no tradeoff between consumption and cumulative risk"** — the corollary that there exists a region where consumption growth is cumulative-risk-Pareto-optimal. Operationally: growth has a "free lunch" zone below the risk-minimizing rate.

## Model setup (per [[2026-05-24-ea-forum-trammell-erag-deep-dive-1]] + [[2026-05-24-reflective-altruism-erag-kuznets-curve]])

**Two-sector economy:**

| Variable | Meaning |
|---|---|
| C_t | consumption output at time t |
| H_t | safety output at time t |
| A_t | consumption technology level |
| B_t | safety technology level |
| L_ct | labor in consumption sector |
| L_ht | labor in safety sector |
| N_t | total population (grows exogenously at rate n̄) |
| s_t | fraction of scientists in consumption sector |
| l_t | fraction of workers in consumption sector |
| σ_t | fraction of population working as scientists |
| α > 0 | technology elasticity |
| φ < 1 | technology diminishing returns |
| λ | scientist-productivity exponent |
| γ | risk-aversion / utility-curvature parameter |
| ρ | pure time preference |
| ε > 0 | elasticity of existential risk to consumption |
| β > 0 | elasticity of existential risk to safety |

**Production:**
- Consumption goods: `C_t = A_t^α · L_ct`
- Safety goods: `H_t = B_t^α · L_ht`
- Technology growth: `dA/dt = S_λat · A_t^φ`; `dB/dt = S_λbt · B_t^φ`
- Population: `dN/dt = n̄ · N_t`

**Existential risk hazard rate:**
- `δ_t = δ̄ · C_t^ε · H_t^(-β)`
- Where δ̄ = baseline risk constant; ε > 0 = elasticity to consumption; β > 0 = elasticity to safety.

**Planner's problem:**
- Utility: `u(c_t) = ū + c_t^(1-γ) / (1-γ)` (constant elasticity of marginal utility).
- Agents discount by both survival probability M_t (cumulative no-catastrophe probability) AND pure time preference ρ.

## Three regime conditions (the load-bearing analytical result)

The model produces three distinct regimes based on the ε vs β relationship — the **scale effect of existential risk** (ε − β) determines the long-run trajectory:

### Robust World (ε ≤ β)
- Safety scales faster than consumption with technology.
- **Growth reduces existential risk** as long as safety spending doesn't decline exponentially.
- Long-run survival probability M_∞ > 0.
- **This is the "growth is unambiguously good for survival" regime.**

### Fragile World (ε > β, but not too far)
- Consumption scales faster than safety with technology.
- Outcome depends on γ (risk-aversion parameter):
  - **γ > 1:** labor shifts toward safety quickly enough as wealth grows → M_∞ > 0. (Kuznets curve fires; humanity survives.)
  - **γ ≤ 1:** labor doesn't shift fast enough → M_∞ = 0. (Kuznets curve too slow; doom.)
- **This is the "growth + risk-aversion-sufficient = survival" regime.**

### Hyper-Fragile World (ε ≫ β)
- Consumption scales *much* faster than safety.
- Formal condition: **(ε − β) / β > α · λ / (1 − φ)**.
- **No feasible safe allocation exists.** Growth cannot solve the problem regardless of γ.
- **This is the "no policy lever in this model can save you" regime.**

## Key quantitative result (from EA Forum Deep Dive #1)

> "For γ ≈ 1.1, reducing discount rate by 13 basis points achieves same safety gains as doubling consumption."

Operationally: **moral longtermism (low discount rate) may be more cost-effective per dollar than growth acceleration.** This is a non-obvious result that cuts against the pure "accelerate" reading of the paper.

## Policy implications

1. **Preventing stagnation is critical** — even brief growth slowdowns extend time spent in the high-risk zone; temporary booms followed by busts leave net harm.
2. **Sustained acceleration helps** — 30 years of accelerated growth (even if temporary) reduces cumulative existential risk via the Kuznets mechanism.
3. **Patience vs growth tradeoff is real** — see γ ≈ 1.1 result above. Both reducing discount rate AND accelerating growth move the same lever; the cheaper one in any given setting depends on local parameters.
4. **Fragility matters** — if ε ≫ β (Hyper-Fragile regime), growth cannot solve the problem; the policy frame must shift entirely.

## Critical limitations and adversarial readings

### Thorstad critique (per [[2026-05-24-reflective-altruism-erag-kuznets-curve]])

> "Thorstad argues the model treats consumption as the source of existential risk, but most existential risks derive from technological advancement (AI, bioweapons), not consumption. Revising the model to reflect this 'would lose all of the main theorems.'"

**This is load-bearing for the SA LP / thematic-portfolio subagent design.** The model's risk-driver-is-consumption assumption is exactly inverted from Aschenbrenner's own SA-LP-positioning logic, where the risk-driver IS the technology (frontier AI capability) and consumption is downstream. The mismatch suggests either:
- The model justifies the policy stance only loosely (the math says one thing; the policy stance assumes another).
- A revised model where risk is technology-driven would *strengthen* the case for safety-sector investment and *weaken* the case for raw acceleration — closer to the doomer position than the AGI-realist position.

The subagent's adversarial-critic loop ([[swing-thematic-portfolio-adversarial-critics]]) should incorporate the Thorstad critique as a **structural risk against the long-AI-capex leg** specifically. When the reasoning layer recommends sizing up on a capability-acceleration name, the Thorstad-derived critic should ask: *"is your justification flowing from consumption-as-risk-driver math, but the actual risk in this position is technology-as-risk-driver?"*

### Other adversarial readings to surface

- The Robust World assumption (ε ≤ β) is empirically untested — neither Aschenbrenner nor Trammell argues we're in this regime; the paper proves "growth is good IF we're in Robust or moderately-Fragile-with-high-γ world." Whether we are is the load-bearing empirical question.
- The Kuznets mechanism requires that "richer society shifts labor to safety" — empirically observable in some domains (pollution control), less in others (alignment research funding as fraction of AI capex).
- The model is single-agent / single-planner; it does not address coordination failure across nations (a separate Aschenbrenner concern in [[2026-05-24-aschenbrenner-sa-iiid-free-world-must-prevail]]).

## Operational interpretation

- The model formalizes a tradeoff between **per-period hazard rate** (which acceleration can raise) and **total cumulative-probability-of-catastrophe** (which acceleration can lower). The headline result is on the cumulative measure, not the period measure.
- The Kuznets curve framing makes safety investment **endogenous to wealth** — richer societies don't just consume more, they buy more safety. Accelerating *to* wealth therefore accelerates *into* the high-safety-investment regime.

**Available paper URLs:**
- Dec 2025 revision: https://philiptrammell.com/static/Existential_Risk_and_Growth.pdf
- GPI working-paper PDF: https://www.globalprioritiesinstitute.org/wp-content/uploads/Leopold-Aschenbrenner-and-Philip-Trammell-Existential-Risk-and-Growth-2.pdf
- Stanford Digital Economy Lab publication page (HTML, abstract verbatim): https://digitaleconomy.stanford.edu/publication/existential-risk-and-growth/
- AEA 2025 conference paper page: https://www.aeaweb.org/conference/2025/program/paper/Af8HRE23
- Earlier (2020-vintage) Aschenbrenner-only version: https://leopoldaschenbrenner.github.io/xriskandgrowth/ExistentialRiskAndGrowth050.pdf
- Trammell's solo 2021 follow-up "Existential Risk and Exogenous Growth": https://philiptrammell.com/static/ExistentialRiskAndExogenousGrowth.pdf

**Identifier:** GPI Working Paper No. 13-2024 (registered 2024; Dec 23, 2025 = current revision).

**Operational interpretation:**
- The model formalizes a tradeoff between **per-period hazard rate** (which acceleration can raise) and **total cumulative-probability-of-catastrophe** (which acceleration can lower). The headline result is on the cumulative measure, not the period measure.
- The Kuznets curve framing makes safety investment **endogenous to wealth** — richer societies don't just consume more, they buy more safety. Accelerating *to* wealth therefore accelerates *into* the high-safety-investment regime.
- Under "typical" parameter ranges, the risk-minimizing growth rate is positive (and "may easily be high"). Paper does not commit to a single numerical value in the abstract — calibration is parameter-regime-dependent.

**PDF full-text not yet readable** — WebFetch returns binary; local PDF extraction (`pdftoppm`) unavailable in sandbox. Available paper URLs:
- Dec 2025 revision: https://philiptrammell.com/static/Existential_Risk_and_Growth.pdf
- GPI working-paper site: https://www.globalprioritiesinstitute.org/wp-content/uploads/Leopold-Aschenbrenner-and-Philip-Trammell-Existential-Risk-and-Growth-2.pdf
- Earlier (2020-vintage) Aschenbrenner-only version: https://leopoldaschenbrenner.github.io/xriskandgrowth/ExistentialRiskAndGrowth050.pdf
- AEA 2025 conference paper page: https://www.aeaweb.org/conference/2025/program/paper/Af8HRE23

**Identifier:** GPI Working Paper No. 13-2024 (registered 2024; Dec 2025 = current revision).

**What's new in the 2025 revision (vs the 2024 working paper):** still unverified — needs the model setup and proof sections from PDF body to compare. Worth tracking on next ingest pass when PDF text-extraction is solved.

## Tyler Cowen's commentary

Cowen offers only *"Self-recommending"* as substantive comment. The endorsement carries weight given Cowen's history with Aschenbrenner (gave him an Emergent Ventures award at 17; counseled him to skip econ grad school). The lack of elaboration is itself signal — Cowen typically reserves "self-recommending" for papers he expects to become canonical.

## Why this matters for the thematic-portfolio subagent

The paper is the **theoretical justification** for the entire SA LP architecture:
1. Long AI capex = "accelerate the technology growth rate" (the risk-minimizing direction per the paper).
2. Short overheated multiples = "ensure economic returns flow to capital that can be reinvested in safety" rather than burned on multiple expansion.
3. The expected-100x quote in [[2026-05-24-aschenbrenner-fortune-october-2025]] is the *trading* expression of this theoretical position.

For the subagent's reasoning layer ([[swing-thematic-portfolio-subagent-research]] Loop 1), this paper is **load-bearing context.** The prompt that extracts Aschenbrenner's "decision rules" from the corpus should weight this paper alongside the 2024 essays — it is the formal underpinning of the policy stance.

## Notable quotes

> "The risk-minimizing technology growth rate is typically positive and may easily be high."

> (Tyler Cowen) "Self-recommending."

## Connections

- Cited [[philip-trammell]] (Oxford GPI economist; new entity) as co-author. Indicates the intellectual link between Aschenbrenner and Oxford GPI / longtermist economics circles.
- Reinforces [[agi-realism]] concept — provides the *quantitative* defense of the "accelerate-but-seriously" middle path between doomers and e/accs.
- Provides theoretical justification for [[situational-awareness-lp]]'s barbell architecture.
- Background: Trammell-Aschenbrenner 2024 working paper (earlier version, not yet ingested separately).
- Open question: whether the paper explicitly addresses *AI-specific* x-risk (versus generic existential risk) — needs PDF read to confirm.

## Open follow-ups

- **PDF not yet read.** Bertrand should grab https://philiptrammell.com/static/Existential_Risk_and_Growth.pdf directly and a full read should follow. Quantitative results section is the highest-priority skim target.
- Identify what changed in the 2025 revision vs. 2024 working paper.
- Cross-reference against Carl Shulman's published intelligence-explosion economics — Shulman's brain-equivalent-watts framing in [[2026-05-24-shulman-80k-hours-podcast]] is structurally consistent with the Trammell-Aschenbrenner growth-vs-risk model.

## Wiki updates triggered

- Created entity [[philip-trammell]] — Oxford GPI economist, co-author
- Extended [[leopold-aschenbrenner]] entity (post-2024 publication; intellectual genealogy via GPI)
- Extended [[agi-realism]] concept (formal-theoretic defense layer)
- Added entry to [[index.md]] and [[log.md]]
