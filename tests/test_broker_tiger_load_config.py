"""Tests for tools.broker.tiger.load_config + the _mask helper.

Never hits the live Tiger API. Uses pytest tmp_path + monkeypatch to
construct credentials directories with synthetic .properties files, and
mocks `TigerOpenClientConfig` so we don't depend on the SDK reading a
real RSA key.
"""
from __future__ import annotations

import os

import pytest

from tools.broker import tiger
from tools.broker.tiger import (
    BrokerConfigError,
    CREDENTIALS_DIR_DEFAULT,
    PROPS_FILENAME,
    _mask,
    load_config,
)


def test_mask_short_value():
    assert _mask("ab") == "****"
    assert _mask("") == "****"
    assert _mask(None) == "****"


def test_mask_normal_value():
    assert _mask("PAPER12345678") == "...5678"
    assert _mask("1234") == "...1234"


def test_load_config_missing_dir(tmp_path):
    with pytest.raises(BrokerConfigError, match="not found"):
        load_config(str(tmp_path / "does-not-exist"))


def test_load_config_missing_props_file(tmp_path):
    with pytest.raises(BrokerConfigError, match=PROPS_FILENAME):
        load_config(str(tmp_path))


class _FakeCfg:
    """Stand-in for TigerOpenClientConfig — same attributes load_config uses."""
    def __init__(self, *, tiger_id="DEV12345678", account="PAPER87654321",
                 license="TBSG", is_paper=True,
                 server_url="https://openapi.tigerfintech.com/gateway"):
        self.tiger_id = tiger_id
        self.account = account
        self.license = license
        self.is_paper = is_paper
        self.server_url = server_url


def _write_props(tmp_path):
    """Write a syntactically valid (empty-ish) props file so the os.path.isfile
    guard passes. Actual parsing is mocked."""
    p = tmp_path / PROPS_FILENAME
    p.write_text("# stub\n")
    return tmp_path


def test_load_config_happy_path(tmp_path, monkeypatch):
    _write_props(tmp_path)
    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        lambda **kw: _FakeCfg(),
    )

    info = load_config(str(tmp_path))

    assert info["tiger_id_masked"] == "...5678"
    assert info["account_masked"] == "...4321"
    assert info["license"] == "TBSG"
    assert info["is_paper"] is True
    assert info["server_url"].startswith("https://")
    assert info["props_dir"] == str(tmp_path)
    # Full account + tiger_id must NOT leak into the returned dict.
    assert "DEV12345678" not in str(info)
    assert "PAPER87654321" not in str(info)


def test_load_config_missing_fields_rejected(tmp_path, monkeypatch):
    _write_props(tmp_path)
    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        lambda **kw: _FakeCfg(tiger_id="", account=""),
    )
    with pytest.raises(BrokerConfigError, match="tiger_id or account"):
        load_config(str(tmp_path))


def test_load_config_sdk_failure_wrapped(tmp_path, monkeypatch):
    _write_props(tmp_path)

    def _boom(**kw):
        raise RuntimeError("RSA key malformed")

    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        _boom,
    )
    with pytest.raises(BrokerConfigError, match="RSA key malformed"):
        load_config(str(tmp_path))


def test_load_config_uses_env_var(tmp_path, monkeypatch):
    _write_props(tmp_path)
    monkeypatch.setenv("TIGER_PROPS_DIR", str(tmp_path))
    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        lambda **kw: _FakeCfg(),
    )
    info = load_config()  # no arg — falls through to env
    assert info["props_dir"] == str(tmp_path)


def test_load_config_default_path_constant():
    # Sanity: the documented default lives outside the repo.
    assert CREDENTIALS_DIR_DEFAULT.startswith("C:/Users/")
    assert "Claude1" not in CREDENTIALS_DIR_DEFAULT
