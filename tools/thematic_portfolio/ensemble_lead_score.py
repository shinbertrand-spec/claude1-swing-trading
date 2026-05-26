"""Ensemble-as-leading-indicator scoring — Loop 6 Pass 3 (deterministic).

Per [[swing-thematic-portfolio-loop6-prediction]] § Architecture sketch §
Pass 3. For each ticker held by ≥2 of {Altimeter, Coatue, Light Street}
but NOT in current SA LP long book, score:

    ensemble_lead_score(ticker) =
        n_ensemble_holders         * 0.4    # 2/3 = 0.8; 3/3 = 1.2
      + thesis_alignment_score     * 0.4    # 0.0 - 1.0
      + ensemble_added_this_quarter * 0.2   # 1 if any ensemble fund newly added, else 0

The formula is **v1-locked** per the gate-3 approved decisions section of the
design note (no parameter sweeps until ≥ 4 cycles of calibration data,
2026-05-26). Same for the sector-bucket scores (1.0 / 0.8 / 0.6 / 0.1) and
:data:`SECTOR_CLASSIFICATION_MAP`.

Output is a ranked candidate list with per-candidate component breakdown,
ready to feed into the Loop 6 LLM synthesis pass. The Loop 6 LLM pass can
add color where the deterministic bucket misclassifies — but the
deterministic score itself does not change.

Loop 6 is **advisory only** — watchlist enrichment, never auto-execution.
This module is a pure-computation primitive; the orchestrator at the
Loop 6 entry point owns the ``validation.no_auto_execution`` /
``validation.no_real_capital_consumer`` hard refusals (gate-3 decision #4).

CLI::

    uv run python -m tools.thematic_portfolio.ensemble_lead_score \\
        --sa-lp ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json \\
        --altimeter    ledgers/thematic/13f/altimeter/0001541617-2026-03-31-long.json \\
        --coatue       ledgers/thematic/13f/coatue/0001135730-2026-03-31-long.json \\
        --light-street ledgers/thematic/13f/light_street/0001569049-2026-03-31-long.json \\
        --altimeter-prior    ledgers/thematic/13f/altimeter/0001541617-2025-12-31-long.json \\
        --coatue-prior       ledgers/thematic/13f/coatue/0001135730-2025-12-31-long.json \\
        --light-street-prior ledgers/thematic/13f/light_street/0001569049-2025-12-31-long.json \\
        --period-latest 2026-03-31 --period-prior 2025-12-31
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from ..cli import emit
from ..contract import TraceEntry
from . import Position
from .sizer import load_long_book_from_json

TOOL = "tools/thematic_portfolio/ensemble_lead_score.py"

# ---------------------------------------------------------------------------
# v1-locked constants (no parameter sweeps until >=4 calibration cycles per
# gate-3 decision 2.3, codified 2026-05-26 in
# swing-thematic-portfolio-loop6-prediction.md § Gate-3 approved decisions)
# ---------------------------------------------------------------------------

ENSEMBLE_FUNDS: tuple[str, ...] = ("altimeter", "coatue", "light_street")
MIN_HOLDER_COUNT: int = 2

# Coefficients on the three score components — v1-locked.
N_HOLDERS_WEIGHT: float = 0.4
THESIS_ALIGNMENT_WEIGHT: float = 0.4
ENSEMBLE_ADD_WEIGHT: float = 0.2

# Sector bucket scores — v1-locked. SA LP's published thesis maps utility-grade
# power-infra to a 1.0 alignment, AI-chip-makers to 0.8 (selective — SA LP
# holds chip puts AND chip longs, so the alignment is real but conditional),
# hyperscalers to 0.6 (SA LP doesn't hold them; they're the buyers, not the
# bottleneck producers), and everything else to 0.1 (off-thesis).
SECTOR_BUCKET_AI_POWER_INFRA: str = "ai_power_infra"
SECTOR_BUCKET_AI_CHIP_MAKERS: str = "ai_chip_makers"
SECTOR_BUCKET_HYPERSCALERS: str = "hyperscalers"
SECTOR_BUCKET_OTHER: str = "other"

BUCKET_SCORES: Mapping[str, float] = {
    SECTOR_BUCKET_AI_POWER_INFRA: 1.0,
    SECTOR_BUCKET_AI_CHIP_MAKERS: 0.8,
    SECTOR_BUCKET_HYPERSCALERS: 0.6,
    SECTOR_BUCKET_OTHER: 0.1,
}

# Ticker → bucket lookup. Built to cover the SA LP + ensemble universe
# observed Q4 2025 → Q1 2026 plus expected near-neighbours. v1-locked.
# Anything not in this map defaults to SECTOR_BUCKET_OTHER. The Loop 6 LLM
# pass adds color on misclassifications; it does not change the score.
SECTOR_CLASSIFICATION_MAP: Mapping[str, str] = {
    # ---- AI-power-infra (utility data-center, fuel cells, grid build) ----
    "BE":   SECTOR_BUCKET_AI_POWER_INFRA,  # Bloom Energy — fuel cells
    "CEG":  SECTOR_BUCKET_AI_POWER_INFRA,  # Constellation — nuclear utility
    "VST":  SECTOR_BUCKET_AI_POWER_INFRA,  # Vistra — power gen
    "TLN":  SECTOR_BUCKET_AI_POWER_INFRA,  # Talen — nuclear utility
    "OKLO": SECTOR_BUCKET_AI_POWER_INFRA,  # Oklo — SMR
    "SMR":  SECTOR_BUCKET_AI_POWER_INFRA,  # NuScale — SMR
    "LEU":  SECTOR_BUCKET_AI_POWER_INFRA,  # Centrus Energy — enriched uranium
    "GEV":  SECTOR_BUCKET_AI_POWER_INFRA,  # GE Vernova — grid build
    "PWR":  SECTOR_BUCKET_AI_POWER_INFRA,  # Quanta Services — grid build
    "BW":   SECTOR_BUCKET_AI_POWER_INFRA,  # Babcock & Wilcox — energy systems
    "ETR":  SECTOR_BUCKET_AI_POWER_INFRA,  # Entergy
    "AES":  SECTOR_BUCKET_AI_POWER_INFRA,  # AES Corp
    "DUK":  SECTOR_BUCKET_AI_POWER_INFRA,  # Duke Energy
    "D":    SECTOR_BUCKET_AI_POWER_INFRA,  # Dominion
    "NRG":  SECTOR_BUCKET_AI_POWER_INFRA,  # NRG Energy
    "EXC":  SECTOR_BUCKET_AI_POWER_INFRA,  # Exelon
    "PCG":  SECTOR_BUCKET_AI_POWER_INFRA,  # PG&E
    "NEE":  SECTOR_BUCKET_AI_POWER_INFRA,  # NextEra
    "SO":   SECTOR_BUCKET_AI_POWER_INFRA,  # Southern Co
    "AEP":  SECTOR_BUCKET_AI_POWER_INFRA,  # American Electric Power
    "EIX":  SECTOR_BUCKET_AI_POWER_INFRA,  # Edison International
    # ---- AI-chip-makers (memory, foundry, design, semi-cap-equip) --------
    "NVDA": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "AMD":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "TSM":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "ASML": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "AMAT": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "LRCX": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "KLAC": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "MU":   SECTOR_BUCKET_AI_CHIP_MAKERS,
    "SNDK": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "WDC":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "AVGO": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "MRVL": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "ARM":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "INTC": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "QCOM": SECTOR_BUCKET_AI_CHIP_MAKERS,
    "ON":   SECTOR_BUCKET_AI_CHIP_MAKERS,
    "ADI":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "TXN":  SECTOR_BUCKET_AI_CHIP_MAKERS,
    "SMH":  SECTOR_BUCKET_AI_CHIP_MAKERS,  # ETF
    "SOXX": SECTOR_BUCKET_AI_CHIP_MAKERS,  # ETF
    # ---- Hyperscalers (compute buyers, not bottleneck producers) ---------
    "MSFT":  SECTOR_BUCKET_HYPERSCALERS,
    "AMZN":  SECTOR_BUCKET_HYPERSCALERS,
    "GOOG":  SECTOR_BUCKET_HYPERSCALERS,
    "GOOGL": SECTOR_BUCKET_HYPERSCALERS,
    "META":  SECTOR_BUCKET_HYPERSCALERS,
    "ORCL":  SECTOR_BUCKET_HYPERSCALERS,
    "IBM":   SECTOR_BUCKET_HYPERSCALERS,
    "CRWV":  SECTOR_BUCKET_HYPERSCALERS,  # CoreWeave — AI-cloud compute seller
}


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentBreakdown:
    """The three signal components that summed into total_score.

    Carried per-candidate so the Loop 6 LLM pass can cite which signal
    drove the score (and so a future calibration analysis can attribute
    precision per component).
    """

    n_ensemble_holders: int
    n_holders_term: float                 # n_ensemble_holders * 0.4
    thesis_alignment_bucket: str          # one of the SECTOR_BUCKET_* constants
    thesis_alignment_score: float         # one of the BUCKET_SCORES values
    thesis_alignment_term: float          # thesis_alignment_score * 0.4
    ensemble_added_this_quarter: bool
    ensemble_added_term: float            # 0.2 if ensemble_added else 0.0


@dataclass(frozen=True)
class EnsembleLeadCandidate:
    """One candidate ranked by ensemble_lead_score."""

    ticker: str
    issuer_name: str
    ensemble_holders: list[str]           # subset of ENSEMBLE_FUNDS, sorted
    newly_added_by: list[str]             # ensemble funds that newly added this quarter, sorted
    total_score: float
    components: ComponentBreakdown


@dataclass
class EnsembleLeadResult:
    """Loop 6 Pass 3 output."""

    period_latest: str
    period_prior: str | None              # None on first-ever firing
    sa_lp_universe_size: int
    ensemble_universe_sizes: dict[str, int]
    n_candidates: int
    candidates: list[EnsembleLeadCandidate]

    def to_dict(self) -> dict:
        return {
            "period_latest": self.period_latest,
            "period_prior": self.period_prior,
            "sa_lp_universe_size": self.sa_lp_universe_size,
            "ensemble_universe_sizes": dict(self.ensemble_universe_sizes),
            "n_candidates": self.n_candidates,
            "candidates": [
                {
                    "ticker": c.ticker,
                    "issuer_name": c.issuer_name,
                    "ensemble_holders": list(c.ensemble_holders),
                    "newly_added_by": list(c.newly_added_by),
                    "total_score": c.total_score,
                    "components": asdict(c.components),
                }
                for c in self.candidates
            ],
        }


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def classify_sector(ticker: str) -> str:
    """Look up the sector bucket for a ticker. Default ``other`` (0.1)."""
    return SECTOR_CLASSIFICATION_MAP.get(ticker, SECTOR_BUCKET_OTHER)


def _ticker_set(book: list[Position]) -> set[str]:
    return {p.ticker for p in book}


def _issuer_for_ticker(
    ticker: str, ensemble_books_latest: Mapping[str, list[Position]]
) -> str:
    """First-found issuer_name across the ensemble books for a ticker.

    Used as the human-readable label in the candidate output. Defensive
    fallback to the ticker itself if no ensemble book has issuer info.
    """
    for fund in ENSEMBLE_FUNDS:
        for p in ensemble_books_latest.get(fund, []):
            if p.ticker == ticker:
                return p.issuer_name
    return ticker


def compute_ensemble_lead_score(
    *,
    sa_lp_book: list[Position],
    ensemble_books_latest: Mapping[str, list[Position]],
    ensemble_books_prior: Mapping[str, list[Position]] | None = None,
    period_latest: str,
    period_prior: str | None = None,
) -> EnsembleLeadResult:
    """Compute the ranked candidate list for Loop 6 Pass 3.

    Args:
        sa_lp_book: SA LP long book in the latest period. Candidates ARE
            excluded if their ticker is in this set.
        ensemble_books_latest: dict mapping each ensemble fund slug
            (``"altimeter"`` / ``"coatue"`` / ``"light_street"``) to its
            long book in the latest period.
        ensemble_books_prior: optional dict of the same shape for the
            prior period. When None, the ``ensemble_added_this_quarter``
            signal is 0 for every candidate (no signal available on the
            first-ever firing).
        period_latest: ``"YYYY-MM-DD"`` of the latest period.
        period_prior: ``"YYYY-MM-DD"`` of the prior period. May be None
            iff ``ensemble_books_prior`` is None.

    Returns:
        An :class:`EnsembleLeadResult` with candidates sorted by
        ``total_score`` descending. Ties broken alphabetically by ticker
        for deterministic output.

    Raises:
        ValueError: ``ensemble_books_prior`` is non-None but missing one
            of the funds in ``ENSEMBLE_FUNDS`` (the partial-prior case
            is ambiguous and surfaces as an error rather than a silent
            zero).
    """
    if ensemble_books_prior is not None:
        missing = set(ENSEMBLE_FUNDS) - set(ensemble_books_prior)
        if missing:
            raise ValueError(
                f"ensemble_books_prior is missing funds {sorted(missing)}; "
                "pass either all-or-none for prior period."
            )

    sa_lp_tickers = _ticker_set(sa_lp_book)
    ensemble_tickers_latest: dict[str, set[str]] = {
        fund: _ticker_set(ensemble_books_latest.get(fund, []))
        for fund in ENSEMBLE_FUNDS
    }
    ensemble_tickers_prior: dict[str, set[str]] = (
        {fund: _ticker_set(ensemble_books_prior.get(fund, [])) for fund in ENSEMBLE_FUNDS}
        if ensemble_books_prior is not None else {fund: set() for fund in ENSEMBLE_FUNDS}
    )

    # Union of all ensemble-latest tickers, exclude SA LP, keep only ≥2-holder
    all_ensemble_latest_tickers: set[str] = set().union(*ensemble_tickers_latest.values())
    candidate_tickers: list[str] = []
    for ticker in all_ensemble_latest_tickers:
        if ticker in sa_lp_tickers:
            continue
        holders = [f for f in ENSEMBLE_FUNDS if ticker in ensemble_tickers_latest[f]]
        if len(holders) >= MIN_HOLDER_COUNT:
            candidate_tickers.append(ticker)

    candidates: list[EnsembleLeadCandidate] = []
    for ticker in candidate_tickers:
        holders = [f for f in ENSEMBLE_FUNDS if ticker in ensemble_tickers_latest[f]]
        # newly_added_by — only meaningful when prior period is known
        if ensemble_books_prior is not None:
            newly_added_by = [
                f for f in ENSEMBLE_FUNDS
                if ticker in ensemble_tickers_latest[f]
                and ticker not in ensemble_tickers_prior[f]
            ]
        else:
            newly_added_by = []

        n_holders = len(holders)
        bucket = classify_sector(ticker)
        alignment_score = BUCKET_SCORES[bucket]
        ensemble_added_bool = len(newly_added_by) > 0

        n_holders_term = n_holders * N_HOLDERS_WEIGHT
        thesis_alignment_term = alignment_score * THESIS_ALIGNMENT_WEIGHT
        ensemble_added_term = (
            ENSEMBLE_ADD_WEIGHT if ensemble_added_bool else 0.0
        )
        total = n_holders_term + thesis_alignment_term + ensemble_added_term

        components = ComponentBreakdown(
            n_ensemble_holders=n_holders,
            n_holders_term=n_holders_term,
            thesis_alignment_bucket=bucket,
            thesis_alignment_score=alignment_score,
            thesis_alignment_term=thesis_alignment_term,
            ensemble_added_this_quarter=ensemble_added_bool,
            ensemble_added_term=ensemble_added_term,
        )

        candidates.append(
            EnsembleLeadCandidate(
                ticker=ticker,
                issuer_name=_issuer_for_ticker(ticker, ensemble_books_latest),
                ensemble_holders=sorted(holders),
                newly_added_by=sorted(newly_added_by),
                total_score=total,
                components=components,
            )
        )

    # Deterministic sort: score desc, then ticker asc
    candidates.sort(key=lambda c: (-c.total_score, c.ticker))

    return EnsembleLeadResult(
        period_latest=period_latest,
        period_prior=period_prior,
        sa_lp_universe_size=len(sa_lp_tickers),
        ensemble_universe_sizes={
            fund: len(ensemble_tickers_latest[fund]) for fund in ENSEMBLE_FUNDS
        },
        n_candidates=len(candidates),
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# File-path wrapper + CLI
# ---------------------------------------------------------------------------


def compute_from_paths(
    *,
    sa_lp_path: Path,
    altimeter_path: Path,
    coatue_path: Path,
    light_street_path: Path,
    altimeter_prior_path: Path | None = None,
    coatue_prior_path: Path | None = None,
    light_street_prior_path: Path | None = None,
    period_latest: str,
    period_prior: str | None = None,
) -> TraceEntry:
    """File-path wrapper around :func:`compute_ensemble_lead_score`.

    Pass either all three prior-path arguments OR none; partial prior
    state is a configuration error.
    """
    prior_paths = (altimeter_prior_path, coatue_prior_path, light_street_prior_path)
    if any(p is not None for p in prior_paths) and not all(p is not None for p in prior_paths):
        raise ValueError(
            "Either provide ALL three ensemble prior paths or NONE; "
            "partial prior state is ambiguous."
        )

    sa_lp_book = load_long_book_from_json(sa_lp_path)
    ensemble_books_latest = {
        "altimeter": load_long_book_from_json(altimeter_path),
        "coatue": load_long_book_from_json(coatue_path),
        "light_street": load_long_book_from_json(light_street_path),
    }
    ensemble_books_prior: Mapping[str, list[Position]] | None
    if altimeter_prior_path is not None:
        ensemble_books_prior = {
            "altimeter": load_long_book_from_json(altimeter_prior_path),
            "coatue": load_long_book_from_json(coatue_prior_path),  # type: ignore[arg-type]
            "light_street": load_long_book_from_json(light_street_prior_path),  # type: ignore[arg-type]
        }
    else:
        ensemble_books_prior = None

    result = compute_ensemble_lead_score(
        sa_lp_book=sa_lp_book,
        ensemble_books_latest=ensemble_books_latest,
        ensemble_books_prior=ensemble_books_prior,
        period_latest=period_latest,
        period_prior=period_prior,
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "sa_lp_path": str(sa_lp_path),
            "altimeter_path": str(altimeter_path),
            "coatue_path": str(coatue_path),
            "light_street_path": str(light_street_path),
            "altimeter_prior_path": str(altimeter_prior_path) if altimeter_prior_path else None,
            "coatue_prior_path": str(coatue_prior_path) if coatue_prior_path else None,
            "light_street_prior_path": str(light_street_prior_path) if light_street_prior_path else None,
            "period_latest": period_latest,
            "period_prior": period_prior,
        },
        output=result.to_dict(),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.ensemble_lead_score",
        description=__doc__,
    )
    p.add_argument("--sa-lp", required=True, help="SA LP long-book JSON (latest period).")
    p.add_argument("--altimeter",    required=True, help="Altimeter long-book JSON (latest).")
    p.add_argument("--coatue",       required=True, help="Coatue long-book JSON (latest).")
    p.add_argument("--light-street", required=True, help="Light Street long-book JSON (latest).")
    p.add_argument("--altimeter-prior",    help="Altimeter long-book JSON (prior — optional).")
    p.add_argument("--coatue-prior",       help="Coatue long-book JSON (prior — optional).")
    p.add_argument("--light-street-prior", help="Light Street long-book JSON (prior — optional).")
    p.add_argument("--period-latest", required=True, help='"YYYY-MM-DD" of latest period.')
    p.add_argument("--period-prior", help='"YYYY-MM-DD" of prior period (optional).')
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    trace = compute_from_paths(
        sa_lp_path=Path(args.sa_lp),
        altimeter_path=Path(args.altimeter),
        coatue_path=Path(args.coatue),
        light_street_path=Path(args.light_street),
        altimeter_prior_path=Path(args.altimeter_prior) if args.altimeter_prior else None,
        coatue_prior_path=Path(args.coatue_prior) if args.coatue_prior else None,
        light_street_prior_path=Path(args.light_street_prior) if args.light_street_prior else None,
        period_latest=args.period_latest,
        period_prior=args.period_prior,
    )
    emit(trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
