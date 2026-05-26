"""Thematic-portfolio deterministic-arithmetic tools (gate-3 build).

Per [[swing-thematic-portfolio-build-kickoff]] Weeks 3-4 module set + per
[[swing-thematic-portfolio-session-2-design-changes]] revisions. Sibling
to the existing swing-equity tool catalogue (`tools/*`), structurally
identical but operating on quarterly-rebalance + event-driven inputs
(SA LP 13F + ensemble 13Fs + Loop 1 reasoning output) instead of per-trade
OHLCV.

Modules:

* :mod:`tools.thematic_portfolio.sizer` — unified mirror sizer
  (``subagent_weight = 1.0 × sa_lp_weight_in_long_book × thematic_allocation``,
  hard-capped at 5% of total portfolio). Replaces the original tier-based
  bucket sizer per session-2 design change #1.

* :mod:`tools.thematic_portfolio.ensemble_overlap` — M1 position-set Jaccard
  + M3 rank-based ensemble triangulation + critic-trigger context (per
  session-2 design changes #3, #4, #5). M2 critic-outcome-alignment is
  paper-trade-phase only — requires accumulated 4-quarter critic decision
  history; deferred until Loop 1 has fired across ≥4 quarters.

* :mod:`tools.thematic_portfolio.drift_analysis` — Loop 6 Pass 1 (per
  [[swing-thematic-portfolio-loop6-prediction]]). Cross-period 13F drift
  profile per fund: new positions, exits, adds, trims, size-change
  percentile distributions. Pure arithmetic; feeds the Loop 6 LLM
  synthesis pass which forecasts SA LP's next-quarter 13F deltas
  (advisory only — watchlist enrichment, never auto-execution).

* :class:`Position` — shared dataclass mirroring an edgartools 13F infotable
  row. Long-book filter happens at the caller's edge; the sizer expects a
  pre-filtered long-only list.

Both modules follow the Phase 2 contract: pure functions returning
:class:`tools.contract.TraceEntry`; CLI entry points via ``python -m``;
no I/O beyond reading the 13F JSON files the caller passes by path.

Hard constraints (per the parent design):

* Specific position-fund pairs in any design note are illustrative, NOT
  contracts. These tools accept live 13F data per cycle; no constant
  encodes a specific position-fund pair.
* Ensemble triangulation uses RANK-based comparison, not notional-weighted —
  Light Street's Q1 2026 long book is $0.50B vs Coatue's $29.06B;
  notional weighting drowns Light Street's signal.
* The sizer's hard cap is 5% of total portfolio per Q7. No tier-based bucket
  caps — that was the old design. The current design preserves SA LP's
  natural concentration structure (e.g. top-6 cluster ~84% of thematic
  bucket on Q1 2026 data).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    """One 13F long-book position. Mirrors an edgartools infotable row.

    The caller is responsible for filtering to long-only (``put_call is None``)
    before passing to the sizer or ensemble-overlap tools. Pre-filtering keeps
    these tools pure-arithmetic and avoids embedding put_call semantics into
    sizing decisions.

    Attributes:
        ticker: trading symbol (e.g. ``"BE"``, ``"SNDK"``). Used as the
            primary key in all overlap + sizing computations.
        issuer_name: human-readable issuer name from the 13F filing
            (e.g. ``"BLOOM ENERGY CORP"``). Informational; never used as a key.
        cusip: 9-character CUSIP. Optional in this dataclass since some
            preprocessing pipelines may strip it; tickers are the join key.
        value_usd: position market value at the 13F period-end in USD
            (e.g. ``877300000.0`` for $877.3M).
    """

    ticker: str
    issuer_name: str
    value_usd: float
    cusip: str | None = None
