"""Tests for tools.trend_template."""
from __future__ import annotations

import pandas as pd
import pytest

from tools.trend_template import compute_from_ohlcv


def test_strong_uptrend_passes_most_criteria(uptrend_ohlcv):
    """Synthetic uptrend should score 7-8/8 (with RS unknown → c8 False)."""
    e = compute_from_ohlcv(uptrend_ohlcv, include_rs=True, rs_rating=85)
    out = e.output
    assert out["criteria"]["c1_price_above_150_and_200_sma"] is True
    assert out["criteria"]["c2_sma150_above_sma200"] is True
    assert out["criteria"]["c3_sma200_rising_30d"] is True
    assert out["criteria"]["c4_sma50_above_150_and_200"] is True
    assert out["criteria"]["c5_price_above_sma50"] is True
    assert out["criteria"]["c8_rs_rating_ge_70"] is True
    assert out["trend_template_passes"] >= 6
    assert out["stage"] == 2


def test_downtrend_stage_4(downtrend_ohlcv):
    e = compute_from_ohlcv(downtrend_ohlcv, include_rs=False)
    assert e.output["stage"] == 4
    assert e.output["criteria"]["c1_price_above_150_and_200_sma"] is False


def test_flat_stage_1_or_3(flat_ohlcv):
    e = compute_from_ohlcv(flat_ohlcv, include_rs=False)
    # Range-bound noise around 100; stage classifier is permissive — may be 1 or 3.
    assert e.output["stage"] in {1, 3}


def test_index_skip_rs(uptrend_ohlcv):
    """When include_rs=False, score is out of 7 not 8."""
    e = compute_from_ohlcv(uptrend_ohlcv, include_rs=False)
    assert e.output["trend_template_total"] == 7
    assert "c8_rs_rating_ge_70" not in e.output["criteria"]
    assert e.output["rs_status"] == "skipped_for_index"


def test_rs_unknown_counts_as_false(uptrend_ohlcv):
    """Without an RS rating, c8 reports False and rs_status='unknown'."""
    e = compute_from_ohlcv(uptrend_ohlcv, include_rs=True, rs_rating=None)
    assert e.output["criteria"]["c8_rs_rating_ge_70"] is False
    assert e.output["rs_status"] == "unknown"


def test_too_few_rows_raises():
    df = pd.DataFrame(
        {"Close": [100.0] * 100},
        index=pd.date_range("2024-01-02", periods=100, freq="B"),
    )
    with pytest.raises(ValueError, match="252"):
        compute_from_ohlcv(df)
