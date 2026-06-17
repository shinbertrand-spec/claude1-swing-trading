"""Event-driven opportunistic-insider-buying strategy KIND.

A modest, slow, multi-week diversifier — NOT a high-octane signal. The thesis:
when an insider with a *track record* (Phase 3) makes a meaningful open-market
purchase (Phase 4 conviction), the stock tends to drift up over the following
months (post-event drift; Cohen-Malloy-Pomorski 2012). We hold a long fixed
~6 months and let the drift play out.

Mechanics:

* **Event-driven**, not rebalance-driven: a signal fires on the first bar
  strictly AFTER an insider buying event's acceptance date (no event-bar peek).
* **Momentum-class entry** (per the epic): enter at the next bar's open; the
  net-of-cost simulator treats it as a marketable buy. Fill certainty matters
  more than entry precision on a 6-month hold.
* **6-month fixed hold** (``max_hold_days`` ≈ 126), with an ATR stop for
  catastrophe protection (per the paper-auto ATR-stop carve-out, not the 8%
  rule). No target by default — the hold IS the exit.
* **Conviction gate**: only events at or above ``min_conviction`` (default
  medium) enter; the events file already carries the Phase 4 verdict.
* **Overlap suppression**: while a position from a prior event is still within
  its hold window, later same-ticker events are skipped (we're already in).

Events come from a precomputed file (``params["events_path"]``) built by
:func:`tools.fundamentals.insider_events.build_events`. ``precompute`` only
loads + filters it — all the EDGAR/classify work happened offline.

Ships behind the deployment gate: this KIND is a backtest CANDIDATE and any
``deployable_setups.yml`` row for it carries ``hold: true`` until Phase 6's
net-of-cost gate clears it. Fails the gate → retire it. Do NOT tune to pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal
from ...fundamentals.insider_events import InsiderEvent, load_events

KIND = "event_insider_buying"

# conviction → backtest grade (informational; the gate doesn't read it)
_GRADE = {"high": "A", "medium": "B", "low": "C"}


class EventState(NamedTuple):
    events_by_ticker: dict[str, list[InsiderEvent]]


def precompute(universe_dfs: dict, params: dict) -> EventState:
    """Load the precomputed insider-events file, filtered to this universe."""
    events_path = params.get("events_path")
    if not events_path:
        raise ValueError(
            "event_insider_buying needs params['events_path'] pointing at a "
            "built insider-events YAML (see tools.fundamentals.insider_events)"
        )
    benchmark = params.get("benchmark")
    universe = {t for t in universe_dfs if t != benchmark}
    min_conviction = str(params.get("min_conviction", "medium"))
    by_ticker = load_events(Path(events_path), min_conviction=min_conviction, universe=universe)
    return EventState(events_by_ticker=by_ticker)


def replay(df: pd.DataFrame, ticker: str, params: dict, state: EventState) -> list[TradeSignal]:
    """Emit a signal per qualifying event for ``ticker`` (next-bar-open entry)."""
    events = state.events_by_ticker.get(ticker.upper())
    if not events:
        return []
    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))
    max_hold_days = int(params.get("max_hold_days", 126))
    target_r_multiple = params.get("target_r_multiple")
    if target_r_multiple is not None:
        target_r_multiple = float(target_r_multiple)

    df_dates = [pd.Timestamp(ix).date() for ix in df.index]
    n = len(df_dates)
    signals: list[TradeSignal] = []
    last_exit_idx = -1   # overlap suppression: index through which we're "in"

    for ev in events:
        try:
            ev_date = pd.Timestamp(ev.event_date).date()
        except (ValueError, TypeError):
            continue
        # entry = first bar STRICTLY after the acceptance date (anti-look-ahead)
        entry_idx = _first_index_after(df_dates, ev_date)
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
        stop_distance = atr_stop_multiple * atr_value
        stop_price = entry_price - stop_distance
        if stop_price >= entry_price:
            continue
        target_price = (
            entry_price + target_r_multiple * stop_distance
            if target_r_multiple is not None else None
        )

        signals.append(TradeSignal(
            ticker=ticker,
            setup_type=KIND,
            setup_grade=_GRADE.get(ev.conviction_level, "B"),
            entry_date=ev_date,
            fill_date=df_dates[entry_idx],
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            max_hold_days=max_hold_days,
            atr_at_signal=atr_value,
            notes={
                "event_date": ev.event_date,
                "conviction": ev.conviction_level,
                "composite_score": ev.composite_score,
                "n_insiders": ev.n_insiders,
                "best_tier": ev.best_tier,
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
