---
type: source
created: 2026-05-24
ingested: 2026-05-24
title: "Situational Awareness — IIIa. Racing to the Trillion-Dollar Cluster"
author: Leopold Aschenbrenner
url: https://situational-awareness.ai/racing-to-the-trillion-dollar-cluster/
raw_path: raw/2026-05-24-aschenbrenner-sa-iiia-trillion-dollar-cluster.md
kind: article
tags: [ai, agi, infrastructure, compute, energy, aschenbrenner]
scope: cross
---

# Racing to the Trillion-Dollar Cluster — Chapter IIIa

> Source: `raw/2026-05-24-aschenbrenner-sa-iiia-trillion-dollar-cluster.md`
> Ingested: 2026-05-24
> Author: [[leopold-aschenbrenner]]

## TL;DR

- **The industrial mobilization chapter.** AI will drive multi-trillion-dollar capex by 2030 — making Manhattan and Apollo look modest. The Bay-Area-software-economy frame radically understates this.
- **Cluster trajectory:** $500M GPT-4 cluster (2022) → $10s of billions / 1GW (2026) → $100s of billions / 10GW (2028) → **$1T+ / 100GW (2030, >20% of US electricity)**.
- **Power is the binding constraint, not chips.** TSMC can supply the silicon if CoWoS/HBM ramp. The choke point is 100GW of new continuous power.
- **100GW is geologically/industrially doable in the US via Marcellus/Utica shale alone.** Barrier is regulatory (NEPA, FERC, climate commitments), not physical. Aschenbrenner: regulatory inertia must be overcome — clusters MUST be built in the US for national security.
- **AI revenue catches up:** $100B+/yr by ~2026 (OpenAI run-rate doubling every ~6 months; Office add-on math).
- **Geopolitical bottom line:** datacenters planned today host AGI tomorrow. They must be in US/allied democracies, not Middle Eastern autocracies.

## Key claims

- **Cluster-build cost ≠ GPU-rental cost.** ~$500M GPT-4 cluster = TCO including amortization; GPUs are ~50% of capex (rest: power, datacenter, cooling, networking). B100/H100 FLOP/$ improves ~1.5× per generation; expect <10× FLOP/$ improvement per decade.
- **Total AI investment will be $1T/yr by 2027.** 2024 baseline: $150-200B (Nvidia $100B, big-tech capex growing $50-100B/yr). AMD forecast $400B AI chips alone by 2027.
- **Power as choke point.** US electricity generation grew 5% in the last decade. Utilities project 4.7% over next 5 years — wildly underestimating. 100GW = >20% of US production.
- **100GW from Marcellus shale alone is feasible:** ~36 BCF/day Marcellus production could generate 150GW continuous (250GW combined-cycle). Requires ~1,200 new wells (current 40 drilling rigs can drill in <1 year). Natural-gas plants ~$1K/kW capex → 100GW = ~$100B + 2-yr build.
- **AI revenue trajectory:** OpenAI $1B run-rate Aug 2023 → $2B Feb 2024 (~6-month doubling). Naive extrapolation → $10B late 2024 / early 2025. Microsoft Office 350M paid seats × 1/3 adoption × $100/mo = $140B/yr from one product.
- **Visible cluster acquisitions:** Zuckerberg 350K H100s. Amazon 1GW campus next to nuclear plant. Kuwait rumor: 1.4M H100-equivalent cluster (2026-scale). Microsoft/OpenAI rumored $100B cluster (2028, ~ISS scale).
- **National-security argument:** "We cannot make the same mistake again" (US Mideast energy dependence). Infrastructure in autocracies = irreversible weight-theft risk + capricious-dictator superintelligence.

## Notable quotes

> "The race to AGI won't just play out in code and behind laptops — it'll be a race to mobilize America's industrial might. Unlike anything else we've recently seen come out of Silicon Valley, AI is a massive industrial process: each new model requires a giant new cluster, soon giant new power plants, and eventually giant new chip fabs."

> "Behind the scenes, the most staggering techno-capital acceleration has been put into motion."

> "'Where do I find 10GW?' is a favorite topic of conversation in SF. What any compute guy is thinking about is securing power, land, permitting, and datacenter construction."

> "$1T/year of total AI investment by 2027 seems outrageous. But it's worth taking a look at other historical reference classes... At $1T/year, AI investment would be about 3% of GDP."

> "American national security must come first, before the allure of free-flowing Middle Eastern cash, arcane regulation, or even, yes, admirable climate commitments."

## Cluster trajectory table

| Year | H100-equiv | Capex | Power | Reference scale |
|---|---|---|---|---|
| 2022 (GPT-4) | ~10K | ~$500M | ~10 MW | 10K homes |
| 2024 | ~100K | $billions | ~100 MW | 100K homes |
| 2026 | ~1M | $10s of B | ~1 GW | Hoover Dam / large nuclear reactor |
| 2028 | ~10M | $100s of B | ~10 GW | small/medium US state |
| 2030 | ~100M | $1T+ | ~100 GW | >20% of US electricity |

## Specific quantitative claims

- Nvidia datacenter revenue: ~$14B annualized (FY2023) → ~$90B annualized (Q1 FY2025).
- Overall AI investment: 2024 $150-200B → 2027 $1T → 2030 $8T.
- H100 power draw: 700W per GPU; ~1.4kW with datacenter overhead.
- Marcellus shale: ~36 BCF/day → 150GW continuous / 250GW combined-cycle.
- Wells required for 100GW: ~1,200; 40 rigs at 3 wells/month = <1 year.
- Big-tech capex (recent): MS $50B+, Google $50B+, AWS $40B+, Meta $40B+ annually; combined growth $50-100B/yr.
- US electricity production: ~4,250 TWh/yr. 100GW cluster = 876 TWh/yr = 20.6% of total.
- OpenAI revenue: $1B run-rate Aug 2023 → $2B Feb 2024.
- Zuckerberg's H100 acquisition: 350K H100s.
- TSMC wafer capacity: ~400K wafers/month (5/3/7nm combined); AI chips ~5-10% of annual production (2024).
- Microsoft Office subscribers: ~350M.
- Natural-gas plant capex: ~$1K/kW for 100GW.
- Manhattan/Apollo: ~0.4% GDP (~$100B modern dollars). $1T/yr AI = 3% GDP.

## Connections

- Core to [[the-project]] (Ch IV — the institutional vehicle that runs these clusters).
- Sets up [[2026-05-24-aschenbrenner-sa-iiib-lock-down-the-labs|Ch IIIb]] (these clusters need defending).
- Sets up [[2026-05-24-aschenbrenner-sa-iiid-free-world-must-prevail|Ch IIId]] (geopolitics of where they're built).
- Concept seeds: [[techno-capital-acceleration]], [[total-cost-of-ownership-tco]], [[cowos]] (chip-on-wafer-on-substrate), [[hbm]] (high-bandwidth memory).
- Future entity targets: [[tsmc]], [[nvidia]], [[amd]], [[intel]], [[mark-zuckerberg]], [[microsoft]], [[meta]], [[amazon]], [[stargate-cluster]].
- Place targets: Marcellus/Utica shale, Pennsylvania, [[middle-east]] (as cluster siting alternative).

## Wiki updates triggered

- Extended [[the-project]] concept (infrastructure layer)
- Linked from [[leopold-aschenbrenner]] entity
- Added to [[index.md]] and [[log.md]]
