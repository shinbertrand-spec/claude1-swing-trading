"""EP (Episodic Pivot) setup replay.

Per ``swing-earnings-pivot.md`` — Phase 5.b adds EP to the backtest
registry. Walks historical OHLCV bar-by-bar; at each bar tests whether
the previous bar's catalyst signature qualifies as an EP, and if so
emits a :class:`TradeSignal` to enter on the catalyst bar's continuation
(next bar's open) at the opening range high.

# Phase 5.b approximation: MAGNA's fundamental criteria (massive earnings
# surprise, analyst upgrades) require an earnings + analyst-revision
# history not present in OHLCV. We approximate from price + volume:
#
#   * M (massive earnings)        → gap ≥ 15% (extreme gap presumes a
#                                    massive fundamental surprise)
#   * A (after-hours gap)         → gap ≥ 10% AND volume ≥ 3× ADV
#   * G (gap confirmed)           → today's close > today's open
#                                    (held the gap)
#   * N (neglected)               → prior_rally_pct: 3m AND 6m returns
#                                    ≤ 20%
#   * A (analyst upgrades)        → OMITTED (no source in OHLCV)
#
# Score is therefore 0-4 (not 0-5). Tag for Phase 5.c refinement after
# fundamentals-history data source integration (e.g. SimFin, sharadar).

Stop placement: lows of the EP day (signal bar), capped at min(ATR, 8%).
Trail mode is configured at the simulator level — for EP, ``ma_trail``
(10-day MA) is the Kullamägi-school default and recommended.
"""
from __future__ import annotations

import pandas as pd

from ..atr_compute import compute_from_ohlcv as atr_compute
from .setup_replay import TradeSignal, SETUP_REPLAY_REGISTRY

# Phase 5.b baseline thresholds.
MIN_GAP_PCT = 0.10
MASSIVE_GAP_PCT = 0.15
MIN_VOLUME_RATIO = 3.0
NEGLECTED_LOOKBACK_3M = 63
NEGLECTED_LOOKBACK_6M = 126
# "Neglected base" means the stock isn't in a euphoric runaway. For the
# original MAGNA on small/mid-caps, 20% over 6mo was tight. For an
# S&P 500-leaning liquid universe, normal bull-market quality names
# trend up 30-40%/yr; 20% is too strict and rejects nearly all candidates.
# 50% over 6mo is genuinely "not yet euphoric" and matches the doctrine's
# intent: filter out names that have already had their move, not names
# in a healthy uptrend.
NEGLECTED_THRESHOLD = 0.50
MIN_HISTORY_BARS = 200    # need enough for 6m lookback + ATR + indicators
GAP_LARGE_FAIL_PCT = 0.20  # 20%+ gaps have 44.8% Day-1 failure → downgrade


def _approx_magna_score(
    df_through_today: pd.DataFrame,
) -> tuple[int, dict, float, float]:
    """Return (score, breakdown, gap_pct, volume_ratio).

    Evaluated using prev-bar close + today's open/close + today's volume.
    """
    today = df_through_today.iloc[-1]
    prev_close = float(df_through_today["Close"].iloc[-2])
    today_open = float(today["Open"])
    today_close = float(today["Close"])
    today_volume = float(today["Volume"])
    adv_20 = float(df_through_today["Volume"].iloc[-21:-1].mean())

    gap_pct = (today_open - prev_close) / prev_close if prev_close > 0 else 0.0
    volume_ratio = today_volume / adv_20 if adv_20 > 0 else 0.0

    # Neglected check.
    if len(df_through_today) >= NEGLECTED_LOOKBACK_6M + 1:
        close_now = float(df_through_today["Close"].iloc[-1])
        close_3m = float(df_through_today["Close"].iloc[-(NEGLECTED_LOOKBACK_3M + 1)])
        close_6m = float(df_through_today["Close"].iloc[-(NEGLECTED_LOOKBACK_6M + 1)])
        rally_3m = (close_now / close_3m) - 1.0 if close_3m > 0 else 0.0
        rally_6m = (close_now / close_6m) - 1.0 if close_6m > 0 else 0.0
        neglected = (rally_3m <= NEGLECTED_THRESHOLD) and (rally_6m <= NEGLECTED_THRESHOLD)
    else:
        rally_3m = rally_6m = 0.0
        neglected = False

    m_pass = gap_pct >= MASSIVE_GAP_PCT
    a1_pass = (gap_pct >= MIN_GAP_PCT) and (volume_ratio >= MIN_VOLUME_RATIO)
    g_pass = today_close > today_open
    n_pass = neglected

    score = sum([m_pass, a1_pass, g_pass, n_pass])
    return score, {
        "M_massive_gap": m_pass,
        "A_gap_volume": a1_pass,
        "G_gap_confirmed": g_pass,
        "N_neglected": n_pass,
        "rally_3m_pct": rally_3m,
        "rally_6m_pct": rally_6m,
    }, gap_pct, volume_ratio


def _grade_ep(
    score: int,
    gap_pct: float,
    intraday_expansion_pct: float,
) -> str:
    """Phase 5.b grading — same shape as :mod:`tools.ep_grade` but with
    the score scaled out of 4 (MAGNA-A_analyst omitted).

    Threshold mapping: original ep_grade.py uses 4-of-5 → Swan. Faithful
    translation to the 4-criterion OHLCV approximation is 3-of-4 → Swan
    (i.e., one missing criterion permitted), not 4-of-4 (which was
    effectively "perfect score"). The caller (_detect_ep_at_bar) gates
    additionally on G_gap_confirmed to preserve the "held the gap" rule.
    """
    if score >= 3:
        grade = "Swan"
    else:
        grade = "Chicken"

    # SuperSwan upgrade: gap >= MASSIVE_GAP + intraday_expansion >= 5%
    # (close significantly above open) + neglected (already in score).
    if grade == "Swan" and gap_pct >= MASSIVE_GAP_PCT and intraday_expansion_pct >= 0.05:
        grade = "SuperSwan"

    # Golden EP upgrade: SuperSwan + gap in 10-19% sweet spot.
    if grade == "SuperSwan" and 0.10 <= gap_pct < 0.20:
        grade = "GoldenEP"

    # Large-gap downgrade (per swing-earnings-pivot 20%+ failure rate).
    if gap_pct >= GAP_LARGE_FAIL_PCT and grade in {"GoldenEP", "SuperSwan", "Swan"}:
        downgraded = {"GoldenEP": "Swan", "SuperSwan": "Swan", "Swan": "Duck"}
        grade = downgraded[grade]
    return grade


def _detect_ep_at_bar(df_slice: pd.DataFrame) -> tuple[bool, str | None, dict]:
    """Return (detected, grade, evidence) at the last bar of ``df_slice``."""
    if len(df_slice) < MIN_HISTORY_BARS:
        return False, None, {"reason": f"insufficient history ({len(df_slice)} < {MIN_HISTORY_BARS})"}

    score, breakdown, gap_pct, volume_ratio = _approx_magna_score(df_slice)
    if gap_pct < MIN_GAP_PCT:
        return False, None, {"reason": f"gap {gap_pct:.4f} below {MIN_GAP_PCT}"}

    today = df_slice.iloc[-1]
    today_open = float(today["Open"])
    today_high = float(today["High"])
    intraday_expansion_pct = (today_high - today_open) / today_open if today_open > 0 else 0.0

    if score < 3:
        return False, None, {
            "reason": f"approx-MAGNA score {score} < 3",
            "breakdown": breakdown,
            "gap_pct": gap_pct,
        }

    # Doctrine guard: even at score=3, refuse to enter if today's close
    # was at/below today's open — the gap was not held. EP without
    # confirmation = high-failure setup.
    if not breakdown["G_gap_confirmed"]:
        return False, None, {
            "reason": "gap not held (today close <= open)",
            "breakdown": breakdown,
            "gap_pct": gap_pct,
        }

    grade = _grade_ep(score, gap_pct, intraday_expansion_pct)
    # Chicken = score below 3. Duck = downgrade from Swan after a 20%+ gap
    # (44.8% Day-1 failure rate per doctrine). Both are non-tradeable.
    if grade in {"Chicken", "Duck"}:
        return False, None, {
            "reason": f"grade {grade} below Swan threshold",
            "score": score,
            "gap_pct": gap_pct,
        }

    return True, grade, {
        "approx_magna_score": score,
        "breakdown": breakdown,
        "gap_pct": gap_pct,
        "volume_ratio": volume_ratio,
        "intraday_expansion_pct": intraday_expansion_pct,
        "opening_range_high": today_high,    # use day's high as ORH proxy (no intraday data)
    }


def replay_ep(
    df: pd.DataFrame,
    ticker: str,
    start_index: int = MIN_HISTORY_BARS,
    max_hold_days: int = 60,           # EP hold is multi-week; longer than SEPA-VCP
    target_r_multiple: float = 3.0,    # higher R-target — EP supports it
) -> list[TradeSignal]:
    """Walk ``df`` bar-by-bar; emit a TradeSignal each time EP fires.

    Args:
        df: OHLCV DataFrame indexed by date.
        ticker: ticker symbol.
        start_index: skip the first N bars so detectors have history.
        max_hold_days: simulator exits after this many bars if no stop /
            target triggers first. Default 60 for EPs (longer than SEPA).
        target_r_multiple: target = entry + R × (entry - stop). Default
            3.0 — EP setups support higher targets per Stockbee Day-7
            sustain data.

    Returns:
        list of :class:`TradeSignal`. Empty if nothing fires.
    """
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")

    signals: list[TradeSignal] = []
    n = len(df)
    for i in range(start_index, n - 1):
        df_slice = df.iloc[: i + 1]
        detected, grade, evidence = _detect_ep_at_bar(df_slice)
        if not detected:
            continue

        # Entry = next bar's open (no look-ahead on signal bar).
        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])

        # Stop = signal bar's low, capped at 8% below entry.
        signal_low = float(df.iloc[i]["Low"])
        max_stop_dist = entry_price * 0.08
        stop_price = max(signal_low, entry_price - max_stop_dist)
        if stop_price >= entry_price:
            # Can happen if next open is far below signal low; skip.
            continue

        try:
            atr_entry = atr_compute(df_slice, period=14)
        except ValueError:
            continue
        atr_value = atr_entry.output["atr"]
        target_price = entry_price + target_r_multiple * (entry_price - stop_price)

        signal_date = pd.Timestamp(df.index[i]).date()
        fill_date = pd.Timestamp(df.index[i + 1]).date()
        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type="EP",
                setup_grade=grade,
                entry_date=signal_date,
                fill_date=fill_date,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes=evidence,
            )
        )
    return signals


# Register at import time so the runner sees EP.
SETUP_REPLAY_REGISTRY["EP"] = replay_ep
