"""P/E expansion late-stage warning (per ``swing-sell-discipline.md``).

"P/E expansion doubled or more during late-stage price action" is an
additional sell warning per the operational note — late-cycle buyers are
paying up regardless of valuation.

Two entry points:

* :func:`compute` — pure. Caller supplies ``baseline_pe`` + ``current_pe``.
* :func:`compute_from_ticker` — fetches TTM EPS via
  :mod:`tools.fundamentals.edgar_eps` and current Close via
  :mod:`tools.data`, then delegates to :func:`compute`. The caller supplies
  the entry price (or entry date — both work).

Swing-horizon caveat: TTM EPS changes slowly relative to a 2-6 week swing
position. ``compute_from_ticker`` uses the CURRENT TTM EPS as the baseline
denominator for both ``baseline_pe`` and ``current_pe``. This means
``current_pe / baseline_pe`` ≈ ``current_price / entry_price`` over short
holds — the signal effectively fires when the position has run roughly
2x off entry. That captures the doctrine's "late-cycle buyers paying up"
energy without requiring a historical TTM EPS time-series.

CLI::

    uv run python -m tools.pe_expansion_check --baseline 18.0 --current 38.0
    uv run python -m tools.pe_expansion_check --ticker AAPL --entry-price 180.00 --current-price 240.00
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/pe_expansion_check.py"

DEFAULT_EXPANSION_THRESHOLD = 2.0   # doubled


def compute(
    baseline_pe: float,
    current_pe: float,
    threshold_ratio: float = DEFAULT_EXPANSION_THRESHOLD,
) -> TraceEntry:
    """Did the P/E ratio double (or by ``threshold_ratio``) since baseline?

    Args:
        baseline_pe: P/E earlier in the move (e.g. at base breakout).
        current_pe: current P/E.
        threshold_ratio: expansion multiple that fires the warning.
            Default 2.0 (doubled).

    Raises:
        ValueError: if either P/E is non-positive (negative-earnings or
            missing-data conditions caller must handle separately).
    """
    if baseline_pe <= 0:
        raise ValueError(f"baseline_pe must be positive; got {baseline_pe}")
    if current_pe <= 0:
        raise ValueError(f"current_pe must be positive; got {current_pe}")
    expansion_ratio = current_pe / baseline_pe
    expanded = expansion_ratio >= threshold_ratio
    return TraceEntry(
        tool=TOOL,
        inputs={
            "baseline_pe": baseline_pe,
            "current_pe": current_pe,
            "threshold_ratio": threshold_ratio,
            "v1_preliminary": True,
        },
        output={
            "pe_expanded": expanded,
            "expansion_ratio": expansion_ratio,
            "warning_late_stage": expanded,
            "v1_preliminary_flag": True,
        },
    )


def compute_from_ticker(
    ticker: str,
    entry_price: float,
    current_price: float | None = None,
    threshold_ratio: float = DEFAULT_EXPANSION_THRESHOLD,
    *,
    _ttm_eps_fetcher=None,
    _ohlcv_fetcher=None,
) -> TraceEntry:
    """Fetch TTM EPS via EDGAR and compute P/E expansion vs ``entry_price``.

    Args:
        ticker: US-listed equity.
        entry_price: position entry price (defines baseline_pe).
        current_price: latest price. When None, fetched as the last
            Close from :func:`tools.data.fetch_ohlcv` ``period="5d"``.
        threshold_ratio: expansion multiple that fires the warning.
            Default 2.0 (doubled).
        _ttm_eps_fetcher: test seam — function taking a ticker string
            and returning an :class:`~tools.fundamentals.edgar_eps.EPSResult`.
        _ohlcv_fetcher: test seam — function taking ``(ticker, period,
            interval)`` and returning a ``DataFetchResult``.

    Returns:
        :class:`TraceEntry` whose ``output`` shape extends
        :func:`compute` with ``ticker``, ``ttm_eps``, ``entry_price``,
        ``current_price``, ``baseline_pe``, ``current_pe``, ``eps_source``.

        When TTM EPS is non-positive (negative earnings) or the fetch
        fails, the entry returns ``pe_expanded: False`` with a
        ``reason`` explaining why — the caller can still consume the
        output rather than handling exceptions.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive; got {entry_price}")

    # TTM EPS fetch — surface failure cleanly as pe_expanded=False
    eps_source = "edgartools:us-gaap:NetIncomeLoss/diluted_shares"
    try:
        if _ttm_eps_fetcher is None:
            from .fundamentals.edgar_eps import fetch_ttm_eps as _ttm_eps_fetcher  # type: ignore[no-redef]
        eps_result = _ttm_eps_fetcher(ticker)
    except Exception as exc:  # noqa: BLE001 — adapter can throw broadly
        return TraceEntry(
            tool=TOOL,
            inputs={
                "ticker": ticker.upper(),
                "entry_price": entry_price,
                "current_price": current_price,
                "threshold_ratio": threshold_ratio,
            },
            output={
                "pe_expanded": False,
                "warning_late_stage": False,
                "expansion_ratio": None,
                "ticker": ticker.upper(),
                "ttm_eps": None,
                "entry_price": entry_price,
                "current_price": current_price,
                "baseline_pe": None,
                "current_pe": None,
                "eps_source": eps_source,
                "reason": f"edgar_eps fetch failed: {exc}",
                "v1_preliminary_flag": True,
            },
        )

    ttm_eps = float(getattr(eps_result, "ttm_eps", eps_result.get("ttm_eps")
                            if isinstance(eps_result, dict) else 0.0))

    if ttm_eps <= 0:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "ticker": ticker.upper(),
                "entry_price": entry_price,
                "current_price": current_price,
                "threshold_ratio": threshold_ratio,
            },
            output={
                "pe_expanded": False,
                "warning_late_stage": False,
                "expansion_ratio": None,
                "ticker": ticker.upper(),
                "ttm_eps": ttm_eps,
                "entry_price": entry_price,
                "current_price": current_price,
                "baseline_pe": None,
                "current_pe": None,
                "eps_source": eps_source,
                "reason": "non-positive TTM EPS (loss-making company); P/E undefined",
                "v1_preliminary_flag": True,
            },
        )

    # Current price fetch if not supplied
    if current_price is None:
        try:
            if _ohlcv_fetcher is None:
                from .data import fetch_ohlcv as _ohlcv_fetcher  # type: ignore[no-redef]
            r = _ohlcv_fetcher(ticker, period="5d", interval="1d")
            current_price = float(r.df["Close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            return TraceEntry(
                tool=TOOL,
                inputs={
                    "ticker": ticker.upper(),
                    "entry_price": entry_price,
                    "current_price": None,
                    "threshold_ratio": threshold_ratio,
                },
                output={
                    "pe_expanded": False,
                    "warning_late_stage": False,
                    "expansion_ratio": None,
                    "ticker": ticker.upper(),
                    "ttm_eps": ttm_eps,
                    "entry_price": entry_price,
                    "current_price": None,
                    "baseline_pe": entry_price / ttm_eps,
                    "current_pe": None,
                    "eps_source": eps_source,
                    "reason": f"current price fetch failed: {exc}",
                    "v1_preliminary_flag": True,
                },
            )

    baseline_pe = entry_price / ttm_eps
    current_pe = current_price / ttm_eps

    # Delegate to pure compute for the ratio + threshold check.
    base_entry = compute(
        baseline_pe=baseline_pe,
        current_pe=current_pe,
        threshold_ratio=threshold_ratio,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={
            "ticker": ticker.upper(),
            "entry_price": entry_price,
            "current_price": current_price,
            "threshold_ratio": threshold_ratio,
        },
        output={
            **base_entry.output,
            "ticker": ticker.upper(),
            "ttm_eps": ttm_eps,
            "entry_price": entry_price,
            "current_price": current_price,
            "baseline_pe": baseline_pe,
            "current_pe": current_pe,
            "eps_source": eps_source,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.pe_expansion_check",
        description="P/E expansion late-stage warning.",
    )
    p.add_argument("--baseline", type=float, dest="baseline_pe")
    p.add_argument("--current", type=float, dest="current_pe")
    p.add_argument("--ticker", type=str)
    p.add_argument("--entry-price", type=float, dest="entry_price")
    p.add_argument("--current-price", type=float, dest="current_price")
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_EXPANSION_THRESHOLD,
        dest="threshold_ratio",
    )
    args = p.parse_args()
    if args.ticker:
        if args.entry_price is None:
            p.error("--ticker requires --entry-price")
        emit(
            compute_from_ticker(
                ticker=args.ticker,
                entry_price=args.entry_price,
                current_price=args.current_price,
                threshold_ratio=args.threshold_ratio,
            )
        )
        return
    if args.baseline_pe is None or args.current_pe is None:
        p.error("provide --baseline and --current, OR --ticker --entry-price")
    emit(
        compute(
            baseline_pe=args.baseline_pe,
            current_pe=args.current_pe,
            threshold_ratio=args.threshold_ratio,
        )
    )


if __name__ == "__main__":
    main()
