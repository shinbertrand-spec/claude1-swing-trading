"""Tests for tools.trace_rerun."""
from __future__ import annotations

import pytest

from tools.trace_rerun import (
    TraceRerunError,
    assert_traces_consistent,
    rerun,
)


def _ledger_with_steps(steps):
    return {"reasoning_trace": list(steps)}


def test_pure_tool_matches_when_inputs_replay_exactly():
    """Re-running compute_yoy(1.87, 1.55) reproduces recorded output."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(1.87, 1.55)
    recorded_dict = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": recorded.output,
        "fetched_at": recorded.fetched_at,
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    assert len(report.results) == 1
    assert report.results[0].status == "match"
    assert report.results[0].klass == "pure"


def test_pure_tool_divergent_detected():
    """Mutate recorded output → divergent."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(1.87, 1.55)
    bad_output = dict(recorded.output)
    bad_output["yoy_growth_decimal"] = 0.99   # wrong
    recorded_dict = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": bad_output,
        "fetched_at": recorded.fetched_at,
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    r = report.results[0]
    assert r.status == "divergent"
    assert r.diffs[0]["path"].endswith("yoy_growth_decimal")


def test_pure_tool_signature_mismatch_detected():
    """Recorded inputs don't match compute() signature → divergent."""
    recorded_dict = {
        "id": 1,
        "tool": "tools/compute_yoy.py",
        "inputs": {"foo": "bar"},   # wrong shape
        "output": {"yoy_growth_decimal": 0.0},
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    r = report.results[0]
    assert r.status == "divergent"
    assert "signature" in r.detail or "argument" in r.detail or "unexpected" in r.detail.lower()


def test_ohlcv_tool_shape_ok():
    """OHLCV tool with correct keys → shape_ok."""
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"period": 14, "ticker": "AAPL"},
        "output": {
            "atr": 4.5,
            "atr_pct_of_close": 0.023,
            "period": 14,
            "last_close": 192.74,
            "last_close_date": "2026-05-17",
        },
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_ok"
    assert r.klass == "ohlcv"


def test_ohlcv_tool_shape_fail_missing_key():
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"period": 14},
        "output": {"atr": 4.5},   # missing required keys
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_fail"


def test_manual_tool_well_formed():
    recorded = {
        "id": 1,
        "tool": "manual:broker_api",
        "inputs": {"ticker": "AAPL"},
        "output": {"some": "value"},
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "well_formed"
    assert r.klass == "manual"


def test_unknown_tool_flagged():
    recorded = {
        "id": 1,
        "tool": "tools/nonexistent.py",
        "inputs": {},
        "output": None,
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "unknown_tool"


def test_assert_raises_on_divergence():
    recorded = {
        "id": 1,
        "tool": "tools/compute_yoy.py",
        "inputs": {"current": 2.0, "prior": 1.0},
        "output": {"yoy_growth_decimal": 99.0, "yoy_growth_pct": 9900.0},   # wrong
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    with pytest.raises(TraceRerunError, match="step id=1"):
        assert_traces_consistent(_ledger_with_steps([recorded]))


def test_assert_passes_on_clean():
    from tools.stop_sizer import compute as stop_compute
    recorded = stop_compute(entry_price=192.74, atr=4.57)
    step = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": recorded.output,
        "fetched_at": recorded.fetched_at,
    }
    entry = assert_traces_consistent(_ledger_with_steps([step]))
    assert entry.output["has_divergence"] is False


# --- Cosmetic display-rounding: a recorded value rounded for readability must
#     NOT be flagged as a divergence, but aggressive rounding still is. ---

def test_faithful_rounding_matches_4dp_yoy():
    """Ledger stores yoy_growth_decimal at 4dp; re-run yields full precision."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(2.01, 1.448)  # 0.3881... -> stored 0.3881
    out = dict(recorded.output)
    # Round every float to 4 decimals the way a human-written ledger would.
    out = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}
    step = {"id": 1, "tool": recorded.tool, "inputs": recorded.inputs,
            "output": out, "fetched_at": recorded.fetched_at}
    report = rerun(_ledger_with_steps([step]))
    assert report.results[0].status == "match"
    assert report.has_divergence is False


def test_aggressive_sub_unit_rounding_still_divergent():
    """0.40 stored for a true 0.388 is a material 13% rounding step -> divergent."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(2.0, 1.442)  # ~0.387 decimal
    out = dict(recorded.output)
    out["yoy_growth_decimal"] = 0.40    # over-rounded to 1dp
    step = {"id": 1, "tool": recorded.tool, "inputs": recorded.inputs,
            "output": out, "fetched_at": recorded.fetched_at}
    report = rerun(_ledger_with_steps([step]))
    assert report.results[0].status == "divergent"


def test_faithful_rounding_helper_bounds():
    from tools.trace_rerun import _is_faithful_rounding, _decimal_places
    assert _decimal_places(0.3883) == 4
    assert _decimal_places(439.44) == 2
    assert _decimal_places(5.0) == 1   # repr(5.0) == '5.0' -> one decimal place
    assert _is_faithful_rounding(0.3883, 0.3883495145631066) is True
    assert _is_faithful_rounding(439.44, 439.438) is True
    assert _is_faithful_rounding(0.39, 0.3883495) is True          # legit 2dp
    assert _is_faithful_rounding(0.40, 0.3883495) is False         # aggressive 1dp
    assert _is_faithful_rounding(450.0, 439.438) is False          # not a rounding
    assert _is_faithful_rounding(True, 1) is False                 # bools never


# --- Schema growth: re-run may emit keys the recorded ledger predates. ---

def test_pure_tool_schema_growth_tolerated():
    """Recorded output is a subset of the (newer) re-run output -> match."""
    from tools.stop_sizer import compute as stop_compute
    recorded = stop_compute(entry_price=192.74, atr=4.57)
    # Simulate an older ledger that only stored a few of today's keys.
    kept = {"stop_price", "stop_distance_pct", "binding_constraint"}
    out = {k: v for k, v in recorded.output.items() if k in kept}
    assert set(out) < set(recorded.output)  # genuinely a subset
    step = {"id": 1, "tool": recorded.tool, "inputs": recorded.inputs,
            "output": out, "fetched_at": recorded.fetched_at}
    report = rerun(_ledger_with_steps([step]))
    assert report.results[0].status == "match"


def test_pure_tool_recorded_extra_key_still_divergent():
    """A recorded key the tool no longer produces is a retired/fabricated field."""
    from tools.stop_sizer import compute as stop_compute
    recorded = stop_compute(entry_price=192.74, atr=4.57)
    out = dict(recorded.output)
    out["phantom_field"] = 123.0   # not produced by the tool
    step = {"id": 1, "tool": recorded.tool, "inputs": recorded.inputs,
            "output": out, "fetched_at": recorded.fetched_at}
    report = rerun(_ledger_with_steps([step]))
    r = report.results[0]
    assert r.status == "divergent"
    assert any(d["path"].endswith("phantom_field") for d in r.diffs)


# --- OHLCV value-slice: core keys present, only metadata missing. ---

def test_ohlcv_value_slice_is_shape_partial_not_fail():
    """Agent stored atr core values without period/last_close_date metadata."""
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"ticker": "AMAT"},
        "output": {"atr": 22.62, "atr_pct_of_close": 0.04993, "last_close": 453.01},
        "fetched_at": "2026-06-06T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_partial"
    assert "last_close_date" in r.missing_keys and "period" in r.missing_keys
    # shape_partial must NOT count as a divergence (would otherwise BLOCK).
    assert report.has_divergence is False
    assert report.divergent_count == 0


def test_ohlcv_missing_core_key_still_shape_fail():
    """Dropping a CORE key (last_close) is a real authenticity failure."""
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"ticker": "AMAT"},
        "output": {"atr": 22.62, "atr_pct_of_close": 0.04993},  # no last_close
        "fetched_at": "2026-06-06T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_fail"
    assert report.has_divergence is True
