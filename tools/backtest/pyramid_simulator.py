"""Multi-leg pyramided trade simulator (per ``swing-momentum-execution.md``).

Replays the Anchor-and-Pyramid workflow inside the backtest:

* **STARTER** fills on the signal's ``fill_date`` at 1/3 of intended size
  (the ``TradeSignal`` represents the STARTER leg).
* **ADD-ON #1** fires if a Momentum Burst (``tools.momentum_burst_detect``)
  triggers within ``addon_1_max_window_bars`` of the starter. Adds shares
  to bring the position to full intended size. Stop migrates to combined
  break-even on the merged STARTER + ADD-ON #1.
* **ADD-ON #2** fires on Day 7 milestone (``tools.day7_milestone_check``)
  + grade qualifier (Super Swan / Golden EP only) + regime gate
  (Stage 2 confirmed only). Adds 50% on top of full → 1.5× intended.
  Trail tightens (caller-configurable via ``trail_config_after_addon_2``).

Exit semantics match :mod:`tools.backtest.simulator` — gap-through stop,
intrabar stop hit (or close-below for ma_trail), target, max-hold. The
combined position exits together at one price.

The ``r_multiple`` in the returned :class:`PyramidedTradeOutcome` is the
size-weighted average of each leg's R-multiple, where R is defined by
the STARTER leg's risk (starter_entry - starter_stop).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..day7_milestone_check import compute_from_ohlcv as day7_compute
from ..momentum_burst_detect import compute_from_ohlcv as momentum_burst_compute
from .setup_replay import TradeSignal
from .simulator import TradeOutcome
from .trailing_stop import TrailConfig, make_policy, trail_exit_signal

# Per swing-momentum-execution Anchor-and-Pyramid sizing convention.
STARTER_SHARES_FRACTION = 1.0 / 3.0
ADDON_1_SHARES_FRACTION = 2.0 / 3.0           # brings position from 1/3 to full
ADDON_2_SHARES_FRACTION = 0.5                  # 50% on top of full → 1.5× intended

ADDON_1_DEFAULT_WINDOW_BARS = 10
ADDON_2_DAY7_OFFSET = 7
DAY7_QUALIFIED_GRADES = frozenset({"SuperSwan", "GoldenEP"})
STAGE_3_ALLOWED_REGIMES_FOR_ADDON_2 = frozenset({"stage_2_confirmed"})


@dataclass
class Leg:
    """One filled entry leg in a pyramided trade."""

    name: str                  # "STARTER" | "ADD-ON #1" | "ADD-ON #2"
    trigger: str               # "EPGap" / "VCPBreakout" / "MomentumBurst" / "Day7Milestone" / "manual"
    fill_date: date
    fill_price: float
    shares_fraction: float     # of intended full size
    initial_stop: float


@dataclass
class PyramidPolicy:
    """How the pyramid should evolve mid-trade."""

    enabled: bool = True
    addon_1_max_window_bars: int = ADDON_1_DEFAULT_WINDOW_BARS
    addon_2_qualified_grades: frozenset[str] = field(
        default_factory=lambda: DAY7_QUALIFIED_GRADES
    )
    addon_2_regime_classes: frozenset[str] = field(
        default_factory=lambda: STAGE_3_ALLOWED_REGIMES_FOR_ADDON_2
    )
    regime_class: str = "stage_2_confirmed"  # ambient regime — Phase 5.c baseline; per-bar regime is Phase 5.d
    # When ADD-ON #2 fires, optionally switch the trail policy (e.g. tighten to ma_trail 10d per swing-momentum-execution).
    trail_config_after_addon_2: TrailConfig | None = None


@dataclass
class PyramidedTradeOutcome(TradeOutcome):
    """Single combined-position outcome with multi-leg entry history."""

    legs: list[Leg] = field(default_factory=list)
    combined_breakeven: float = 0.0
    addon_1_filled: bool = False
    addon_2_filled: bool = False
    skipped_addon_reasons: list[str] = field(default_factory=list)


def _combined_breakeven(legs: list[Leg]) -> float:
    total_shares = sum(leg.shares_fraction for leg in legs)
    if total_shares == 0:
        return 0.0
    weighted = sum(leg.fill_price * leg.shares_fraction for leg in legs)
    return weighted / total_shares


def _combined_r_multiple(
    legs: list[Leg], exit_price: float, starter_entry: float, starter_stop: float
) -> float:
    """Size-weighted R per swing-momentum-execution conventions.

    R is defined by the STARTER leg's risk (starter_entry - starter_stop).
    Each leg's contribution is its shares_fraction × (exit_price - leg.fill_price).
    The total R-multiple = (combined per-share P&L × total_shares) / starter_risk.
    """
    starter_risk = starter_entry - starter_stop
    if starter_risk <= 0:
        return 0.0
    total_shares = sum(leg.shares_fraction for leg in legs)
    total_pnl = sum(
        leg.shares_fraction * (exit_price - leg.fill_price) for leg in legs
    )
    return total_pnl / (total_shares * starter_risk)


def simulate_trade_pyramided(
    signal: TradeSignal,
    df: pd.DataFrame,
    trail_config: TrailConfig | None = None,
    pyramid_policy: PyramidPolicy | None = None,
) -> PyramidedTradeOutcome:
    """Simulate ``signal`` with mid-trade addon detection.

    Args:
        signal: STARTER leg signal. Setup-grade + regime info is read
            from this object and from ``pyramid_policy``.
        df: full OHLCV indexed by date.
        trail_config: stop-trail policy for STARTER and ADD-ON #1 phases.
            Defaults to ``TrailConfig(mode="fixed")``.
        pyramid_policy: addon rules. Defaults to enabled with framework
            defaults.

    Returns:
        :class:`PyramidedTradeOutcome` describing the full lifecycle —
        legs filled, combined break-even, exit, R-multiple weighted across legs.
    """
    if trail_config is None:
        trail_config = TrailConfig(mode="fixed")
    if pyramid_policy is None:
        pyramid_policy = PyramidPolicy(enabled=True)

    required = {"Open", "High", "Low", "Close", "Volume"}
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

    starter_entry = signal.entry_price
    starter_stop = signal.stop_price
    target = signal.target_price
    if starter_entry - starter_stop <= 0:
        raise ValueError(
            f"stop must be below entry; got entry={starter_entry}, stop={starter_stop}"
        )
    # Capture the starter-day low for the Day-7 milestone check (uses
    # the bar BEFORE fill_pos as the EP/anchor bar — the signal day).
    if fill_pos < 1:
        raise ValueError("need at least one bar before fill_date for signal/anchor day")
    anchor_low = float(df["Low"].iloc[fill_pos - 1])

    legs: list[Leg] = [
        Leg(
            name="STARTER",
            trigger=signal.notes.get("starter_trigger", signal.setup_type),
            fill_date=signal.fill_date,
            fill_price=starter_entry,
            shares_fraction=STARTER_SHARES_FRACTION,
            initial_stop=starter_stop,
        )
    ]
    current_stop = starter_stop
    addon_1_filled = False
    addon_2_filled = False
    skipped_reasons: list[str] = []
    active_trail = trail_config

    open_ = df["Open"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    last_pos = len(df) - 1

    def _build_outcome(
        exit_date: date,
        exit_price: float,
        reason: str,
        bars_held: int,
    ) -> PyramidedTradeOutcome:
        breakeven = _combined_breakeven(legs)
        r_mult = _combined_r_multiple(legs, exit_price, starter_entry, starter_stop)
        # Use the STARTER's R definition for back-compat with the
        # TradeOutcome shape consumed by metrics.
        return PyramidedTradeOutcome(
            signal=signal,
            exit_date=exit_date,
            exit_price=exit_price,
            exit_reason=reason,
            bars_held=bars_held,
            pnl_pct=(exit_price - breakeven) / breakeven if breakeven > 0 else 0.0,
            r_multiple=r_mult,
            final_stop=current_stop,
            legs=list(legs),
            combined_breakeven=breakeven,
            addon_1_filled=addon_1_filled,
            addon_2_filled=addon_2_filled,
            skipped_addon_reasons=skipped_reasons,
        )

    for offset in range(0, signal.max_hold_days + 1):
        i = fill_pos + offset
        if i > last_pos:
            return _build_outcome(
                exit_date=idx_dates[last_pos],
                exit_price=float(close.iloc[last_pos]),
                reason="end_of_data",
                bars_held=last_pos - fill_pos,
            )

        # 1. Update trail using bars from fill onward.
        ohlcv_so_far = df.iloc[fill_pos : i + 1]
        update_stop = make_policy(active_trail)
        new_stop = update_stop(
            current_stop=current_stop,
            entry_price=starter_entry,
            ohlcv_so_far=ohlcv_so_far,
        )
        if new_stop > current_stop:
            current_stop = new_stop

        bar_open = float(open_.iloc[i])
        bar_high = float(high.iloc[i])
        bar_low = float(low.iloc[i])
        bar_close = float(close.iloc[i])

        # 2. Gap-through-stop.
        if bar_open < current_stop:
            return _build_outcome(
                exit_date=idx_dates[i],
                exit_price=bar_open,
                reason="gap_through_stop",
                bars_held=offset,
            )

        # 3. ADD-ON #1 — Momentum Burst within window.
        if (
            pyramid_policy.enabled
            and not addon_1_filled
            and 1 <= offset <= pyramid_policy.addon_1_max_window_bars
        ):
            try:
                mb = momentum_burst_compute(df.iloc[: i + 1])
                if mb.output["triggered"]:
                    addon_1_price = bar_close
                    if addon_1_price <= starter_entry * 1.05:
                        skipped_reasons.append(
                            f"addon_1_skipped_chase_check at offset {offset}: "
                            f"close {addon_1_price} not above starter+5%"
                        )
                    else:
                        legs.append(
                            Leg(
                                name="ADD-ON #1",
                                trigger="MomentumBurst",
                                fill_date=idx_dates[i],
                                fill_price=addon_1_price,
                                shares_fraction=ADDON_1_SHARES_FRACTION,
                                initial_stop=current_stop,
                            )
                        )
                        addon_1_filled = True
                        # Migrate stop to combined break-even.
                        new_breakeven = _combined_breakeven(legs)
                        current_stop = max(current_stop, new_breakeven)
            except ValueError:
                pass

        # 4. ADD-ON #2 — Day 7 milestone, grade-gated, regime-gated.
        if (
            pyramid_policy.enabled
            and addon_1_filled
            and not addon_2_filled
            and offset >= ADDON_2_DAY7_OFFSET
        ):
            if signal.setup_grade not in pyramid_policy.addon_2_qualified_grades:
                if not skipped_reasons or "addon_2_skipped_grade" not in skipped_reasons[-1]:
                    skipped_reasons.append(
                        f"addon_2_skipped_grade: setup_grade {signal.setup_grade} "
                        f"not in qualified set {sorted(pyramid_policy.addon_2_qualified_grades)}"
                    )
            elif pyramid_policy.regime_class not in pyramid_policy.addon_2_regime_classes:
                if not skipped_reasons or "addon_2_skipped_regime" not in skipped_reasons[-1]:
                    skipped_reasons.append(
                        f"addon_2_skipped_regime: regime_class {pyramid_policy.regime_class} "
                        f"requires {sorted(pyramid_policy.addon_2_regime_classes)}"
                    )
            else:
                try:
                    day7 = day7_compute(
                        df.iloc[: i + 1],
                        entry_date=signal.entry_date,
                        entry_low=anchor_low,
                        intraday_low_check=True,
                    )
                    if day7.output["survives_day7"]:
                        addon_2_price = bar_close
                        legs.append(
                            Leg(
                                name="ADD-ON #2",
                                trigger="Day7Milestone",
                                fill_date=idx_dates[i],
                                fill_price=addon_2_price,
                                shares_fraction=ADDON_2_SHARES_FRACTION,
                                initial_stop=current_stop,
                            )
                        )
                        addon_2_filled = True
                        # Switch trail config if specified (e.g. ma_trail 10d).
                        if pyramid_policy.trail_config_after_addon_2 is not None:
                            active_trail = pyramid_policy.trail_config_after_addon_2
                    elif day7.output["broke_entry_low"]:
                        skipped_reasons.append(
                            f"addon_2_skipped_day7_broken: anchor low {anchor_low} "
                            f"breached on day {day7.output['broke_entry_low_on_day']}"
                        )
                except (ValueError, KeyError):
                    pass

        # 5. Trail-based stop check.
        if trail_exit_signal(
            active_trail,
            bar_close=bar_close,
            bar_low=bar_low,
            current_stop=current_stop,
        ):
            exit_px = current_stop if active_trail.mode != "ma_trail" else bar_close
            reason = "trail_stop_hit" if active_trail.mode != "fixed" else "stop_hit"
            return _build_outcome(
                exit_date=idx_dates[i],
                exit_price=exit_px,
                reason=reason,
                bars_held=offset,
            )

        # 6. Target hit.
        if target is not None and bar_high >= target:
            return _build_outcome(
                exit_date=idx_dates[i],
                exit_price=target,
                reason="target_hit",
                bars_held=offset,
            )

        # 7. Max-hold.
        if offset == signal.max_hold_days:
            return _build_outcome(
                exit_date=idx_dates[i],
                exit_price=bar_close,
                reason="max_hold",
                bars_held=offset,
            )
    raise RuntimeError("pyramid_simulator: fell out of loop unexpectedly")


def simulate_signals_pyramided(
    signals: list[TradeSignal],
    df: pd.DataFrame,
    trail_config: TrailConfig | None = None,
    pyramid_policy: PyramidPolicy | None = None,
) -> list[PyramidedTradeOutcome]:
    return [
        simulate_trade_pyramided(s, df, trail_config=trail_config, pyramid_policy=pyramid_policy)
        for s in signals
    ]
