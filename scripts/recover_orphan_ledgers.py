"""Recover the 3 paper-auto orphan orders from 2026-05-26 manual rebalance.

Backfills paper-auto ledgers + positions.json entries for orders placed at
Tiger but lost on ledger-schema validation (the `_liquid_us` setup_types
weren't in the schema enum; that's now fixed).

These three orders are LIVE at Tiger with no framework state behind them
until this script runs:
    MXL  #43372227454453760  clenow_momentum_liquid_us  503 sh @ $99.36 stop $65.77
    GO   #43372227691168768  residual_momentum_liquid_us 6225 sh @ $8.03 stop $6.41
    INTU #43372227924478976  xs_short_term_reversal_liquid_us 155 sh @ $320.58 stop $277.76

VRT was the 4th candidate — it landed cleanly because xs_short_term_reversal
was already in the schema enum.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.auto_paper import state  # noqa: E402


ORPHANS = [
    {
        "ticker": "MXL",
        "setup_type": "clenow_momentum_liquid_us",
        "broker_order_id": 43372227454453760,
        "pivot_price": 99.36,
        "limit_price": 99.36,
        "stop_price": 65.77,
        "shares": 503,
        "sector_etf": "XLK",
    },
    {
        "ticker": "GO",
        "setup_type": "residual_momentum_liquid_us",
        "broker_order_id": 43372227691168768,
        "pivot_price": 8.03,
        "limit_price": 8.03,
        "stop_price": 6.41,
        "shares": 6225,
        "sector_etf": "XLK",  # heuristic — actually consumer staples
    },
    {
        "ticker": "INTU",
        "setup_type": "xs_short_term_reversal_liquid_us",
        "broker_order_id": 43372227924478976,
        "pivot_price": 320.58,
        "limit_price": 320.58,
        "stop_price": 277.76,
        "shares": 155,
        "sector_etf": "XLK",
    },
]


def main() -> None:
    print("# Orphan ledger recovery")
    print()
    for o in ORPHANS:
        ticker = o["ticker"]
        if state.ledger_exists(ticker):
            print(f"  {ticker}: SKIP — ledger already exists at {state.ledger_path(ticker)}")
            continue
        try:
            path = state.write_submitted_ledger(
                ticker=ticker,
                setup_type=o["setup_type"],
                setup_grade="B",
                pivot_price=o["pivot_price"],
                limit_price=o["limit_price"],
                stop_price=o["stop_price"],
                shares=o["shares"],
                broker_order_id=o["broker_order_id"],
                broker="tiger_paper",
                sector_etf=o["sector_etf"],
                reasoning_trace=[],
            )
        except Exception as exc:
            print(f"  {ticker}: ledger write FAILED — {exc!r}")
            continue
        print(f"  {ticker}: ledger written → {path}")

        try:
            state.append_to_positions_json({
                "ticker": ticker.upper(),
                "ledger_path": path.replace("\\", "/"),
                "entry_date": state._today(),
                "entry_price": o["limit_price"],
                "shares": o["shares"],
                "stop": o["stop_price"],
                "target_1": None,
                "sector": o["sector_etf"],
                "broker_order_id": o["broker_order_id"],
                "broker": "tiger_paper",
                "stage": "submitted",
                "setup_type": o["setup_type"],
                "setup_grade": "B",
            })
            print(f"  {ticker}: appended to journal/paper-auto/positions.json")
        except Exception as exc:
            print(f"  {ticker}: positions.json append FAILED — {exc!r}")

    print()
    print("Done. Verify with: cat journal/paper-auto/positions.json")


if __name__ == "__main__":
    main()
