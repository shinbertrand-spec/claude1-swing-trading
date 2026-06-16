"""Tests for tools.auto_paper.entry_pricing — entry-limit split by setup class.

Regression guard for the 2026-06-16 no-fill bug: momentum entries must use a
marketable limit (fills at the open through the overnight gap, matching the
next-open backtest); reversion entries rest at the prior close (no chase).
"""
from __future__ import annotations

from tools.auto_paper import entry_pricing as ep


# ---------------------------------------------------------- entry_limit_price


def test_momentum_uses_3pct_marketable_limit():
    # task acceptance: ts_momentum(100) == 103.0
    assert ep.entry_limit_price("ts_momentum", 100) == 103.0


def test_reversion_rests_at_pivot():
    # task acceptance: xs_short_term_reversal(100) == 100.0
    assert ep.entry_limit_price("xs_short_term_reversal", 100) == 100.0


def test_all_momentum_kinds_chase_up():
    for kind in ("ts_momentum", "residual_momentum", "clenow_momentum",
                 "dual_ma_trend_following"):
        assert ep.entry_limit_price(kind, 200.0) == 206.0, kind


def test_all_reversion_kinds_no_chase():
    for kind in ("xs_short_term_reversal", "connors_rsi2", "xs_low_volatility"):
        assert ep.entry_limit_price(kind, 200.0) == 200.0, kind


def test_unknown_kind_is_no_chase_and_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="tools.auto_paper.entry_pricing"):
        out = ep.entry_limit_price("totally_unknown_kind", 100.0)
    assert out == 100.0           # conservative no-chase
    assert any("unknown kind" in r.message for r in caplog.records)


def test_rounds_to_cents():
    # 333.33 * 1.03 = 343.3299 → 343.33
    assert ep.entry_limit_price("ts_momentum", 333.33) == 343.33


# ---------------------------------------------------------------- resolve_kind


def test_resolve_kind_live_setups():
    """The 5 live deployables must route to their base KIND (authoritative:
    spec["kind"]). If this breaks, momentum silently falls to no-chase."""
    assert ep.resolve_kind("ts_momentum_liquid_us") == "ts_momentum"
    assert ep.resolve_kind("residual_momentum_liquid_us") == "residual_momentum"
    assert ep.resolve_kind("clenow_momentum_liquid_us") == "clenow_momentum"
    assert ep.resolve_kind("xs_short_term_reversal_liquid_us") == "xs_short_term_reversal"
    assert ep.resolve_kind("xs_short_term_reversal") == "xs_short_term_reversal"


def test_resolve_kind_then_price_momentum_end_to_end():
    """The full live routing: setup_type → kind → marketable limit."""
    for setup in ("ts_momentum_liquid_us", "residual_momentum_liquid_us",
                  "clenow_momentum_liquid_us"):
        kind = ep.resolve_kind(setup)
        assert ep.entry_limit_price(kind, 100.0) == 103.0, setup


def test_resolve_kind_suffix_fallback_for_unknown_spec():
    """When no spec file exists, strip a known universe suffix."""
    assert ep.resolve_kind("ts_momentum_ai_broad") == "ts_momentum"
    assert ep.resolve_kind("something_ai_pure") == "something"
