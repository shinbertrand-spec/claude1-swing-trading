"""Tests for tools.vcp_detect — Phase 2 baseline heuristic."""
from __future__ import annotations

import pytest

from tools.vcp_detect import compute_from_ohlcv


def test_vcp_detected_on_synthetic(vcp_ohlcv):
    """The fixture is hand-shaped: 3 progressive contractions then breakout."""
    e = compute_from_ohlcv(vcp_ohlcv, weeks=12)
    out = e.output
    assert out["contractions_count"] >= 2
    # Depths should be progressive (decreasing) on a clean VCP fixture.
    depths = [c["depth_pct"] for c in out["contractions"]]
    if len(depths) >= 2:
        assert all(depths[i] < depths[i - 1] for i in range(1, len(depths)))
    assert out["final_depth_pct"] is not None
    assert out["pivot"] is not None
    # Breakout-bar last close above pivot.
    assert out["above_pivot"] is True
    # Phase 2 baseline marker present.
    assert "phase2_baseline_note" in out


def test_uptrend_without_clear_contractions(uptrend_ohlcv):
    """Smooth uptrend has noise but no clear progressive VCP shape; assert
    that the tool runs and reports something coherent — don't require it to
    refuse the trade since heuristic may produce noise-driven contractions."""
    e = compute_from_ohlcv(uptrend_ohlcv, weeks=12)
    # Always emits the shape; detected can be either; verify the shape only.
    assert "detected" in e.output
    assert "volume_ratio" in e.output


def test_volume_ratio_present(vcp_ohlcv):
    e = compute_from_ohlcv(vcp_ohlcv, weeks=12)
    # Fixture's last-bar volume is engineered to ~1.6× 20d avg.
    assert e.output["volume_ratio"] > 1.2


def test_missing_columns_raises():
    import pandas as pd

    df = pd.DataFrame(
        {"Close": [100.0] * 100},
        index=pd.date_range("2024-01-02", periods=100, freq="B"),
    )
    with pytest.raises(ValueError, match="Volume"):
        compute_from_ohlcv(df)
