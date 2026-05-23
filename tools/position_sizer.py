"""Position sizer — ATR-derived risk budget capped by concentration.

Per ``swing-position-sizing.md`` integrated formula:

    risk_budget_$  = account × risk_budget_pct[setup_grade] × regime_multiplier
    stop_distance  = stop_sizer(entry, atr, adr_pct).stop_distance
    shares_by_risk = risk_budget_$ / stop_distance
    shares_by_conc = (account × concentration_cap_pct) / entry
    shares         = floor(min(shares_by_risk, shares_by_conc))
    binding        = whichever capped

Per Requirement 2 (Liar Circuits): the agent never computes this in prose.

CLI::

    uv run python -m tools.position_sizer \\
        --account 150000 --entry 192.74 --atr 4.57 \\
        --setup-grade A+ --regime stage_2_confirmed
"""
from __future__ import annotations

import argparse
import math

from .cli import emit
from .contract import TraceEntry
from .stop_sizer import compute as stop_compute

TOOL = "tools/position_sizer.py"

# Per swing-position-sizing.md "Per-setup-rating risk budget table".
RISK_BUDGET_PCT: dict[str, float] = {
    "A+": 0.020,
    "A": 0.015,
    "B": 0.010,
    "C": 0.005,
    # EP grade variants per swing-earnings-pivot.md
    "GoldenEP": 0.020,
    "SuperSwan": 0.020,
    "Swan": 0.015,
    "Duck": 0.010,
    "Chicken": 0.005,
    # Secondary-setup variants per swing-position-sizing.md
    "Pullback-A": 0.010,
    "Pullback-B": 0.0075,
    "RSI-Div": 0.0075,
    "Resistance-Break": 0.0075,
}

# Per swing-regime-playbook.md "Regime-scaled risk budget" table.
REGIME_MULTIPLIER: dict[str, float] = {
    "stage_2_confirmed": 1.0,
    "stage_2_weakening": 0.75,
    "stage_3_transitional": 0.5,
    "stage_4": 0.0,
}

DEFAULT_CONCENTRATION_CAP_PCT = 0.25


def compute(
    account: float,
    entry_price: float,
    atr: float,
    setup_grade: str,
    regime_class: str,
    adr_pct: float | None = None,
    atr_multiple: float = 2.0,
    concentration_cap_pct: float = DEFAULT_CONCENTRATION_CAP_PCT,
    cash_available: float | None = None,
) -> TraceEntry:
    """Compute shares, capital, stop, and the binding constraint.

    Args:
        account: total portfolio value ($).
        entry_price: planned entry ($).
        atr: 14-period ATR ($), typically from :mod:`tools.atr_compute`.
        setup_grade: key into :data:`RISK_BUDGET_PCT`.
        regime_class: key into :data:`REGIME_MULTIPLIER`.
        adr_pct: optional Kullamägi ADR input.
        atr_multiple: ATR multiplier for stop. Default 2.0.
        concentration_cap_pct: max single-position capital fraction.
            Default 0.25 per swing-position-sizing (relaxed from v1 0.05
            because risk-based math now governs).
        cash_available: optional sanity check — final capital cannot
            exceed this.

    Returns:
        TraceEntry with shares / capital / stop_price / effective_risk_$ /
        binding_constraint / regime_multiplier.

    Raises:
        ValueError: on bad inputs, unknown grade, or unknown regime.
    """
    if account <= 0:
        raise ValueError(f"account must be positive; got {account}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive; got {entry_price}")
    if atr <= 0:
        raise ValueError(f"atr must be positive; got {atr}")
    if setup_grade not in RISK_BUDGET_PCT:
        raise ValueError(
            f"unknown setup_grade {setup_grade!r}; known: "
            f"{sorted(RISK_BUDGET_PCT)}"
        )
    if regime_class not in REGIME_MULTIPLIER:
        raise ValueError(
            f"unknown regime_class {regime_class!r}; known: "
            f"{sorted(REGIME_MULTIPLIER)}"
        )

    base_risk_pct = RISK_BUDGET_PCT[setup_grade]
    regime_mult = REGIME_MULTIPLIER[regime_class]
    effective_risk_pct = base_risk_pct * regime_mult

    # Stage 4 = no new positions.
    if regime_mult == 0.0:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "account": account,
                "entry_price": entry_price,
                "atr": atr,
                "setup_grade": setup_grade,
                "regime_class": regime_class,
            },
            output={
                "shares": 0,
                "capital": 0.0,
                "stop_price": None,
                "effective_risk_usd": 0.0,
                "effective_risk_pct": 0.0,
                "binding_constraint": "regime_stage_4_no_new_positions",
                "regime_multiplier": 0.0,
            },
        )

    risk_budget_usd = account * effective_risk_pct
    stop_entry = stop_compute(
        entry_price=entry_price,
        atr=atr,
        adr_pct=adr_pct,
        atr_multiple=atr_multiple,
    )
    stop_distance = stop_entry.output["stop_distance"]

    shares_by_risk = risk_budget_usd / stop_distance
    shares_by_conc = (account * concentration_cap_pct) / entry_price

    binding_source: str
    if shares_by_risk <= shares_by_conc:
        shares = math.floor(shares_by_risk)
        binding_source = stop_entry.output["binding_constraint"]
    else:
        shares = math.floor(shares_by_conc)
        binding_source = "concentration_cap"

    # Cash sanity check overrides if too tight.
    if cash_available is not None:
        max_shares_by_cash = math.floor(cash_available / entry_price)
        if max_shares_by_cash < shares:
            shares = max_shares_by_cash
            binding_source = "cash_available"

    if shares <= 0:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "account": account,
                "entry_price": entry_price,
                "atr": atr,
                "setup_grade": setup_grade,
                "regime_class": regime_class,
                "adr_pct": adr_pct,
                "atr_multiple": atr_multiple,
                "concentration_cap_pct": concentration_cap_pct,
                "cash_available": cash_available,
            },
            output={
                "shares": 0,
                "capital": 0.0,
                "stop_price": None,
                "effective_risk_usd": 0.0,
                "effective_risk_pct": 0.0,
                "binding_constraint": "insufficient_size_to_round_to_one_share",
                "regime_multiplier": regime_mult,
            },
        )

    capital = shares * entry_price
    effective_risk_usd = shares * stop_distance
    stop_price = stop_entry.output["stop_price"]

    return TraceEntry(
        tool=TOOL,
        inputs={
            "account": account,
            "entry_price": entry_price,
            "atr": atr,
            "setup_grade": setup_grade,
            "regime_class": regime_class,
            "adr_pct": adr_pct,
            "atr_multiple": atr_multiple,
            "concentration_cap_pct": concentration_cap_pct,
            "cash_available": cash_available,
        },
        output={
            "shares": shares,
            "capital": capital,
            "capital_pct": capital / account,
            "stop_price": stop_price,
            "stop_distance": stop_distance,
            "stop_distance_pct": stop_distance / entry_price,
            "effective_risk_usd": effective_risk_usd,
            "effective_risk_pct": effective_risk_usd / account,
            "binding_constraint": binding_source,
            "regime_multiplier": regime_mult,
            "base_risk_budget_pct": base_risk_pct,
            "stop_sizer_output": stop_entry.output,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.position_sizer",
        description="Position size via ATR-derived stop + risk budget + concentration cap.",
    )
    p.add_argument("--account", type=float, required=True)
    p.add_argument("--entry", type=float, required=True, dest="entry_price")
    p.add_argument("--atr", type=float, required=True)
    p.add_argument(
        "--setup-grade",
        required=True,
        choices=sorted(RISK_BUDGET_PCT),
    )
    p.add_argument(
        "--regime",
        required=True,
        choices=sorted(REGIME_MULTIPLIER),
        dest="regime_class",
    )
    p.add_argument("--adr-pct", type=float, default=None)
    p.add_argument("--atr-multiple", type=float, default=2.0)
    p.add_argument("--concentration-cap-pct", type=float, default=DEFAULT_CONCENTRATION_CAP_PCT)
    p.add_argument("--cash-available", type=float, default=None)
    args = p.parse_args()
    emit(
        compute(
            account=args.account,
            entry_price=args.entry_price,
            atr=args.atr,
            setup_grade=args.setup_grade,
            regime_class=args.regime_class,
            adr_pct=args.adr_pct,
            atr_multiple=args.atr_multiple,
            concentration_cap_pct=args.concentration_cap_pct,
            cash_available=args.cash_available,
        )
    )


if __name__ == "__main__":
    main()
