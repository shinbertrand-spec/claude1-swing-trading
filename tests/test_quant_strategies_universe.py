"""Tests for tools.quant_strategies._universe.

Covers loader semantics + the legacy/registered universe transition path
used by tools.quant_strategies.runner and tools.auto_paper.quant_scanner.
"""
from __future__ import annotations

import pytest

from tools.quant_strategies import _universe


def setup_function(_func) -> None:
    _universe.reset_cache()


def test_get_universe_sp500_leaning_88():
    """The frozen 88-ticker snapshot loads cleanly and matches the count."""
    tickers = _universe.get_universe("sp500_leaning_88")
    assert isinstance(tickers, list)
    assert len(tickers) == 87  # 87 + SPY benchmark = the "88" label
    assert all(isinstance(t, str) and t.strip() == t for t in tickers)
    assert "AAPL" in tickers
    assert "NVDA" in tickers
    assert "QQQ" in tickers  # ETF intentionally in the cross-sectional pool
    assert "SPY" not in tickers  # benchmark is a separate spec field


def test_get_universe_returns_fresh_copy():
    """Caller mutation must not corrupt cached state."""
    a = _universe.get_universe("sp500_leaning_88")
    a.append("MUTATED")
    b = _universe.get_universe("sp500_leaning_88")
    assert "MUTATED" not in b


def test_get_universe_unknown_name_raises():
    with pytest.raises(_universe.UniverseError, match="no registered universe"):
        _universe.get_universe("does_not_exist")


@pytest.mark.parametrize("bad", ["", ".", "../etc/passwd", "a/b", "a\\b"])
def test_get_universe_rejects_invalid_names(bad):
    with pytest.raises(_universe.UniverseError, match="invalid universe name"):
        _universe.get_universe(bad)


def test_list_universes_includes_known():
    names = _universe.list_universes()
    assert "sp500_leaning_88" in names


def test_get_universe_metadata_strips_tickers():
    meta = _universe.get_universe_metadata("sp500_leaning_88")
    assert "tickers" not in meta
    assert meta["name"] == "sp500_leaning_88"
    assert "pinned_at" in meta


def test_resolve_universe_tickers_via_name():
    spec = {"universe": {"name": "sp500_leaning_88", "benchmark": "SPY"}}
    tickers = _universe.resolve_universe_tickers(spec)
    assert len(tickers) == 87
    assert "AAPL" in tickers


def test_resolve_universe_tickers_via_inline_list():
    spec = {"universe": {"tickers": ["AAPL", "MSFT", "NVDA"], "benchmark": "SPY"}}
    tickers = _universe.resolve_universe_tickers(spec)
    assert tickers == ["AAPL", "MSFT", "NVDA"]


def test_resolve_universe_tickers_name_wins_when_both_present():
    """During the transition period, both fields may coexist temporarily.

    The registered ``name:`` is authoritative — this lets a refactor
    land the new field without forcing simultaneous removal of the
    inline list during a multi-step rollout.
    """
    spec = {
        "universe": {
            "name": "sp500_leaning_88",
            "tickers": ["AAPL"],
            "benchmark": "SPY",
        }
    }
    tickers = _universe.resolve_universe_tickers(spec)
    assert len(tickers) == 87  # name wins


def test_resolve_universe_tickers_missing_both_raises():
    spec = {"universe": {"benchmark": "SPY"}}
    with pytest.raises(_universe.UniverseError, match="missing both"):
        _universe.resolve_universe_tickers(spec)


def test_resolve_universe_tickers_missing_universe_block_raises():
    with pytest.raises(_universe.UniverseError, match="missing 'universe'"):
        _universe.resolve_universe_tickers({})


def test_resolve_universe_tickers_empty_inline_list_raises():
    spec = {"universe": {"tickers": [], "benchmark": "SPY"}}
    with pytest.raises(_universe.UniverseError, match="non-empty list"):
        _universe.resolve_universe_tickers(spec)


def test_get_universe_sp500_2026q2():
    """The S&P 500 snapshot loads cleanly at the expected scale."""
    tickers = _universe.get_universe("sp500_2026q2")
    assert isinstance(tickers, list)
    # S&P 500 includes a handful of dual-class shares (GOOG/GOOGL, FOX/FOXA,
    # NWS/NWSA, BRK-B + a few others), so the constituent count typically
    # sits in the 498-510 range.
    assert 498 <= len(tickers) <= 510, f"count {len(tickers)} outside expected range"
    assert "AAPL" in tickers
    assert "BRK-B" in tickers  # dual-class with yfinance hyphen-formatting
    assert "ON" in tickers  # YAML 1.1 bool-coercion guard (ON is a real S&P ticker)


def test_get_universe_yaml_bool_coercion_guard():
    """Universe files must protect against YAML 1.1 bool coercion.

    PyYAML's safe_load (YAML 1.1) parses unquoted ``ON``, ``OFF``, ``YES``,
    ``NO``, ``Y``, ``N`` as booleans. Universe YAMLs must quote tickers
    (or the loader must reject the file) so 'ON' does not become True.
    """
    # If sp500_2026q2 loaded successfully and "ON" is a string, the file is
    # correctly quoted. If the file regressed (someone removed the quotes),
    # the loader's non-string validation would have raised UniverseError
    # before this test ran — but assert the property here anyway as a
    # sentinel.
    tickers = _universe.get_universe("sp500_2026q2")
    on_entries = [t for t in tickers if t == "ON" or t is True]
    assert on_entries == ["ON"], f"expected ['ON'], got {on_entries}"


def test_sp500_leaning_88_byte_identical_to_inline_yaml():
    """Regression guard: the frozen snapshot must match the original
    inline universe.tickers list embedded in every strategy YAML.

    If this fails, either the snapshot was mutated (forbidden — publish
    a new universe instead) or one of the strategy YAMLs has drifted
    from the canonical list (also a bug — they should all be identical
    until the refactor switches them to universe.name).
    """
    import yaml as pyyaml
    from pathlib import Path

    strat_dir = Path(_universe.__file__).parent
    yamls = [
        "clenow_momentum.yml",
        "connors_rsi2.yml",
        "dual_ma_trend_following.yml",
        "ts_momentum.yml",
        "xs_low_volatility.yml",
        "xs_short_term_reversal.yml",
    ]
    snapshot = _universe.get_universe("sp500_leaning_88")
    for fname in yamls:
        p = strat_dir / fname
        if not p.is_file():
            continue
        spec = pyyaml.safe_load(p.read_text(encoding="utf-8"))
        inline = spec.get("universe", {}).get("tickers")
        if inline is None:
            # YAML has been migrated to universe.name — skip the inline
            # comparison; the loader's identity check above is the guarantee.
            continue
        assert inline == snapshot, (
            f"{fname} inline tickers drifted from sp500_leaning_88 snapshot"
        )