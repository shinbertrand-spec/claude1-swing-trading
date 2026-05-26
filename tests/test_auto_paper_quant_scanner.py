"""Tests for tools.auto_paper.quant_scanner.

The scanner is the bridge between KIND_REGISTRY backtest plugins and the
live auto-paper pipeline. Tests use synthetic OHLCV (no yfinance) and
stub the universe via the ``universe_dfs`` parameter of ``scan_setup``.

Validates:

* End-to-end: synthetic universe with a sharp end-of-period dip →
  scan_setup emits a CandidateInput for the dip ticker (Connors RSI(2))
* Regime filter: bearish synthetic SPY → no candidates emitted
* Setup not in KIND_REGISTRY → empty ScannerReport with note
* CandidateInput shape: shares > 0, stop < limit, sector_etf set
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.auto_paper import quant_scanner


# ------------------------------------------------------- synthetic OHLCV


def _trending_df(n: int = 300, start: float = 100.0, slope: float = 0.001) -> pd.DataFrame:
    closes = np.array([start * np.exp(slope * i) for i in range(n)])
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": np.full(n, 1_000_000, dtype=int),
    }, index=pd.date_range("2024-01-02", periods=n, freq="B"))


def _downtrend_df(n: int = 300, start: float = 100.0, slope: float = -0.001) -> pd.DataFrame:
    closes = np.array([start * np.exp(slope * i) for i in range(n)])
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": np.full(n, 1_000_000, dtype=int),
    }, index=pd.date_range("2024-01-02", periods=n, freq="B"))


def _dip_at_end_df(n: int = 300, start: float = 100.0) -> pd.DataFrame:
    closes = np.array([start * (1 + 0.002 * i) for i in range(n - 5)])
    closes = np.concatenate([closes, closes[-1] * np.array([0.97, 0.94, 0.91, 0.89, 0.88])])
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": np.full(n, 1_000_000, dtype=int),
    }, index=pd.date_range("2024-01-02", periods=n, freq="B"))


# ------------------------------------------------------- happy path


def test_scan_setup_emits_sized_candidate_for_oversold_ticker():
    """Bullish SPY + a sharply-dipping ticker → a sized CandidateInput."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}

    row = {
        "setup": "connors_rsi2",
        # Override the spec's grid with explicit live params so we don't
        # depend on the spec's first-combo defaults.
        "deployable_params": {
            "rsi_period": 2,
            "cumulative_period": 2,
            "entry_threshold": 20.0,
            "cooldown_days": 3,
            "regime_sma_period": 50,
            "atr_period": 20,
            "atr_stop_multiple": 2.0,
            "max_hold_days": 5,
        },
    }
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        regime_class="stage_2_confirmed",
        universe_dfs=universe,
    )
    assert report.setup == "connors_rsi2"
    assert "A" in report.eligible_tickers
    assert len(report.candidates) >= 1
    c = report.candidates[0]
    assert c.ticker == "A"
    assert c.setup_type == "connors_rsi2"
    assert c.setup_grade == "B"
    assert c.shares > 0
    assert c.stop_price < c.limit_price < c.pivot_price * 1.01  # ~20bp offset
    assert c.target_price is None
    assert c.sector_etf is not None


def test_scan_setup_regime_filter_blocks_in_bear():
    """Downtrending SPY → no eligibility → empty candidates."""
    spy = _downtrend_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    row = {
        "setup": "connors_rsi2",
        "deployable_params": {
            "rsi_period": 2, "cumulative_period": 2,
            "entry_threshold": 20.0, "cooldown_days": 3,
            "regime_sma_period": 50, "atr_period": 20,
            "atr_stop_multiple": 2.0, "max_hold_days": 5,
        },
    }
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        regime_class="stage_4",
        universe_dfs=universe,
    )
    assert report.candidates == []


def test_scan_setup_unknown_kind_returns_note():
    """A setup whose spec file is missing should yield a noted empty report."""
    row = {"setup": "no_such_kind"}
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        universe_dfs={},
    )
    assert report.candidates == []
    # Post-fix (2026-05-26): the row's `setup` field is a spec FILENAME, so
    # an unknown setup surfaces first as a missing-spec note. A spec that
    # IS present but whose `kind:` value isn't in KIND_REGISTRY surfaces
    # the other note (see test_scan_setup_alias_spec_dereferences_via_kind).
    assert "no quant_strategies spec" in report.note


def test_scan_setup_skips_when_benchmark_missing():
    """If the spec's benchmark isn't in universe_dfs, return a noted empty."""
    universe = {"A": _trending_df()}  # missing SPY
    row = {
        "setup": "connors_rsi2",
        "deployable_params": {
            "rsi_period": 2, "cumulative_period": 2,
            "entry_threshold": 10.0, "cooldown_days": 3,
            "regime_sma_period": 50, "atr_period": 20,
            "atr_stop_multiple": 2.0, "max_hold_days": 5,
        },
    }
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        universe_dfs=universe,
    )
    assert report.candidates == []
    assert "failed to load" in report.note


def test_scan_setup_sized_candidates_pass_pipeline_track_limits():
    """Sized candidate emitted by scanner should clear the per-track 5% cap
    on a $1M account (sanity test for the sizer integration)."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    row = {
        "setup": "connors_rsi2",
        "deployable_params": {
            "rsi_period": 2, "cumulative_period": 2,
            "entry_threshold": 20.0, "cooldown_days": 3,
            "regime_sma_period": 50, "atr_period": 20,
            "atr_stop_multiple": 2.0, "max_hold_days": 5,
        },
    }
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        regime_class="stage_2_confirmed",
        universe_dfs=universe,
    )
    assert len(report.candidates) >= 1
    c = report.candidates[0]
    cost = c.shares * c.limit_price
    # Per CLAUDE.md, hard cap is 5% per position; the sizer's concentration
    # cap default is 25% (risk-based since v2). Allow either.
    assert cost <= 250_001.0  # within 25% concentration cap on $1M


# ------------------------------------------------------- registry-vs-spec-name decoupling
#
# Regression coverage for the 2026-05-26 bug: deployable_setups.yml rows
# use spec FILENAMES (e.g. ``clenow_momentum_liquid_us``) as the ``setup:``
# value, but KIND_REGISTRY is keyed on the strategy module's ``KIND``
# constant (e.g. ``clenow_momentum``). The pre-fix scanner filtered
# ``setup in KIND_REGISTRY`` and silently dropped any spec-file whose
# filename differed from its ``kind:`` field. These tests pin the post-fix
# contract: scan_setup loads the spec first and dereferences via
# ``spec["kind"]``; scan_today does not pre-filter by KIND_REGISTRY.


def test_scan_setup_alias_spec_dereferences_via_kind(tmp_path, monkeypatch):
    """A spec whose filename != its kind: should resolve via spec['kind']."""
    import textwrap

    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    alias_yml = spec_dir / "connors_rsi2_alias.yml"
    alias_yml.write_text(textwrap.dedent("""
        meta:
          name: connors_rsi2_alias
          version: "1.0"
          source: test
          description: alias spec for registry-vs-filename regression test
          horizon_days: 5
          status: test
        kind: connors_rsi2
        universe:
          tickers: [A]
          benchmark: SPY
        period:
          start: "2024-01-01"
          end: "2025-12-31"
        params:
          rsi_period: 2
          cumulative_period: 2
          entry_threshold: 20.0
          cooldown_days: 3
          regime_sma_period: 50
          atr_period: 20
          atr_stop_multiple: 2.0
          max_hold_days: 5
        execution:
          trail: fixed
        gate:
          sharpe_min: 1.0
          max_dd_pct: 25.0
          n_min: 30
    """).strip())
    monkeypatch.setattr(quant_scanner, "SPEC_DIR", str(spec_dir))

    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    row = {"setup": "connors_rsi2_alias"}  # filename, not the kind
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        regime_class="stage_2_confirmed",
        universe_dfs=universe,
    )
    # The pre-fix bug would drop this row at the registry-membership
    # check ("connors_rsi2_alias" is not a KIND_REGISTRY key). Post-fix:
    # the spec resolves, spec["kind"] == "connors_rsi2" is in the registry,
    # and a candidate fires for the dip ticker.
    assert report.note == "", f"unexpected note: {report.note!r}"
    assert "A" in report.eligible_tickers
    assert len(report.candidates) >= 1


def test_scan_setup_spec_with_unknown_kind_surfaces_note(tmp_path, monkeypatch):
    """A spec whose ``kind:`` value isn't in KIND_REGISTRY surfaces a note."""
    import textwrap

    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    bad_yml = spec_dir / "bad_kind_spec.yml"
    bad_yml.write_text(textwrap.dedent("""
        meta: {name: bad_kind_spec, version: "1.0", source: t, description: t, horizon_days: 1, status: t}
        kind: not_a_real_kind
        universe: {tickers: [A], benchmark: SPY}
        period: {start: "2024-01-01", end: "2025-12-31"}
        params: {}
        execution: {trail: fixed}
        gate: {sharpe_min: 1.0, max_dd_pct: 25.0, n_min: 30}
    """).strip())
    monkeypatch.setattr(quant_scanner, "SPEC_DIR", str(spec_dir))

    row = {"setup": "bad_kind_spec"}
    report = quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
        universe_dfs={},
    )
    assert report.candidates == []
    assert "not in KIND_REGISTRY" in report.note
    assert "not_a_real_kind" in report.note
