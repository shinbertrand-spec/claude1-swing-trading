"""Cross-period 13F drift analysis — Loop 6 Pass 1 (deterministic).

Per [[swing-thematic-portfolio-loop6-prediction]] § Architecture sketch §
Pass 1. Compares two periods of a fund's 13F long book and emits a
structured ``DriftProfile`` characterizing the fund's quarter-over-quarter
drift style: new positions, exits, adds (existing position weight ↑),
trims (existing position weight ↓), and size-change percentile
distributions.

The Loop 6 LLM synthesis pass (Pass 5, Opus 4.7) reads this output to
answer questions like "does SA LP typically full-position immediately
or scale in over 2 quarters?" Pure arithmetic over pre-loaded
:class:`Position` lists. No data fetch.

Loop 6 is **advisory only** — watchlist enrichment, never auto-execution.
This module is one input to that pipeline and itself imposes no
validation flags; the orchestrator at the Loop 6 entry point is
responsible for the ``validation.no_auto_execution`` /
``validation.no_real_capital_consumer`` hard refusals (gate-3 approved
decisions #4, codified 2026-05-26).

CLI::

    uv run python -m tools.thematic_portfolio.drift_analysis \\
        --latest ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json \\
        --prior  ledgers/thematic/13f/sa_lp/0002045724-2025-12-31-long.json \\
        --fund sa_lp \\
        --period-latest 2026-03-31 --period-prior 2025-12-31
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ..cli import emit
from ..contract import TraceEntry
from . import Position
from .sizer import load_long_book_from_json

TOOL = "tools/thematic_portfolio/drift_analysis.py"


@dataclass(frozen=True)
class PositionDelta:
    """One ticker that appears in both periods, with the value delta."""

    ticker: str
    issuer_name: str
    value_prior_usd: float
    value_latest_usd: float
    delta_usd: float
    pct_change: float | None  # None when prior == 0 (defensive; should not occur for adds/trims)


@dataclass(frozen=True)
class SizeChangeDistribution:
    """Percentile distribution over a list of pct_change values."""

    n: int
    p25: float | None
    p50: float | None
    p75: float | None


@dataclass
class DriftProfile:
    """Cross-period drift summary for one fund.

    All ticker lists are sorted deterministically: new_positions + exits
    by ``value_usd`` desc; adds + trims by absolute ``delta_usd`` desc
    (most material first).
    """

    fund: str
    period_latest: str
    period_prior: str
    n_positions_latest: int
    n_positions_prior: int
    total_value_latest_usd: float
    total_value_prior_usd: float
    new_positions: list[Position]
    exits: list[Position]
    adds: list[PositionDelta]
    trims: list[PositionDelta]
    unchanged: list[PositionDelta]
    adds_distribution: SizeChangeDistribution
    trims_distribution: SizeChangeDistribution

    def to_dict(self) -> dict:
        return {
            "fund": self.fund,
            "period_latest": self.period_latest,
            "period_prior": self.period_prior,
            "n_positions_latest": self.n_positions_latest,
            "n_positions_prior": self.n_positions_prior,
            "total_value_latest_usd": self.total_value_latest_usd,
            "total_value_prior_usd": self.total_value_prior_usd,
            "new_positions": [asdict(p) for p in self.new_positions],
            "exits": [asdict(p) for p in self.exits],
            "adds": [asdict(d) for d in self.adds],
            "trims": [asdict(d) for d in self.trims],
            "unchanged": [asdict(d) for d in self.unchanged],
            "adds_distribution": asdict(self.adds_distribution),
            "trims_distribution": asdict(self.trims_distribution),
        }


def _index_by_ticker(book: list[Position]) -> dict[str, Position]:
    """Last-write-wins ticker → Position map.

    13F long books should not have duplicates per ticker (the corpus
    ingester collapses dual-class CUSIPs at extract time), but defensive
    last-write-wins is harmless: it leaves drift_analysis robust to any
    upstream deduplication regression.
    """
    return {p.ticker: p for p in book}


def _safe_pct_change(prior: float, latest: float) -> float | None:
    """``(latest - prior) / prior`` with 0 → None.

    None is the honest value when the denominator is 0; the LLM pass
    should treat it as "no signal" rather than coerce it to infinity.
    """
    if prior == 0.0:
        return None
    return (latest - prior) / prior


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile over a pre-sorted list. ``None`` if empty.

    Matches numpy's default (method='linear') behavior, implemented in
    stdlib to keep the dependency surface tiny.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    rank = pct * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _build_distribution(deltas: list[PositionDelta]) -> SizeChangeDistribution:
    """Compute n + p25 + p50 + p75 over the pct_change values of ``deltas``.

    Skips entries with ``pct_change is None`` (zero-denominator cases).
    """
    pcts = sorted(d.pct_change for d in deltas if d.pct_change is not None)
    return SizeChangeDistribution(
        n=len(pcts),
        p25=_percentile(pcts, 0.25),
        p50=_percentile(pcts, 0.50),
        p75=_percentile(pcts, 0.75),
    )


def compute_drift_profile(
    *,
    latest_book: list[Position],
    prior_book: list[Position],
    fund: str,
    period_latest: str,
    period_prior: str,
) -> DriftProfile:
    """Compute the :class:`DriftProfile` for one fund across two 13F periods.

    Args:
        latest_book: positions from the more-recent 13F (e.g. Q1 2026).
        prior_book: positions from the prior 13F (e.g. Q4 2025).
        fund: fund slug (``"sa_lp"`` / ``"altimeter"`` / ``"coatue"`` /
            ``"light_street"``). Informational only — used for the
            output's ``fund:`` field.
        period_latest: ``"YYYY-MM-DD"`` of the latest period.
        period_prior: ``"YYYY-MM-DD"`` of the prior period.

    Returns:
        A :class:`DriftProfile` ready to feed into the Loop 6 LLM pass.
    """
    latest_idx = _index_by_ticker(latest_book)
    prior_idx = _index_by_ticker(prior_book)

    latest_tickers = set(latest_idx)
    prior_tickers = set(prior_idx)

    new_position_tickers = latest_tickers - prior_tickers
    exit_tickers = prior_tickers - latest_tickers
    overlap_tickers = latest_tickers & prior_tickers

    new_positions = sorted(
        (latest_idx[t] for t in new_position_tickers),
        key=lambda p: -p.value_usd,
    )
    exits = sorted(
        (prior_idx[t] for t in exit_tickers),
        key=lambda p: -p.value_usd,
    )

    adds: list[PositionDelta] = []
    trims: list[PositionDelta] = []
    unchanged: list[PositionDelta] = []
    for ticker in overlap_tickers:
        prior_pos = prior_idx[ticker]
        latest_pos = latest_idx[ticker]
        delta_usd = latest_pos.value_usd - prior_pos.value_usd
        pct = _safe_pct_change(prior_pos.value_usd, latest_pos.value_usd)
        record = PositionDelta(
            ticker=ticker,
            issuer_name=latest_pos.issuer_name,
            value_prior_usd=prior_pos.value_usd,
            value_latest_usd=latest_pos.value_usd,
            delta_usd=delta_usd,
            pct_change=pct,
        )
        if delta_usd > 0:
            adds.append(record)
        elif delta_usd < 0:
            trims.append(record)
        else:
            unchanged.append(record)

    adds.sort(key=lambda d: -d.delta_usd)
    trims.sort(key=lambda d: d.delta_usd)  # most-negative first
    unchanged.sort(key=lambda d: d.ticker)

    return DriftProfile(
        fund=fund,
        period_latest=period_latest,
        period_prior=period_prior,
        n_positions_latest=len(latest_book),
        n_positions_prior=len(prior_book),
        total_value_latest_usd=sum(p.value_usd for p in latest_book),
        total_value_prior_usd=sum(p.value_usd for p in prior_book),
        new_positions=new_positions,
        exits=exits,
        adds=adds,
        trims=trims,
        unchanged=unchanged,
        adds_distribution=_build_distribution(adds),
        trims_distribution=_build_distribution(trims),
    )


def compute_from_paths(
    *,
    latest_path: Path,
    prior_path: Path,
    fund: str,
    period_latest: str,
    period_prior: str,
) -> TraceEntry:
    """File-path wrapper around :func:`compute_drift_profile`.

    Loads both books via :func:`load_long_book_from_json` and returns a
    :class:`tools.contract.TraceEntry` ready to ledger.
    """
    latest_book = load_long_book_from_json(latest_path)
    prior_book = load_long_book_from_json(prior_path)
    profile = compute_drift_profile(
        latest_book=latest_book,
        prior_book=prior_book,
        fund=fund,
        period_latest=period_latest,
        period_prior=period_prior,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={
            "latest_path": str(latest_path),
            "prior_path": str(prior_path),
            "fund": fund,
            "period_latest": period_latest,
            "period_prior": period_prior,
        },
        output=profile.to_dict(),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.drift_analysis",
        description=__doc__,
    )
    p.add_argument(
        "--latest", required=True,
        help="Path to the more-recent 13F long-book JSON.",
    )
    p.add_argument(
        "--prior", required=True,
        help="Path to the prior-period 13F long-book JSON.",
    )
    p.add_argument(
        "--fund", required=True,
        help="Fund slug (sa_lp / altimeter / coatue / light_street).",
    )
    p.add_argument(
        "--period-latest", required=True,
        help='Latest period date "YYYY-MM-DD".',
    )
    p.add_argument(
        "--period-prior", required=True,
        help='Prior period date "YYYY-MM-DD".',
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    trace = compute_from_paths(
        latest_path=Path(args.latest),
        prior_path=Path(args.prior),
        fund=args.fund,
        period_latest=args.period_latest,
        period_prior=args.period_prior,
    )
    emit(trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
