"""Trade simulator: TradeSignal + post-entry OHLCV → TradeOutcome.

Per-bar evaluation. Phase 5.b adds **trailing-stop support** via
:class:`TrailConfig` from :mod:`tools.backtest.trailing_stop`. Phase 5.c
adds **sell-aware exits** via :class:`SellPolicy` from
:mod:`tools.backtest.sell_aware` — per-bar sell-discipline composer that
exits when sell_decision returns anything other than "hold".

Bar evaluation order (conservative — stop wins ties):

1. Update trailing stop based on bars seen so far (no-op for ``fixed``).
2. Check gap-down: if Open < current_stop, exit at Open (gap through stop).
3. Check stop trigger per the trail policy:
   * ``fixed`` / ``ratchet`` — intrabar low ≤ stop fires.
   * ``ma_trail`` — close below stop fires (Kullamägi rule).
4. Check High ≥ target (if target set): exit at target.
5. If ``sell_policy.enabled`` and past grace period: run sell-decision
   composer; on non-hold action exit at the bar's close.
6. If max_hold_days reached: exit at Close.
7. Else continue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .sell_aware import SellPolicy, evaluate_bar, exit_action_to_reason
from .setup_replay import TradeSignal
from .trailing_stop import TrailConfig, make_policy, trail_exit_signal


@dataclass
class TradeOutcome:
    signal: TradeSignal
    exit_date: date
    exit_price: float
    exit_reason: str        # "stop_hit" | "gap_through_stop" | "trail_stop_hit" | "target_hit" | "max_hold" | "end_of_data"
    bars_held: int
    pnl_pct: float          # (exit - entry) / entry
    r_multiple: float       # (exit - entry) / (entry - stop_at_entry)
    final_stop: float       # the trailing stop level at exit (informational)


def simulate_trade(
    signal: TradeSignal,
    df: pd.DataFrame,
    trail_config: TrailConfig | None = None,
    sell_policy: SellPolicy | None = None,
) -> TradeOutcome:
    """Simulate ``signal`` against post-signal OHLCV.

    Args:
        signal: trade signal from a replay module (e.g.
            :func:`setup_replay.replay_sepa_vcp`).
        df: full OHLCV indexed by date. Must include bars from
            ``signal.fill_date`` onward.
        trail_config: stop-trail policy. Defaults to ``TrailConfig(mode="fixed")``
            which reproduces Phase 5.a behavior.
        sell_policy: optional per-bar sell-discipline composer (Phase 5.c).
            Defaults to disabled (``SellPolicy(enabled=False)``); the
            trade exits only via stop/target/max-hold. When enabled, a
            non-hold sell_decision action on any bar (past grace period)
            exits at that bar's close with ``exit_reason="sell_decision_<action>"``.

    Returns:
        :class:`TradeOutcome`. If data runs out before any exit triggers,
        ``exit_reason = "end_of_data"`` at the last available close.
    """
    if trail_config is None:
        trail_config = TrailConfig(mode="fixed")
    if sell_policy is None:
        sell_policy = SellPolicy(enabled=False)
    update_stop = make_policy(trail_config)

    required = {"Open", "High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")

    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    if signal.fill_date not in idx_dates:
        raise ValueError(
            f"fill_date {signal.fill_date} not in DataFrame index "
            f"(span {idx_dates[0]}..{idx_dates[-1]})"
        )
    fill_pos = idx_dates.index(signal.fill_date)
    entry = signal.entry_price
    initial_stop = signal.stop_price
    target = signal.target_price
    risk_per_share = entry - initial_stop
    if risk_per_share <= 0:
        raise ValueError(
            f"stop must be below entry; got entry={entry}, stop={initial_stop}"
        )

    open_ = df["Open"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    current_stop = initial_stop
    last_pos = len(df) - 1
    for offset in range(0, signal.max_hold_days + 1):
        i = fill_pos + offset
        if i > last_pos:
            exit_pos = last_pos
            return _outcome(
                signal,
                exit_date=idx_dates[exit_pos],
                exit_price=float(close.iloc[exit_pos]),
                reason="end_of_data",
                bars_held=exit_pos - fill_pos,
                entry=entry,
                stop_at_entry=initial_stop,
                final_stop=current_stop,
            )

        # 1. Update trail (uses bars from fill onward up to and including this one).
        ohlcv_so_far = df.iloc[fill_pos : i + 1]
        new_stop = update_stop(
            current_stop=current_stop,
            entry_price=entry,
            ohlcv_so_far=ohlcv_so_far,
        )
        if new_stop < current_stop:
            # Should be unreachable — every policy enforces never-widen.
            raise RuntimeError(
                f"trail policy widened stop ({current_stop} → {new_stop}); "
                "policies must enforce never-widen"
            )
        current_stop = new_stop

        bar_open = float(open_.iloc[i])
        bar_high = float(high.iloc[i])
        bar_low = float(low.iloc[i])
        bar_close = float(close.iloc[i])

        # 2. Gap-through-stop on open (universal — applies to all trail modes).
        if bar_open < current_stop:
            return _outcome(
                signal,
                exit_date=idx_dates[i],
                exit_price=bar_open,
                reason="gap_through_stop",
                bars_held=offset,
                entry=entry,
                stop_at_entry=initial_stop,
                final_stop=current_stop,
            )

        # 3. Trail-based stop check (intrabar low vs stop, OR close-below for MA).
        if trail_exit_signal(
            trail_config,
            bar_close=bar_close,
            bar_low=bar_low,
            current_stop=current_stop,
        ):
            # For fixed/ratchet: stop hit intrabar; fill at stop.
            # For ma_trail: close below trail; fill at close.
            exit_px = current_stop if trail_config.mode != "ma_trail" else bar_close
            reason = "trail_stop_hit" if trail_config.mode != "fixed" else "stop_hit"
            return _outcome(
                signal,
                exit_date=idx_dates[i],
                exit_price=exit_px,
                reason=reason,
                bars_held=offset,
                entry=entry,
                stop_at_entry=initial_stop,
                final_stop=current_stop,
            )

        # 4. Target hit (assume fill at target).
        if target is not None and bar_high >= target:
            return _outcome(
                signal,
                exit_date=idx_dates[i],
                exit_price=target,
                reason="target_hit",
                bars_held=offset,
                entry=entry,
                stop_at_entry=initial_stop,
                final_stop=current_stop,
            )

        # 5. Sell-discipline composer (Phase 5.c). Only past grace
        # period — early in the trade climax-top / sell-into-strength
        # patterns trivially fire on entry-day price action.
        if sell_policy.enabled and offset > sell_policy.grace_period_bars:
            event = evaluate_bar(
                df_through_today=df.iloc[: i + 1],
                starter_entry=entry,
                fill_date=signal.fill_date,
                setup_grade=signal.setup_grade,
                policy=sell_policy,
            )
            if event.action != "hold":
                return _outcome(
                    signal,
                    exit_date=idx_dates[i],
                    exit_price=bar_close,
                    reason=exit_action_to_reason(event.action),
                    bars_held=offset,
                    entry=entry,
                    stop_at_entry=initial_stop,
                    final_stop=current_stop,
                )

        # 6. Max-hold reached → exit at this bar's close.
        if offset == signal.max_hold_days:
            return _outcome(
                signal,
                exit_date=idx_dates[i],
                exit_price=bar_close,
                reason="max_hold",
                bars_held=offset,
                entry=entry,
                stop_at_entry=initial_stop,
                final_stop=current_stop,
            )
    raise RuntimeError("simulator: fell out of loop unexpectedly")


def _outcome(
    signal: TradeSignal,
    exit_date: date,
    exit_price: float,
    reason: str,
    bars_held: int,
    entry: float,
    stop_at_entry: float,
    final_stop: float,
) -> TradeOutcome:
    pnl_pct = (exit_price - entry) / entry
    r_multiple = (exit_price - entry) / (entry - stop_at_entry)
    return TradeOutcome(
        signal=signal,
        exit_date=exit_date,
        exit_price=exit_price,
        exit_reason=reason,
        bars_held=bars_held,
        pnl_pct=pnl_pct,
        r_multiple=r_multiple,
        final_stop=final_stop,
    )


def simulate_signals(
    signals: list[TradeSignal],
    df: pd.DataFrame,
    trail_config: TrailConfig | None = None,
    sell_policy: SellPolicy | None = None,
) -> list[TradeOutcome]:
    """Convenience: run :func:`simulate_trade` over a signal list."""
    return [
        simulate_trade(s, df, trail_config=trail_config, sell_policy=sell_policy)
        for s in signals
    ]
