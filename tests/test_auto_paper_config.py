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
    # After the 2026-05-26 8y extension (lever A) + connors_rsi2 replenishment +
    # dual_ma simulator-concurrency-bug defuse, only xs_short_term_reversal and
    # connors_rsi2 survive in deployable:. dual_ma moved to
    # parked_by_simulator_concurrency_bug: because the simulator's _equity_curve
    # has no portfolio-level concurrent-position cap and the strategy lacks an
    # in-spec workaround (unlike xs_short_term_reversal's bottom_pct +
    # connors_rsi2's max_concurrent_positions). SEPA-VCP / EP / clenow are in
    # parked_by_tightened_gate:.
    assert "xs_short_term_reversal" in names
    assert "connors_rsi2" in names
    assert "dual_ma_trend_following" not in names  # parked by simulator concurrency bug
    assert "EP" not in names          # parked by tightened gate + 8y extension
    assert "SEPA-VCP" not in names    # parked by tightened gate
    assert "clenow_momentum" not in names  # parked by 8y extension (DD breach)
    assert "Pullback-20SMA" not in names


def test_is_deployable():
    assert is_deployable("xs_short_term_reversal") is True
    assert is_deployable("connors_rsi2") is True
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
