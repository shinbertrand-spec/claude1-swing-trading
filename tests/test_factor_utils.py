"""Tests for cross-sectional factor math + sector map."""
from __future__ import annotations

import math

import pytest

from tools.quant_strategies import factor_utils as fu
from tools.quant_strategies import sector_map as sm


def test_zscore_mean0_std1():
    z = fu.zscore({"a": 1, "b": 2, "c": 3, "d": 4})
    assert sum(z.values()) == pytest.approx(0.0, abs=1e-9)
    # sample std of standardized values == 1
    vals = list(z.values())
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    assert math.sqrt(var) == pytest.approx(1.0)


def test_zscore_zero_dispersion_is_zeros():
    assert fu.zscore({"a": 5, "b": 5, "c": 5}) == {"a": 0.0, "b": 0.0, "c": 0.0}


def test_zscore_single_name_is_zero():
    assert fu.zscore({"a": 9}) == {"a": 0.0}


def test_winsorize_clips_extremes():
    vals = {f"t{i}": float(i) for i in range(100)}
    vals["outlier"] = 1e9
    w = fu.winsorize(vals, limit=0.02)
    assert w["outlier"] < 1e9            # clipped down
    assert max(w.values()) <= sorted(vals.values())[-3] + 1


def test_sector_neutralize_removes_sector_tilt():
    # Tech names all high raw value, energy all low. After neutralizing,
    # within-sector ordering is preserved but the sector level is removed.
    values = {"AAA": 10, "BBB": 12, "CCC": 1, "DDD": 3}
    sectors = {"AAA": "XLK", "BBB": "XLK", "CCC": "XLE", "DDD": "XLE"}
    z = fu.sector_neutralize(values, sectors)
    # within XLK, BBB > AAA; within XLE, DDD > CCC
    assert z["BBB"] > z["AAA"]
    assert z["DDD"] > z["CCC"]
    # the cheap-sector names are no longer uniformly lowest (tilt removed):
    # BBB (top of its sector) and DDD (top of its sector) both above their peers
    assert z["AAA"] < z["BBB"] and z["CCC"] < z["DDD"]


def test_combine_weighted_sum_on_common_tickers():
    factors = {
        "value": {"a": 1.0, "b": -1.0, "c": 0.5},
        "mom": {"a": -1.0, "b": 1.0},        # c missing
    }
    combined = fu.combine(factors, {"value": 0.5, "mom": 0.5})
    assert set(combined) == {"a", "b"}        # c excluded (missing mom)
    assert combined["a"] == pytest.approx(0.0)
    assert combined["b"] == pytest.approx(0.0)


def test_combine_value_only_weight_keeps_all():
    factors = {"value": {"a": 2.0, "b": -2.0}, "mom": {"a": 1.0, "b": 1.0}}
    combined = fu.combine(factors, {"value": 1.0, "mom": 0.0})
    assert combined == {"a": 2.0, "b": -2.0}


def test_standardize_factor_pipeline():
    raw = {f"t{i}": float(i) for i in range(50)}
    z = fu.standardize_factor(raw)
    assert sum(z.values()) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# sector map                                                                  #
# --------------------------------------------------------------------------- #
def test_build_sector_map_uses_injected_fetcher(tmp_path):
    fake = {"AAPL": "XLK", "XOM": "XLE", "WEIRD": sm.UNKNOWN}
    m = sm.build_sector_map(
        ["AAPL", "XOM", "WEIRD"], cache_path=tmp_path / "s.json",
        fetcher=lambda t: fake.get(t, sm.UNKNOWN))
    assert m == fake


def test_sic_to_etf_carveouts_and_groups():
    assert sm.sic_to_etf(3674) == "XLK"    # semiconductors
    assert sm.sic_to_etf(2834) == "XLV"    # pharma prep
    assert sm.sic_to_etf(7372) == "XLK"    # prepackaged software
    assert sm.sic_to_etf(6798) == "XLRE"   # REIT
    assert sm.sic_to_etf(6021) == "XLF"    # national commercial banks
    assert sm.sic_to_etf(4911) == "XLU"    # electric services
    assert sm.sic_to_etf(1311) == "XLE"    # crude petroleum + natural gas
    assert sm.sic_to_etf(5912) == "XLY"    # drug stores (retail)
    assert sm.sic_to_etf(9995) == sm.UNKNOWN  # nonclassifiable


def test_build_sector_map_serves_cache_without_refetch(tmp_path):
    calls = []

    def fetch(t):
        calls.append(t)
        return "XLK"

    cp = tmp_path / "s.json"
    sm.build_sector_map(["AAPL"], cache_path=cp, fetcher=fetch)
    sm.build_sector_map(["AAPL"], cache_path=cp, fetcher=fetch)  # cached
    assert calls == ["AAPL"]                      # fetched once
