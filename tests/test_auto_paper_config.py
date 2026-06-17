"""Tests for tools.auto_paper.config — deployable-setups loader."""
from __future__ import annotations

import pytest
import yaml

from tools.auto_paper.config import (
    DeployableConfigError,
    deployable_setup_names,
    is_deployable,
    load,
)


def _write(tmp_path, data):
    p = tmp_path / "ds.yml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_load_default_path():
    """Default file at tools/deployable_setups.yml — must exist + parse."""
    data = load()
    assert isinstance(data, dict)
    assert "deployable" in data


def test_load_missing_file(tmp_path):
    with pytest.raises(DeployableConfigError, match="not found"):
        load(str(tmp_path / "missing.yml"))


def test_load_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("not: :: yaml: :::")
    with pytest.raises(DeployableConfigError, match="YAML parse error"):
        load(str(p))


def test_load_non_mapping(tmp_path):
    p = tmp_path / "list.yml"
    p.write_text("- just-a-list\n- not-a-mapping\n")
    with pytest.raises(DeployableConfigError, match="must be a mapping"):
        load(str(p))


def test_deployable_setup_names_extracts():
    names = deployable_setup_names()
    # NET-OF-COST GATE RETIREMENT (2026-06-17): the hardened portfolio-sim gate
    # (net-of-cost + OHLC fills + cap-weight) retired 4 of the 5 live generic
    # setups — residual/clenow/both xs_short_term_reversal variants fail at net
    # OOS Sharpe 0.12-0.52 (filled fwd-returns ~0.2-0.9% vs missed 4-8% =
    # adverse selection). Only ts_momentum_liquid_us survives (net OOS Sharpe
    # 1.04). connors_rsi2 already parked 2026-06-09. See scripts/net_gate_rerun.py.
    assert "ts_momentum_liquid_us" in names   # ONLY net-of-cost survivor
    assert "xs_short_term_reversal" not in names         # RETIRED 2026-06-17 (net OOS 0.52)
    assert "xs_short_term_reversal_liquid_us" not in names  # RETIRED 2026-06-17 (net OOS 0.14)
    assert "clenow_momentum_liquid_us" not in names     # RETIRED 2026-06-17 (net OOS 0.12)
    assert "residual_momentum_liquid_us" not in names   # RETIRED 2026-06-17 (net OOS 0.26)
    assert "connors_rsi2" not in names  # PARKED 2026-06-09 — fails gate under concurrency cap
    assert "dual_ma_trend_following" not in names  # parked by simulator concurrency bug
    assert "EP" not in names          # parked by tightened gate + 8y extension
    assert "SEPA-VCP" not in names    # parked by tightened gate
    assert "clenow_momentum" not in names  # parked by 8y extension (DD breach)
    assert "Pullback-20SMA" not in names
    # HOLD gate (2026-06-05): the 3 ai_thematic rows carry hold: true and are
    # excluded from the live scan until the v2 eval clears + plan approval.
    assert "xs_short_term_reversal_ai_pure" not in names
    assert "xs_short_term_reversal_ai_broad" not in names
    assert "connors_rsi2_ai_broad" not in names


def test_is_deployable():
    assert is_deployable("ts_momentum_liquid_us") is True  # only net-of-cost survivor (2026-06-17)
    assert is_deployable("xs_short_term_reversal") is False  # RETIRED 2026-06-17 — fails net-of-cost gate
    assert is_deployable("clenow_momentum_liquid_us") is False  # RETIRED 2026-06-17 — fails net-of-cost gate
    assert is_deployable("residual_momentum_liquid_us") is False  # RETIRED 2026-06-17 — fails net-of-cost gate
    assert is_deployable("connors_rsi2") is False  # PARKED 2026-06-09 — stale pre-cap verdict, fails gate at Sharpe 0.59
    assert is_deployable("dual_ma_trend_following") is False  # parked 2026-05-26 by simulator concurrency bug
    assert is_deployable("EP") is False           # parked 2026-05-26 by 8y extension
    assert is_deployable("SEPA-VCP") is False     # parked 2026-05-26 by tightened gate
    assert is_deployable("clenow_momentum") is False  # parked 2026-05-26 by 8y extension
    assert is_deployable("Pullback-20SMA") is False
    assert is_deployable("Unknown-Setup") is False


def test_deployable_setup_names_custom_path(tmp_path):
    custom = _write(tmp_path, {
        "deployable": [{"setup": "Custom-1"}, {"setup": "Custom-2"}],
    })
    assert deployable_setup_names(custom) == {"Custom-1", "Custom-2"}


def test_empty_deployable_list(tmp_path):
    custom = _write(tmp_path, {"deployable": []})
    assert deployable_setup_names(custom) == set()


def test_missing_deployable_key(tmp_path):
    custom = _write(tmp_path, {"parked": [{"setup": "Foo"}]})
    assert deployable_setup_names(custom) == set()


def test_malformed_row_ignored(tmp_path):
    """Rows missing the `setup` key should be silently skipped, not crash."""
    custom = _write(tmp_path, {
        "deployable": [
            {"setup": "Good"},
            {"variant": "no-setup-key"},  # malformed
            "string-not-dict",            # malformed
            {"setup": "Also-Good"},
        ],
    })
    assert deployable_setup_names(custom) == {"Good", "Also-Good"}


def test_hold_gate_excludes_held_rows(tmp_path):
    """HOLD gate (2026-06-05): a row with hold: true is backtest-cleared but
    NOT live-approved, so it is excluded from the live scan. hold absent or
    false = live (back-compat)."""
    custom = _write(tmp_path, {
        "deployable": [
            {"setup": "Live-Default"},                 # hold absent → live
            {"setup": "Live-Explicit", "hold": False}, # hold false → live
            {"setup": "Held", "hold": True},           # hold true → excluded
        ],
    })
    names = deployable_setup_names(custom)
    assert names == {"Live-Default", "Live-Explicit"}
    assert is_deployable("Held", custom) is False
    assert is_deployable("Live-Default", custom) is True
