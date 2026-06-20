"""Theme / cluster concentration cap — the correlation control.

Per CLAUDE.md Hard Rules (reconciled 2026-06-20): no single correlated theme
may exceed ~30% of net liq in aggregate, CROSS-TRACK, even when each name is
individually risk-sized. Per-name risk-parity sizing (``position_sizer``) bounds
*idiosyncratic* dollar risk; it does nothing about several correlated names
(e.g. the AI-momentum complex) gapping down together — that is one bet.

The arithmetic is deterministic and lives here (Requirement 2 — never in agent
prose). Membership comes from the curated map at ``tools/_themes/clusters.yml``
(decision LOCKED 2026-06-20 — a hard rule needs auditable membership, not a
drifting correlation number). A theme is INTENTIONALLY BROADER than a sector
ETF, so this is NOT redundant with the 20% per-sector cap.

Two layers call this:

* ``tools.auto_paper.pipeline._check_track_limits`` (paper-auto track)
* the ``risk-and-compliance`` Gate-4 hard-rule check (discretionary track)

Both pass the SAME cross-track book set so the cap measures total exposure to
the theme regardless of which book holds the name.

CLI::

    uv run python -m tools.cluster_concentration \\
        --ticker NVDA --proposed-cost 9000 --account 100000
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/cluster_concentration.py"

DEFAULT_CLUSTER_CAP_PCT = 0.30

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THEME_MAP_PATH = _ROOT / "tools" / "_themes" / "clusters.yml"

# Default cross-track book set — both positions.json files.
DEFAULT_BOOK_PATHS = (
    _ROOT / "journal" / "positions.json",
    _ROOT / "journal" / "paper-auto" / "positions.json",
)

# Terminal stages that are NOT live exposure (mirror pipeline._CLOSED_STAGES).
_CLOSED_STAGES = {"closed", "closed_unfilled"}


def load_theme_map(path: str | Path | None = None) -> dict[str, str]:
    """Return a flat ``ticker -> theme`` map inverted from the curated YAML.

    A ticker that appears in two themes is a config error (one bet can't be
    two clusters); the last-wins, but such overlap should not exist in the map.
    """
    p = Path(path) if path is not None else DEFAULT_THEME_MAP_PATH
    doc = yaml.safe_load(p.read_text()) or {}
    out: dict[str, str] = {}
    for theme, body in (doc.get("themes") or {}).items():
        for t in (body or {}).get("tickers", []) or []:
            out[str(t).upper()] = theme
    return out


def theme_of(ticker: str, theme_map: dict[str, str]) -> Optional[str]:
    """Return the theme a ticker belongs to, or ``None`` if untagged."""
    return theme_map.get(ticker.upper())


def position_value(p: dict[str, Any]) -> float:
    """Cost-basis value of a position row: shares × entry_price.

    Matches the valuation the pipeline sector cap uses. Missing/None fields
    contribute 0 (fail toward not-inflating the cluster).
    """
    try:
        return float(p.get("shares") or 0) * float(p.get("entry_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_open(p: dict[str, Any]) -> bool:
    return (p.get("stage") or "").strip().lower() not in _CLOSED_STAGES


def sum_cluster_cost(
    theme: str,
    books: Iterable[list[dict[str, Any]]],
    theme_map: dict[str, str],
) -> float:
    """Sum cost-basis value of all OPEN positions in ``theme`` across all books.

    ``books`` is an iterable of position-row lists (one per track). Cross-track
    by construction — pass both ``positions.json`` lists to measure the whole
    correlated bet.
    """
    total = 0.0
    for book in books:
        for p in book or []:
            if not _is_open(p):
                continue
            tkr = str(p.get("ticker") or "").upper()
            if theme_map.get(tkr) == theme:
                total += position_value(p)
    return total


def compute(
    *,
    theme: Optional[str],
    proposed_cost_usd: float,
    account_value_usd: float,
    existing_same_cluster_cost_usd: float,
    cap_pct: float = DEFAULT_CLUSTER_CAP_PCT,
) -> TraceEntry:
    """Pure arithmetic: does the proposed trade breach the theme cluster cap?

    ``existing_same_cluster_cost_usd`` is supplied by the caller (summed across
    both books) — exactly as ``_check_track_limits`` sums same-sector capital
    for the 20% sector cap. The proposed cost is added on top: total exposure
    after the trade = existing + proposed (an add to a held name double-counts
    nothing, since the held shares are in ``existing`` and the new order is
    ``proposed``).

    A ``theme`` of ``None`` (untagged ticker) is a clean pass — no cluster to
    cap.
    """
    if account_value_usd <= 0:
        raise ValueError(f"account_value_usd must be positive; got {account_value_usd}")

    if theme is None:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "theme": None,
                "proposed_cost_usd": proposed_cost_usd,
                "account_value_usd": account_value_usd,
            },
            output={
                "theme": None,
                "breach": False,
                "binding_constraint": "no_cluster",
                "cluster_total_usd": proposed_cost_usd,
                "cluster_pct": proposed_cost_usd / account_value_usd,
                "cap_pct": cap_pct,
            },
        )

    cluster_total = existing_same_cluster_cost_usd + proposed_cost_usd
    cluster_pct = cluster_total / account_value_usd
    breach = cluster_pct > cap_pct

    return TraceEntry(
        tool=TOOL,
        inputs={
            "theme": theme,
            "proposed_cost_usd": proposed_cost_usd,
            "account_value_usd": account_value_usd,
            "existing_same_cluster_cost_usd": existing_same_cluster_cost_usd,
            "cap_pct": cap_pct,
        },
        output={
            "theme": theme,
            "breach": breach,
            "binding_constraint": "theme_cluster_cap" if breach else "within_cluster_cap",
            "existing_same_cluster_cost_usd": existing_same_cluster_cost_usd,
            "proposed_cost_usd": proposed_cost_usd,
            "cluster_total_usd": cluster_total,
            "cluster_pct": cluster_pct,
            "cap_pct": cap_pct,
        },
    )


def compute_from_books(
    *,
    ticker: str,
    proposed_cost_usd: float,
    account_value_usd: float,
    books: Iterable[list[dict[str, Any]]],
    theme_map: dict[str, str] | None = None,
    cap_pct: float = DEFAULT_CLUSTER_CAP_PCT,
) -> TraceEntry:
    """Convenience: resolve the ticker's theme, sum existing same-cluster cost
    across ``books``, and run :func:`compute`.
    """
    tmap = theme_map if theme_map is not None else load_theme_map()
    theme = theme_of(ticker, tmap)
    existing = sum_cluster_cost(theme, books, tmap) if theme is not None else 0.0
    return compute(
        theme=theme,
        proposed_cost_usd=proposed_cost_usd,
        account_value_usd=account_value_usd,
        existing_same_cluster_cost_usd=existing,
        cap_pct=cap_pct,
    )


def _load_book(path: str | Path) -> list[dict[str, Any]]:
    import json

    try:
        return json.loads(Path(path).read_text()).get("positions", []) or []
    except (FileNotFoundError, ValueError):
        return []


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.cluster_concentration",
        description="Theme/cluster concentration cap (cross-track correlation control).",
    )
    p.add_argument("--ticker", required=True)
    p.add_argument("--proposed-cost", type=float, required=True, dest="proposed_cost_usd")
    p.add_argument("--account", type=float, required=True, dest="account_value_usd")
    p.add_argument("--cap-pct", type=float, default=DEFAULT_CLUSTER_CAP_PCT)
    p.add_argument(
        "--book",
        action="append",
        dest="books",
        help="path to a positions.json; repeatable. Defaults to both tracks.",
    )
    args = p.parse_args()

    book_paths = args.books if args.books else [str(x) for x in DEFAULT_BOOK_PATHS]
    books = [_load_book(bp) for bp in book_paths]
    emit(
        compute_from_books(
            ticker=args.ticker,
            proposed_cost_usd=args.proposed_cost_usd,
            account_value_usd=args.account_value_usd,
            books=books,
            cap_pct=args.cap_pct,
        )
    )


if __name__ == "__main__":
    main()
