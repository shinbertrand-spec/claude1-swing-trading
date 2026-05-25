"""Tier 3 real-world signal compilers for the thematic-portfolio subagent.

Per [[swing-thematic-portfolio-subagent-research]] § Inputs: Tier 3 signals
are deterministic real-world data points that Loop 1's regime classification
+ per-position synthesis cite as evidence — distinct from corpus artifacts
(Tier 1/2) which are LLM-readable text.

V1 ships:

* :mod:`tools.thematic_portfolio.tier3.power_sector` — curated snapshot of
  hyperscalers + AI-power-exposed utilities + power-infra equipment makers
  + crypto-miner pivot plays. Pulls price / TTM EPS / P/E / market cap /
  next-earnings-date via yfinance for ~20 tickers.
* :mod:`tools.thematic_portfolio.tier3.ai_capex_announcements` — annual
  capex trend (PaymentsToAcquirePropertyPlantAndEquipment, with raw-XBRL
  fallback for issuers like AMZN that use PaymentsToAcquireProductiveAssets
  post-2017) for the 5 named hyperscalers via edgartools.
* :mod:`tools.thematic_portfolio.tier3.energy_futures` — last close + 30d
  / 90d / YTD % change for ~11 energy-input symbols (Henry Hub natgas +
  uranium ETFs + WTI crude + utility ETFs as the no-key power-price proxy)
  via yfinance. Aggregate ``thesis_signal`` classifies the AI-power-cost
  pull as supportive / mixed / weakening / no_data.

Deferred to followup sessions:

* ``semiconductor_inventory.py`` — DRAM / HBM / lead-time pricing signals
  that bear on the SA LP put-complex thesis. Most useful data is behind
  trade-publication paywalls (TrendForce / DRAMeXchange); v2 will need
  a different source strategy.
"""
from __future__ import annotations
