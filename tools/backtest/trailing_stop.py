"""Trailing-stop policies for the Phase 5 simulator.

Three modes, pure-function each:

* **fixed** — stop never moves. Matches Phase 5.a baseline behavior.
* **ratchet** — per ``swing-position-sizing``: trail stop to break-even
  once position is +5%, then to entry+5% once at +10%. Stop only ever
  tightens.
* **ma_trail** — Kullamägi-school: trail stop to the N-day SMA of close
  (default 10-day). Exit on close below the SMA. Stop only ever
  tightens — the SMA going up moves the trail up; SMA going down does
  NOT loosen the existing stop.

Each policy exposes:

    update_stop(*, current_stop, entry_price, ohlcv_so_far) -> new_stop

where ``ohlcv_so_far`` is the OHLCV slice from entry through the current
bar (inclusive). The returned ``new_stop`` is ``max(current_stop,
proposed_trail)`` so stops never widen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

# Per swing-position-sizing trail thresholds.
RATCHET_BREAKEVEN_AT_GAIN = 0.05    # +5% → trail to entry (break-even)
RATCHET_PLUS5_AT_GAIN = 0.10        # +10% → trail to entry + 5%
DEFAULT_MA_TRAIL_PERIOD = 10


@dataclass(frozen=True)
class TrailConfig:
    """Trail policy + parameters.

    Attributes:
        mode: one of ``"fixed"``, ``"ratchet"``, ``"ma_trail"``.
        ma_period: SMA period for ``ma_trail`` mode. Ignored otherwise.
    """

    mode: str = "fixed"
    ma_period: int = DEFAULT_MA_TRAIL_PERIOD

    def __post_init__(self) -> None:
        if self.mode not in {"fixed", "ratchet", "ma_trail"}:
            raise ValueError(
                f"unknown trail mode {self.mode!r}; "
                f"valid: 'fixed', 'ratchet', 'ma_trail'"
            )
        if self.ma_period <= 0:
            raise ValueError(f"ma_period must be positive; got {self.ma_period}")


def _fixed(*, current_stop: float, entry_price: float, ohlcv_so_far: pd.DataFrame) -> float:
    return current_stop


def _ratchet(*, current_stop: float, entry_price: float, ohlcv_so_far: pd.DataFrame) -> float:
    if len(ohlcv_so_far) == 0:
        return current_stop
    # Use highest close so far as the trail anchor — the trail is sticky
    # at the best gain achieved, not the current close (would unwind on
    # pullbacks otherwise, violating the "never widens" rule).
    best_close = float(ohlcv_so_far["Close"].astype(float).max())
    gain = (best_close / entry_price) - 1.0
    if gain >= RATCHET_PLUS5_AT_GAIN:
        proposed = entry_price * (1.0 + RATCHET_BREAKEVEN_AT_GAIN)
    elif gain >= RATCHET_BREAKEVEN_AT_GAIN:
        proposed = entry_price
    else:
        proposed = current_stop
    return max(current_stop, proposed)


def _ma_trail(*, current_stop: float, entry_price: float, ohlcv_so_far: pd.DataFrame) -> float:
    """Kullamägi-school: trail to N-day SMA of close.

    Requires at least ``ma_period`` bars from entry onward before the trail
    can engage. Until then the existing stop holds.

    Note: ``ma_period`` is read off the bound :class:`TrailConfig`; this
    function is set up by :func:`make_policy`.
    """
    raise NotImplementedError("call via make_policy('ma_trail', ma_period=N)")


def make_policy(config: TrailConfig) -> Callable[..., float]:
    """Return a callable matching the ``update_stop`` signature for the
    requested mode."""
    if config.mode == "fixed":
        return _fixed
    if config.mode == "ratchet":
        return _ratchet
    if config.mode == "ma_trail":
        period = config.ma_period

        def _ma(*, current_stop: float, entry_price: float, ohlcv_so_far: pd.DataFrame) -> float:
            if len(ohlcv_so_far) < period:
                return current_stop
            sma = float(ohlcv_so_far["Close"].astype(float).tail(period).mean())
            return max(current_stop, sma)

        return _ma
    raise ValueError(f"unreachable: mode {config.mode!r}")


def trail_exit_signal(
    config: TrailConfig,
    *,
    bar_close: float,
    bar_low: float,
    current_stop: float,
) -> bool:
    """Should the trail-stop fire an exit on this bar?

    * ``fixed`` / ``ratchet`` — exit when intrabar low <= stop (matches
      simulator's existing stop-hit semantics).
    * ``ma_trail`` — exit on a CLOSE below the trail (Kullamägi: "first
      close below trail MA = exit"). Intrabar dips don't trigger.
    """
    if config.mode == "ma_trail":
        return bar_close < current_stop
    return bar_low <= current_stop
