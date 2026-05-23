"""Walk-forward windowing for IS / OOS validation.

Per ``walk-forward-analysis`` (vault concept page): the canonical defence
against curve-fitting + survivorship + data-snooping is to tune on an
in-sample (IS) window and validate on an out-of-sample (OOS) window that
**the model never saw during tuning**. Roll the windows forward and
repeat.

Phase 5.a baseline: a single IS/OOS split per ticker is supported plus a
multi-window rolling helper. Actual hyperparameter tuning is deferred —
all Phase 2 setup-detector thresholds are baked-in for now; the harness
just reports IS vs OOS metrics so future tuning has the right structure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .setup_replay import TradeSignal
from .simulator import TradeOutcome


@dataclass
class WindowSpec:
    in_sample_start: date
    in_sample_end: date          # exclusive — first OOS day
    out_of_sample_end: date


@dataclass
class WindowResult:
    spec: WindowSpec
    in_sample_signals: list[TradeSignal]
    in_sample_outcomes: list[TradeOutcome]
    out_of_sample_signals: list[TradeSignal]
    out_of_sample_outcomes: list[TradeOutcome]


def single_split(
    start: date,
    end: date,
    is_fraction: float = 0.70,
) -> WindowSpec:
    """Single IS/OOS split — default 70/30."""
    span_days = (end - start).days
    if span_days <= 0:
        raise ValueError(f"end {end} must be after start {start}")
    split_offset = int(span_days * is_fraction)
    is_end = start + timedelta(days=split_offset)
    return WindowSpec(
        in_sample_start=start,
        in_sample_end=is_end,
        out_of_sample_end=end,
    )


def rolling_splits(
    start: date,
    end: date,
    is_years: int = 3,
    oos_years: int = 1,
    step_years: int = 1,
) -> list[WindowSpec]:
    """Rolling walk-forward windows.

    Each window is ``is_years`` in-sample then ``oos_years`` out-of-sample.
    Roll forward by ``step_years`` until the OOS end would exceed ``end``.
    """
    if is_years <= 0 or oos_years <= 0 or step_years <= 0:
        raise ValueError("is_years, oos_years, step_years must all be positive")
    specs: list[WindowSpec] = []
    cur = start
    while True:
        is_end = date(cur.year + is_years, cur.month, cur.day)
        oos_end = date(is_end.year + oos_years, is_end.month, is_end.day)
        if oos_end > end:
            break
        specs.append(
            WindowSpec(in_sample_start=cur, in_sample_end=is_end, out_of_sample_end=oos_end)
        )
        cur = date(cur.year + step_years, cur.month, cur.day)
    return specs


def split_trades_by_window(
    signals: list[TradeSignal],
    outcomes: list[TradeOutcome],
    spec: WindowSpec,
) -> tuple[list[TradeSignal], list[TradeOutcome], list[TradeSignal], list[TradeOutcome]]:
    """Partition signals + outcomes into IS / OOS based on ``fill_date``.

    A trade is IS iff its ``fill_date`` falls in ``[is_start, is_end)``.
    Trades after ``is_end`` but before ``oos_end`` are OOS. Trades
    outside both windows are dropped.
    """
    assert len(signals) == len(outcomes)
    is_sigs: list[TradeSignal] = []
    is_outs: list[TradeOutcome] = []
    oos_sigs: list[TradeSignal] = []
    oos_outs: list[TradeOutcome] = []
    for sig, outc in zip(signals, outcomes):
        d = sig.fill_date
        if spec.in_sample_start <= d < spec.in_sample_end:
            is_sigs.append(sig)
            is_outs.append(outc)
        elif spec.in_sample_end <= d < spec.out_of_sample_end:
            oos_sigs.append(sig)
            oos_outs.append(outc)
    return is_sigs, is_outs, oos_sigs, oos_outs


def trim_ohlcv(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Slice ``df`` to bars whose date is in ``[start, end)``."""
    dates = pd.DatetimeIndex(df.index).date
    mask = (dates >= start) & (dates < end)
    return df.loc[mask]
