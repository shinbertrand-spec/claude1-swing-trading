"""RSI(14) bullish-divergence detector (per ``swing-setup-library.md`` Secondary 2).

Bullish divergence: price makes a new lower low while RSI makes a higher low.
The premise is that downside momentum is fading even as price grinds lower,
foreshadowing a reversal.

Criteria (all must pass):

* Price makes lower low: latest swing low < prior swing low
* RSI(14) at latest swing low > RSI at prior swing low
* Latest swing low is at obvious support: within ``support_proximity_pct``
  of 50-day SMA, 200-day SMA, or recent prior swing low
* Volume on new price low ≤ volume on prior low (no panic capitulation)

Per swing-setup-library: "use for mean-reversion within an uptrend ONLY;
pass if Stage 4 broad market." Caller verifies broad-market regime.

CLI::

    uv run python -m tools.rsi_divergence NVDA
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/rsi_divergence.py"

RSI_PERIOD = 14
SWING_WINDOW = 5                  # bars each side for swing-low detection
LOOKBACK_BARS = 60                # ~3 months to find two swing lows
DEFAULT_SUPPORT_PROXIMITY_PCT = 2.0  # within 2% of major MA / prior low


def _wilder_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder smoothing RSI."""
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    # Seed: simple mean of first `period`.
    avg_gain = gains.copy()
    avg_loss = losses.copy()
    avg_gain.iloc[:period] = gains.iloc[:period].mean()
    avg_loss.iloc[:period] = losses.iloc[:period].mean()
    for i in range(period, len(close)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gains.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + losses.iloc[i]) / period
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _find_swing_lows(low: pd.Series, window: int = SWING_WINDOW) -> list[int]:
    """Return indices where low is the local min within [i-window, i+window]."""
    troughs: list[int] = []
    values = low.to_numpy()
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] < left.min() and values[i] < right.min():
            troughs.append(i)
    return troughs


def compute_from_ohlcv(
    df: pd.DataFrame,
    support_proximity_pct: float = DEFAULT_SUPPORT_PROXIMITY_PCT,
) -> TraceEntry:
    """Detect bullish RSI divergence anchored at the two most recent swing lows.

    Args:
        df: OHLCV DataFrame. Needs Low, Close, Volume.
        support_proximity_pct: how close to a major MA / prior swing low
            the latest swing low must be to qualify as "at support."
    """
    required = {"Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < max(LOOKBACK_BARS, 200) + SWING_WINDOW:
        raise ValueError(
            f"need at least {max(LOOKBACK_BARS, 200) + SWING_WINDOW} bars; got {len(df)}"
        )

    close = df["Close"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    rsi = _wilder_rsi(close, RSI_PERIOD)

    window_low = low.iloc[-LOOKBACK_BARS:]
    troughs_local = _find_swing_lows(window_low)
    if len(troughs_local) < 2:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "support_proximity_pct": support_proximity_pct,
                "rows": len(df),
                "last_close_date": str(df.index[-1]),
            },
            output={
                "detected": False,
                "reason": f"need >= 2 swing lows in last {LOOKBACK_BARS} bars; got {len(troughs_local)}",
            },
        )

    # Translate window-local indices back to df-absolute indices.
    base_offset = len(df) - LOOKBACK_BARS
    troughs = [base_offset + t for t in troughs_local]
    prior_idx, latest_idx = troughs[-2], troughs[-1]

    prior_low = float(low.iloc[prior_idx])
    latest_low = float(low.iloc[latest_idx])
    prior_rsi = float(rsi.iloc[prior_idx])
    latest_rsi = float(rsi.iloc[latest_idx])
    prior_vol = float(volume.iloc[prior_idx])
    latest_vol = float(volume.iloc[latest_idx])

    price_lower_low = latest_low < prior_low
    rsi_higher_low = latest_rsi > prior_rsi
    divergence = price_lower_low and rsi_higher_low

    # Support proximity check: 50d SMA, 200d SMA, prior swing low.
    sma_50 = float(close.iloc[: latest_idx + 1].tail(50).mean())
    sma_200 = float(close.iloc[: latest_idx + 1].tail(200).mean())
    proximity_tol = latest_low * (support_proximity_pct / 100.0)
    near_50sma = abs(latest_low - sma_50) <= proximity_tol
    near_200sma = abs(latest_low - sma_200) <= proximity_tol
    near_prior_low = abs(latest_low - prior_low) <= proximity_tol
    at_support = near_50sma or near_200sma or near_prior_low

    # Volume confirmation: latest <= prior volume.
    volume_confirms = latest_vol <= prior_vol

    detected = divergence and at_support and volume_confirms

    return TraceEntry(
        tool=TOOL,
        inputs={
            "support_proximity_pct": support_proximity_pct,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "detected": detected,
            "criteria": {
                "price_lower_low": price_lower_low,
                "rsi_higher_low": rsi_higher_low,
                "at_support": at_support,
                "volume_confirms": volume_confirms,
            },
            "swing_lows": {
                "prior": {"index": prior_idx, "price": prior_low, "rsi": prior_rsi, "volume": prior_vol},
                "latest": {"index": latest_idx, "price": latest_low, "rsi": latest_rsi, "volume": latest_vol},
            },
            "support_proximity": {
                "near_50sma": near_50sma,
                "near_200sma": near_200sma,
                "near_prior_low": near_prior_low,
                "sma_50": sma_50,
                "sma_200": sma_200,
            },
            "suggested_stop": latest_low - 0.05 if detected else None,
        },
    )


def compute_from_ticker(ticker: str) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="2y")
    entry = compute_from_ohlcv(fetch.df)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.rsi_divergence",
        description="Bullish RSI(14) divergence at support (Secondary 2).",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
