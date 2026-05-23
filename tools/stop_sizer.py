"""Stop-distance sizer — min(ATR×mult, ADR-as-%, 8% Minervini cap).

Per ``swing-position-sizing.md`` + ``swing-earnings-pivot.md``: the effective
stop is the **tightest** of three caps. The binding constraint is itself
information — if the 8% Minervini cap binds for a C-grade setup, that's a
"skip the trade" signal per the operational note.

Pure arithmetic; no data fetch. ATR is supplied by caller (or pulled via
:mod:`tools.atr_compute`).

CLI::

    uv run python -m tools.stop_sizer --entry 192.74 --atr 4.57 --adr-pct 2.1
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/stop_sizer.py"

MINERVINI_HARD_CAP_PCT = 0.08
DEFAULT_ATR_MULTIPLE = 2.0


def compute(
    entry_price: float,
    atr: float,
    adr_pct: float | None = None,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    minervini_cap_pct: float = MINERVINI_HARD_CAP_PCT,
) -> TraceEntry:
    """Return stop distance + price + which cap bound.

    Args:
        entry_price: planned entry price ($).
        atr: 14-period ATR ($). Typically from :mod:`tools.atr_compute`.
        adr_pct: optional 20-day average daily range as percent of price
            (e.g. ``2.1`` for 2.1%). Kullamägi-school input. Pass ``None``
            to skip the ADR cap.
        atr_multiple: ATR multiplier for the stop-distance candidate.
            Default 2.0 per ``swing-position-sizing``.
        minervini_cap_pct: hard cap as fraction of entry. Default 0.08.

    Returns:
        TraceEntry with output keys: ``stop_distance``, ``stop_price``,
        ``stop_distance_pct``, ``binding_constraint``,
        ``atr_distance`` / ``adr_distance`` / ``minervini_distance``
        for audit.

    Raises:
        ValueError: if ``entry_price`` or ``atr`` non-positive.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive; got {entry_price}")
    if atr <= 0:
        raise ValueError(f"atr must be positive; got {atr}")

    atr_distance = atr * atr_multiple
    minervini_distance = entry_price * minervini_cap_pct
    adr_distance = (entry_price * (adr_pct / 100.0)) if adr_pct is not None else None

    candidates: dict[str, float] = {
        "atr_x_multiple": atr_distance,
        "minervini_8pct_cap": minervini_distance,
    }
    if adr_distance is not None:
        candidates["adr_pct"] = adr_distance

    binding = min(candidates, key=lambda k: candidates[k])
    effective_distance = candidates[binding]
    stop_price = entry_price - effective_distance

    skip_signal = binding == "minervini_8pct_cap" and atr_distance > minervini_distance
    return TraceEntry(
        tool=TOOL,
        inputs={
            "entry_price": entry_price,
            "atr": atr,
            "adr_pct": adr_pct,
            "atr_multiple": atr_multiple,
            "minervini_cap_pct": minervini_cap_pct,
        },
        output={
            "stop_distance": effective_distance,
            "stop_price": stop_price,
            "stop_distance_pct": effective_distance / entry_price,
            "binding_constraint": binding,
            "atr_distance": atr_distance,
            "adr_distance": adr_distance,
            "minervini_distance": minervini_distance,
            "skip_signal_atr_exceeds_cap": skip_signal,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.stop_sizer",
        description="Stop distance = min(ATR×mult, ADR×price, 8% Minervini cap).",
    )
    p.add_argument("--entry", type=float, required=True, dest="entry_price")
    p.add_argument("--atr", type=float, required=True)
    p.add_argument("--adr-pct", type=float, default=None)
    p.add_argument("--atr-multiple", type=float, default=DEFAULT_ATR_MULTIPLE)
    args = p.parse_args()
    emit(
        compute(
            entry_price=args.entry_price,
            atr=args.atr,
            adr_pct=args.adr_pct,
            atr_multiple=args.atr_multiple,
        )
    )


if __name__ == "__main__":
    main()
