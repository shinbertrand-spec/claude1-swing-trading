"""Event-driven earnings announcement-return (EAR) drift strategy KIND.

Long-only post-earnings drift on the *reaction*, not the surprise: buy liquid names
whose earnings-reaction session put them in the top tail of the trailing
cross-sectional announcement-window excess return, optionally conditioned on
surprise history, enter next open, hold 10-21 trading days. Spec authority:
``plans/2026-07-25-event-earnings-drift-candidate-spec.md`` (+ the frozen-filter
addendum committed 2026-08-06 BEFORE any grid run).

Execution-model note (the insider-autopsy inversion, spec §2): the event-day jump
is the FILTER, not the thing chased — the literature measures drift from
post-reaction prices onward, which is exactly where next-bar-open entry sits.

Mechanics (mirrors ``event_insider_buying`` — same architecture, no new machinery):

* Events come from a precomputed file (``params["events_path"]``) built by
  :mod:`tools.fundamentals.earnings_events` under the FROZEN strict-2.02 filter,
  with §3b session alignment baked into each event's ``reaction_date``
  (BMO day T → reaction T; AMC day T → reaction T+1; unknown → AMC treatment).
* A signal fires on the first bar STRICTLY AFTER ``reaction_date`` (next open) —
  never the reaction session itself; no window the signal date can't know.
* Qualification (§3c): ``ear`` > 0 AND ``ear_rank_pct`` >= 100 - ``ear_top_pct``,
  plus (if ``history_condition``) ``prior_pos_ear_count`` >= 2 of last 4.
* Fixed hold ``max_hold_days`` (the hold IS the exit) + 3x-ATR catastrophe stop
  (paper-auto ATR carve-out, NOT the 8% rule). No profit target.
* Overlap-suppressed per ticker (one open event position per name).

STATUS: candidate. Ships behind the deployment gate + the DSR>=0.95 seventh gate
against the post-registration trials count. Fails → retire; do NOT tune to pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal
from ...fundamentals.earnings_events import EarningsEvent, load_events

KIND = "event_earnings_drift"


class EventState(NamedTuple):
    events_by_ticker: dict[str, list[EarningsEvent]]


def precompute(universe_dfs: dict, params: dict) -> EventState:
    """Load the precomputed earnings-events file, filtered to this universe."""
    events_path = params.get("events_path")
    if not events_path:
        raise ValueError(
            "event_earnings_drift needs params['events_path'] pointing at a built "
            "earnings-events YAML (see tools.fundamentals.earnings_events)"
        )
    benchmark = params.get("benchmark")
    universe = {t for t in universe_dfs if t != benchmark}
    by_ticker = load_events(Path(events_path), universe=universe)
    return EventState(events_by_ticker=by_ticker)


def replay(df: pd.DataFrame, ticker: str, params: dict, state: EventState) -> list[TradeSignal]:
    """Emit a signal per qualifying event for ``ticker`` (next-bar-open entry)."""
    events = state.events_by_ticker.get(ticker.upper())
    if not events:
        return []
    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    ear_top_pct = float(params.get("ear_top_pct", 10))
    history_condition = bool(params.get("history_condition", False))
    max_hold_days = int(params.get("max_hold_days", 21))
    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))
    rank_floor = 100.0 - ear_top_pct

    df_dates = [pd.Timestamp(ix).date() for ix in df.index]
    n = len(df_dates)
    signals: list[TradeSignal] = []
    last_exit_idx = -1   # overlap suppression: index through which we're "in"

    for ev in events:
        # §3c qualification — all fields must be present and pass
        if ev.ear is None or ev.ear <= 0:
            continue
        if ev.ear_rank_pct is None or ev.ear_rank_pct < rank_floor:
            continue
        if history_condition and (ev.prior_pos_ear_count is None or ev.prior_pos_ear_count < 2):
            continue
        try:
            rd = pd.Timestamp(ev.reaction_date).date()
        except (ValueError, TypeError):
            continue
        # entry = first bar STRICTLY after the reaction session (anti-look-ahead)
        entry_idx = _first_index_after(df_dates, rd)
        if entry_idx is None or entry_idx >= n:
            continue
        if entry_idx <= last_exit_idx:
            continue  # already holding from a prior event
        entry_price = float(df.iloc[entry_idx]["Open"])
        if entry_price <= 0 or pd.isna(entry_price):
            continue
        # ATR from data up to (and including) the bar before entry — no peek.
        atr_value = _compute_atr(df.iloc[:entry_idx], period=atr_period)
        if atr_value is None or atr_value <= 0:
            continue
        stop_price = entry_price - atr_stop_multiple * atr_value
        if stop_price >= entry_price:
            continue
        signals.append(TradeSignal(
            ticker=ticker,
            setup_type=KIND,
            setup_grade="A" if ev.ear_rank_pct >= 95.0 else "B",
            entry_date=rd,
            fill_date=df_dates[entry_idx],
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=None,          # the hold IS the exit
            max_hold_days=max_hold_days,
            atr_at_signal=atr_value,
            notes={
                "announce_date": ev.announce_date,
                "timing": ev.timing,
                "ear": ev.ear,
                "ear_rank_pct": ev.ear_rank_pct,
                "prior_pos_ear_count": ev.prior_pos_ear_count,
            },
        ))
        last_exit_idx = entry_idx + max_hold_days

    return signals


def _first_index_after(dates: list, d) -> int | None:
    """Index of the first date strictly greater than ``d`` (dates ascending)."""
    import bisect
    i = bisect.bisect_right(dates, d)
    return i if i < len(dates) else None


def _compute_atr(df: pd.DataFrame, period: int) -> float | None:
    if len(df) < period + 1:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr[0] = high[0] - low[0]
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])
