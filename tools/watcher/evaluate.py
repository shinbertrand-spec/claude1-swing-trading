"""Entry-trigger evaluator — the deterministic brain of the watcher.

Given a watchlist entry's structured ``trigger`` block + an OHLCV frame + the
current broad-market regime, classify how close the name is to a buyable entry:

    inactive   — far from the trigger; nothing to watch yet
    approaching — within ``approaching_pct`` of the zone/level
    in_zone    — price is in the buy zone (or has broken the level) but the
                 confirming condition (reversal candle / volume) hasn't fired
    fired      — all trigger conditions met → this is the entry tap
    blocked    — trigger would qualify but the regime gate fails (SPY stage 3/4)

The status ordering (inactive < approaching < in_zone < fired) drives the
edge-triggered push logic in :mod:`tools.watcher.state`.

Trigger types (v1):
    pullback_zone — buy a pullback into [zone_low, zone_high]; optional reversal
                    candle confirmation (composes :mod:`tools.pullback_detect`).
    breakout      — buy a close above ``level`` on volume >= ``min_volume_mult``
                    (also used for an MA "reclaim": set level to the MA value).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import pandas as pd

from ..pullback_detect import compute_from_ohlcv as pullback_compute

# Status constants + their escalation rank (higher = more actionable).
STATUS_INACTIVE = "inactive"
STATUS_BLOCKED = "blocked"
STATUS_APPROACHING = "approaching"
STATUS_IN_ZONE = "in_zone"
STATUS_FIRED = "fired"

RANK: dict[str, int] = {
    STATUS_INACTIVE: 0,
    STATUS_BLOCKED: 0,
    STATUS_APPROACHING: 1,
    STATUS_IN_ZONE: 2,
    STATUS_FIRED: 3,
}

DEFAULT_APPROACHING_PCT = 3.0


@dataclass
class WatchSignal:
    ticker: str
    status: str
    price: float
    trigger_type: str
    distance_pct: float            # signed % from the actionable edge (− = past it)
    reason: str                    # one-line human summary
    detail: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)  # entry/stop/target/rr/note

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reversal_candle(df: pd.DataFrame) -> tuple[bool, Optional[str]]:
    """Compose pullback_detect to get today's bullish-reversal-candle read."""
    try:
        out = pullback_compute(df).output
        return bool(out["criteria"]["bullish_reversal_candle"]), out.get("candle_type")
    except Exception:  # noqa: BLE001 — candle detection is best-effort
        return False, None


def _vol_ratio(df: pd.DataFrame, period: int = 20) -> float:
    v = df["Volume"].astype(float)
    if len(v) < period + 1:
        return 0.0
    avg = float(v.iloc[-(period + 1):-1].mean())
    return float(v.iloc[-1]) / avg if avg > 0 else 0.0


def evaluate_entry(
    ticker: str,
    trigger: dict[str, Any],
    df: pd.DataFrame,
    *,
    regime_ok: bool,
    approaching_pct: float = DEFAULT_APPROACHING_PCT,
) -> WatchSignal:
    """Classify one watchlist entry against its structured trigger."""
    ttype = trigger.get("type")
    price = float(df["Close"].astype(float).iloc[-1])
    payload = {
        k: trigger.get(k)
        for k in ("entry", "stop", "target", "rr", "note")
        if trigger.get(k) is not None
    }

    def sig(status: str, distance_pct: float, reason: str, detail: dict) -> WatchSignal:
        return WatchSignal(
            ticker=ticker, status=status, price=round(price, 2),
            trigger_type=ttype or "unknown", distance_pct=round(distance_pct, 2),
            reason=reason, detail=detail, payload=payload,
        )

    # Regime gate: a would-be entry is BLOCKED when the broad market is stage 3/4.
    regime_gate = trigger.get("regime_gate", True)

    if ttype == "pullback_zone":
        lo = float(trigger["zone_low"])
        hi = float(trigger["zone_high"])
        need_candle = trigger.get("require_reversal_candle", True)
        if price > hi:
            dist = (price - hi) / hi * 100.0  # how far price must FALL to the zone
            if dist <= approaching_pct:
                return sig(STATUS_APPROACHING, dist,
                           f"{dist:.1f}% above buy zone ${lo:.0f}-${hi:.0f}",
                           {"zone": [lo, hi]})
            return sig(STATUS_INACTIVE, dist,
                       f"{dist:.1f}% above buy zone ${lo:.0f}-${hi:.0f}", {"zone": [lo, hi]})
        if price < lo:
            dist = (price - lo) / lo * 100.0  # overshot below — possible knife
            return sig(STATUS_INACTIVE, dist,
                       f"overshot {abs(dist):.1f}% below zone (broke ${lo:.0f}) — watch by hand",
                       {"zone": [lo, hi], "overshoot": True})
        # In the zone.
        candle, ctype = (_reversal_candle(df) if need_candle else (True, None))
        if regime_gate and not regime_ok:
            return sig(STATUS_BLOCKED, 0.0, "in zone but SPY regime is stage 3/4 — blocked",
                       {"zone": [lo, hi], "reversal_candle": candle})
        if candle:
            return sig(STATUS_FIRED, 0.0,
                       f"IN ZONE + reversal candle ({ctype or 'confirmed'}) — entry trigger",
                       {"zone": [lo, hi], "candle_type": ctype})
        return sig(STATUS_IN_ZONE, 0.0, "in buy zone — waiting on a reversal candle",
                   {"zone": [lo, hi], "reversal_candle": False})

    if ttype in ("breakout", "reclaim"):
        level = float(trigger["level"])
        min_vol = float(trigger.get("min_volume_mult", 1.4))
        vr = _vol_ratio(df)
        if price < level:
            dist = (level - price) / level * 100.0  # how far to rise
            status = STATUS_APPROACHING if dist <= approaching_pct else STATUS_INACTIVE
            return sig(status, dist, f"{dist:.1f}% below the ${level:.2f} trigger",
                       {"level": level, "vol_ratio": round(vr, 2)})
        # Broke above the level.
        if regime_gate and not regime_ok:
            return sig(STATUS_BLOCKED, 0.0, "broke level but SPY regime is stage 3/4 — blocked",
                       {"level": level, "vol_ratio": round(vr, 2)})
        if vr >= min_vol:
            return sig(STATUS_FIRED, 0.0,
                       f"BROKE ${level:.2f} on {vr:.1f}x volume — entry trigger",
                       {"level": level, "vol_ratio": round(vr, 2)})
        return sig(STATUS_IN_ZONE, 0.0,
                   f"above ${level:.2f} but volume only {vr:.1f}x (need {min_vol:.1f}x)",
                   {"level": level, "vol_ratio": round(vr, 2)})

    return sig(STATUS_INACTIVE, 0.0, f"unknown trigger type {ttype!r}", {})
