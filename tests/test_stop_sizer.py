"""Tests for tools.stop_sizer."""
from __future__ import annotations

import math

import pytest

from tools.stop_sizer import compute


def test_atr_binds_when_tighter():
    """Apple-like A+ example: ATR×2 < 8% cap → ATR binds."""
    e = compute(entry_price=192.74, atr=4.57, atr_multiple=2.0)
    assert e.output["binding_constraint"] == "atr_x_multiple"
    assert math.isclose(e.output["stop_distance"], 4.57 * 2.0, rel_tol=1e-9)
    assert math.isclose(e.output["stop_price"], 192.74 - 9.14, rel_tol=1e-9)
    assert e.output["skip_signal_atr_exceeds_cap"] is False


def test_minervini_cap_binds_for_high_vol():
    """Tesla-like high-vol example: ATR×2 > 8% cap → cap binds, skip signal fires."""
    e = compute(entry_price=250.0, atr=12.0, atr_multiple=2.0)
    assert e.output["binding_constraint"] == "minervini_8pct_cap"
    assert math.isclose(e.output["stop_distance"], 250.0 * 0.08, rel_tol=1e-9)
    assert e.output["skip_signal_atr_exceeds_cap"] is True


def test_adr_binds_when_tightest():
    """ADR cap (1% × $100 = $1) tighter than ATR×2 ($4) and 8% cap ($8)."""
    e = compute(entry_price=100.0, atr=2.0, adr_pct=1.0, atr_multiple=2.0)
    assert e.output["binding_constraint"] == "adr_pct"
    assert math.isclose(e.output["stop_distance"], 1.0, rel_tol=1e-9)


def test_rejects_nonpositive_entry():
    with pytest.raises(ValueError, match="entry_price"):
        compute(entry_price=0.0, atr=1.0)


def test_rejects_nonpositive_atr():
    with pytest.raises(ValueError, match="atr"):
        compute(entry_price=100.0, atr=-1.0)


def test_audit_fields_present():
    e = compute(entry_price=100.0, atr=1.0, adr_pct=2.0)
    out = e.output
    assert out["atr_distance"] == 2.0
    assert out["minervini_distance"] == 8.0
    assert out["adr_distance"] == 2.0
    # ATR and ADR tie at 2.0 — min() picks 'atr_x_multiple' due to insertion order
    assert out["binding_constraint"] in {"atr_x_multiple", "adr_pct"}
