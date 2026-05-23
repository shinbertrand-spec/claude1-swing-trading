"""Tests for tools.prior_rally_pct."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tools.prior_rally_pct import compute_from_ohlcv


def _df_with_returns(r3m: float, r6m: float, n: int = 130) -> pd.DataFrame:
    """Build OHLCV where close@-126 yields r6m and close@-63 yields r3m
    relative to the final close at $100."""
    closes = [50.0] * n
    closes[-1] = 100.0
    closes[-(63 + 1)] = 100.0 / (1.0 + r3m)
    closes[-(126 + 1)] = 100.0 / (1.0 + r6m)
    return pd.DataFrame(
        {"Close": closes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def test_neglected_when_both_returns_low():
    df = _df_with_returns(r3m=0.04, r6m=-0.12)
    e = compute_from_ohlcv(df, neglected_threshold=0.20)
    assert math.isclose(e.output["rally_3m_pct"], 0.04, rel_tol=1e-6)
    assert math.isclose(e.output["rally_6m_pct"], -0.12, rel_tol=1e-6)
    assert e.output["neglected"] is True


def test_not_neglected_when_3m_above_threshold():
    df = _df_with_returns(r3m=0.35, r6m=0.05)
    e = compute_from_ohlcv(df, neglected_threshold=0.20)
    assert e.output["neglected"] is False


def test_not_neglected_when_6m_above_threshold():
    df = _df_with_returns(r3m=0.10, r6m=0.40)
    e = compute_from_ohlcv(df, neglected_threshold=0.20)
    assert e.output["neglected"] is False


def test_custom_threshold():
    df = _df_with_returns(r3m=0.15, r6m=0.18)
    e = compute_from_ohlcv(df, neglected_threshold=0.10)
    assert e.output["neglected"] is False
    e = compute_from_ohlcv(df, neglected_threshold=0.25)
    assert e.output["neglected"] is True


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {"Close": [100.0] * 50},
        index=pd.date_range("2024-01-02", periods=50, freq="B"),
    )
    with pytest.raises(ValueError, match="127"):
        compute_from_ohlcv(df)


def test_missing_close_raises():
    df = pd.DataFrame({"Open": [100.0] * 200})
    with pytest.raises(ValueError, match="Close"):
        compute_from_ohlcv(df)
