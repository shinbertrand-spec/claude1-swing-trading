"""Tier 3 real-world signal compilers for the thematic-portfolio subagent.

Per [[swing-thematic-portfolio-subagent-research]] § Inputs: Tier 3 signals
are deterministic real-world data points that Loop 1's regime classification
+ per-position synthesis cite as evidence — distinct from corpus artifacts
(Tier 1/2) which are LLM-readable text.

V1 ships:

* :mod:`tools.thematic_portfolio.tier3.power_sector` — curated snapshot of
  hyperscalers + AI-power-exposed utilities + power-infra equipment makers
  + crypto-miner pivot plays. Pulls price / TTM EPS / P/E / market cap /
  next-earnings-date via yfinance for ~16 tickers, writes a single JSON
  the Loop 1 prompt can cite by ticker.

Deferred to followup sessions:

* ``semiconductor_inventory.py`` — DRAM / HBM / lead-time pricing signals
  that bear on the SA LP put-complex thesis. Most useful data is behind
  trade-publication paywalls (TrendForce / DRAMeXchange); v2 will need
  a different source strategy.
* ``ai_capex_announcements.py`` — earnings-call commentary mining for
  hyperscaler AI capex guidance. Requires 10-Q MD&A text extraction
  via edgartools + regex passes; tractable but heavier than power_sector v1.
* ``energy_futures.py`` — natural gas / uranium / power forward curves.
  No-key public-API source TBD (CME has API but requires auth; alternative
  is parsing public futures-curve snapshots from EIA).
"""
from __future__ import annotations
