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
    # Per CLAUDE.md, hard cap is 5% per position. The scanner passes
    # concentration_cap_pct=0.05 explicitly to tools.position_sizer so the
    # binding constraint is min(risk_budget, 5%_cap) and the pipeline's
    # after-the-fact track-rule check never has cause to reject a sized
    # candidate. Pre-2026-05-28 the assertion permitted up to 25% — which
    # encoded the bug where today's smoke-test produced 10 candidates
    # at 7-11% of net liq, ALL rejected by the pipeline track cap.
    assert cost <= 50_001.0, (
        f"candidate {c.ticker} cost ${cost:,.0f} exceeds 5% cap "
        f"on $1M (would be rejected by paper-auto track rules)"
    )


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


# ------------------------------------------------------- rebalance-schedule anchor (Bug 2)
#
# Regression coverage for the 2026-05-26 evening fix: the LIVE scanner
# was loading only a 400-day moving window of OHLCV, so each kind's
# precompute() computed rebalance_dates = range(start_idx, n, step) over
# bench_dates whose [0] slid daily. The backtest anchors at
# spec.period.start; the live scanner must match that anchor so the
# rebalance schedule stays calendar-stable across runs.


def test_refresh_universe_start_date_overrides_lookback(monkeypatch):
    """When start_date is passed, it wins over lookback_days."""
    import datetime as _dt
    calls = []

    # Disable staleness auto-refetch so we only test the start-date path.
    monkeypatch.setattr(quant_scanner, "_benchmark_cache_age_hours", lambda _b="SPY": 0.0)

    def _fake_fetch(ticker, *, start, end, force_refetch=False):
        calls.append((ticker, start, end))

    import pandas as _pd
    def _fake_load(ticker):
        return _pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                              "Close": [1.0], "Volume": [1]},
                             index=_pd.date_range("2024-01-02", periods=1, freq="B"))

    monkeypatch.setattr(quant_scanner.data_cache, "fetch", _fake_fetch)
    monkeypatch.setattr(quant_scanner.data_cache, "load", _fake_load)

    fixed_start = _dt.date(2017, 1, 1)
    quant_scanner._refresh_universe(
        ["A", "B"],
        start_date=fixed_start,
        lookback_days=400,  # should be ignored
    )
    assert len(calls) == 2
    for _t, start, _end in calls:
        assert start == fixed_start, (
            f"start_date should win over lookback_days; got start={start}"
        )


# ------------------------------------------------------- cache staleness auto-refetch (2026-05-28)
#
# Regression coverage for the 2026-05-28 smoke-test finding: the SPY cache
# was 5 days stale (last bar 2026-05-22 on 2026-05-27) and the scanner used
# it silently. data_cache.fetch returns early on cache hit without checking
# fetched_at, so quant_scanner._refresh_universe must perform its own age
# check via the benchmark sidecar and promote force_refetch when stale.


def test_refresh_universe_stale_benchmark_cache_triggers_force_refetch(monkeypatch):
    """Stale benchmark cache → _refresh_universe promotes force_refetch=True."""
    import datetime as _dt
    import pandas as _pd
    fetch_calls = []

    def _fake_fetch(ticker, *, start, end, force_refetch=False):
        fetch_calls.append({"ticker": ticker, "force_refetch": force_refetch})

    def _fake_load(ticker):
        return _pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                              "Close": [1.0], "Volume": [1]},
                             index=_pd.date_range("2024-01-02", periods=1, freq="B"))

    # 30 hours stale → exceeds default 18h threshold → should force-refetch.
    monkeypatch.setattr(quant_scanner, "_benchmark_cache_age_hours",
                        lambda _b="SPY": 30.0)
    monkeypatch.setattr(quant_scanner.data_cache, "fetch", _fake_fetch)
    monkeypatch.setattr(quant_scanner.data_cache, "load", _fake_load)

    quant_scanner._refresh_universe(["A", "B"], start_date=_dt.date(2017, 1, 1))

    assert len(fetch_calls) == 2
    for call in fetch_calls:
        assert call["force_refetch"] is True, (
            f"stale benchmark cache should force-refetch all tickers; "
            f"got force_refetch={call['force_refetch']} for {call['ticker']}"
        )


def test_refresh_universe_fresh_benchmark_cache_skips_force_refetch(monkeypatch):
    """Fresh benchmark cache → _refresh_universe keeps force_refetch=False."""
    import datetime as _dt
    import pandas as _pd
    fetch_calls = []

    def _fake_fetch(ticker, *, start, end, force_refetch=False):
        fetch_calls.append({"ticker": ticker, "force_refetch": force_refetch})

    def _fake_load(ticker):
        return _pd.DataFrame({"Open": [1.0]}, index=_pd.date_range("2024-01-02", periods=1, freq="B"))

    # 2 hours fresh → well inside 18h threshold → no auto-refetch.
    monkeypatch.setattr(quant_scanner, "_benchmark_cache_age_hours",
                        lambda _b="SPY": 2.0)
    monkeypatch.setattr(quant_scanner.data_cache, "fetch", _fake_fetch)
    monkeypatch.setattr(quant_scanner.data_cache, "load", _fake_load)

    quant_scanner._refresh_universe(["A"], start_date=_dt.date(2017, 1, 1))

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["force_refetch"] is False


def test_refresh_universe_missing_benchmark_cache_triggers_force_refetch(monkeypatch):
    """No benchmark cache yet → treat as stale → force-refetch."""
    import datetime as _dt
    import pandas as _pd
    fetch_calls = []

    def _fake_fetch(ticker, *, start, end, force_refetch=False):
        fetch_calls.append({"ticker": ticker, "force_refetch": force_refetch})

    def _fake_load(ticker):
        return _pd.DataFrame({"Open": [1.0]}, index=_pd.date_range("2024-01-02", periods=1, freq="B"))

    monkeypatch.setattr(quant_scanner, "_benchmark_cache_age_hours",
                        lambda _b="SPY": None)
    monkeypatch.setattr(quant_scanner.data_cache, "fetch", _fake_fetch)
    monkeypatch.setattr(quant_scanner.data_cache, "load", _fake_load)

    quant_scanner._refresh_universe(["A"], start_date=_dt.date(2017, 1, 1))

    assert fetch_calls[0]["force_refetch"] is True


def test_refresh_universe_inf_max_age_disables_auto_refetch(monkeypatch):
    """cache_max_age_hours=inf bypasses the staleness check entirely."""
    import datetime as _dt
    import pandas as _pd
    fetch_calls = []

    def _fake_fetch(ticker, *, start, end, force_refetch=False):
        fetch_calls.append({"ticker": ticker, "force_refetch": force_refetch})

    def _fake_load(ticker):
        return _pd.DataFrame({"Open": [1.0]}, index=_pd.date_range("2024-01-02", periods=1, freq="B"))

    # Even with very stale cache, inf max_age must NOT promote force_refetch.
    monkeypatch.setattr(quant_scanner, "_benchmark_cache_age_hours",
                        lambda _b="SPY": 1_000_000.0)
    monkeypatch.setattr(quant_scanner.data_cache, "fetch", _fake_fetch)
    monkeypatch.setattr(quant_scanner.data_cache, "load", _fake_load)

    quant_scanner._refresh_universe(
        ["A"], start_date=_dt.date(2017, 1, 1),
        cache_max_age_hours=float("inf"),
    )

    assert fetch_calls[0]["force_refetch"] is False


def test_scan_setup_passes_spec_period_start_to_refresh_universe(tmp_path, monkeypatch):
    """scan_setup must extract spec.period.start and pass it as start_date so
    the live rebalance schedule matches the backtest anchor (Bug 2)."""
    import datetime as _dt
    import textwrap

    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    yml = spec_dir / "bug2_spec.yml"
    yml.write_text(textwrap.dedent("""
        meta: {name: bug2_spec, version: "1.0", source: t, description: t, horizon_days: 5, status: test}
        kind: connors_rsi2
        universe: {tickers: [A], benchmark: SPY}
        period: {start: "2017-01-01", end: "2026-05-25"}
        params:
          rsi_period: 2
          cumulative_period: 2
          entry_threshold: 20.0
          cooldown_days: 3
          regime_sma_period: 50
          atr_period: 20
          atr_stop_multiple: 2.0
          max_hold_days: 5
        execution: {trail: fixed}
        gate: {sharpe_min: 1.0, max_dd_pct: 25.0, n_min: 30}
    """).strip())
    monkeypatch.setattr(quant_scanner, "SPEC_DIR", str(spec_dir))

    seen_start_dates: list = []

    def _fake_refresh(tickers, *, start_date=None, lookback_days=400, force_refetch=False):
        seen_start_dates.append(start_date)
        # Return empty universe — scan_setup will note benchmark-load failure,
        # but the start_date capture is what we're testing.
        return {}

    monkeypatch.setattr(quant_scanner, "_refresh_universe", _fake_refresh)

    row = {"setup": "bug2_spec"}
    quant_scanner.scan_setup(
        row,
        account_net_liq=1_000_000.0,
    )
    assert seen_start_dates == [_dt.date(2017, 1, 1)], (
        f"scan_setup should pass spec.period.start to _refresh_universe; "
        f"got {seen_start_dates}"
    )


def test_scan_today_skips_held_rows(tmp_path, monkeypatch):
    """Parked rows (``hold: true``) must not be scanned.

    Regression for the 2026-06-15 leak: ``connors_rsi2`` was parked
    2026-06-09 (``hold: true``) but ``scan_today`` iterated EVERY deployable
    row, so parked-strategy candidates reached the critic panel only to be
    deferred — wasting critic spend and masking the live-scan picture. The
    scanner now mirrors ``config.deployable_setup_names()``'s HOLD gate.
    """
    import textwrap

    dep = tmp_path / "deployable_setups.yml"
    dep.write_text(
        textwrap.dedent(
            """
            deployable:
              - setup: live_setup
                track: generic
              - setup: parked_setup
                track: generic
                hold: true
            """
        ).strip(),
        encoding="utf-8",
    )

    scanned: list[str] = []

    def _fake_scan_setup(row, **kwargs):
        scanned.append(row["setup"])
        return quant_scanner.ScannerReport(
            setup=row["setup"], spec_path="", eligible_tickers=[],
            candidates=[], signal_date=None, note="stub",
        )

    monkeypatch.setattr(quant_scanner, "scan_setup", _fake_scan_setup)

    reports = quant_scanner.scan_today(
        account_net_liq=100_000.0,
        regime_class="stage_2_confirmed",
        deployable_path=str(dep),
    )

    assert scanned == ["live_setup"]
    assert "parked_setup" not in scanned
    assert [r.setup for r in reports] == ["live_setup"]
