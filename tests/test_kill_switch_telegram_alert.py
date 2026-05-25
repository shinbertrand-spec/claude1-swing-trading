"""Tests for tools.thematic_portfolio.kill_switch.telegram_alert.

Covers:
  * credentials resolution (env vars vs config files vs missing)
  * happy-path send returns ok=True with message_id
  * config_missing returns ok=False without raising
  * network error returns ok=False without raising
  * Telegram API error response returns ok=False
  * empty message rejected early
  * formatters produce expected Markdown shapes
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.thematic_portfolio.kill_switch import telegram_alert


# --- credentials resolution ---------------------------------------------


def test_resolve_credentials_env_vars_take_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from_file\n", encoding="utf-8")
    access_file = tmp_path / "access.json"
    access_file.write_text(json.dumps({"allowFrom": ["from_file_chat"]}), encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from_env")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "from_env_chat")
    token, chat_id = telegram_alert.resolve_credentials(
        env_path=env_file, access_path=access_file,
    )
    assert token == "from_env"
    assert chat_id == "from_env_chat"


def test_resolve_credentials_falls_back_to_config_files(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=abc123\n", encoding="utf-8")
    access_file = tmp_path / "access.json"
    access_file.write_text(
        json.dumps({"allowFrom": [98765432]}), encoding="utf-8",
    )

    token, chat_id = telegram_alert.resolve_credentials(
        env_path=env_file, access_path=access_file,
    )
    assert token == "abc123"
    assert chat_id == "98765432"


def test_resolve_credentials_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN="quoted_value"\n', encoding="utf-8")
    access_file = tmp_path / "access.json"
    access_file.write_text(json.dumps({"allowFrom": ["123"]}), encoding="utf-8")
    token, chat_id = telegram_alert.resolve_credentials(
        env_path=env_file, access_path=access_file,
    )
    assert token == "quoted_value"


def test_resolve_credentials_handles_dict_entries_in_access_json(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")
    access_file = tmp_path / "access.json"
    access_file.write_text(
        json.dumps({"allowFrom": [{"id": 555111}, "ignored"]}),
        encoding="utf-8",
    )
    _, chat_id = telegram_alert.resolve_credentials(
        env_path=env_file, access_path=access_file,
    )
    assert chat_id == "555111"


def test_resolve_credentials_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    token, chat_id = telegram_alert.resolve_credentials(
        env_path=tmp_path / "nonexistent.env",
        access_path=tmp_path / "nonexistent.json",
    )
    assert token is None
    assert chat_id is None


# --- send_alert ----------------------------------------------------------


def test_send_alert_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    captured = {}

    def fake_post(*, url, body, timeout):
        captured["url"] = url
        captured["body"] = json.loads(body.decode("utf-8"))
        captured["timeout"] = timeout
        return {"ok": True, "result": {"message_id": 42}}

    r = telegram_alert.send_alert("hi", _post_fn=fake_post)
    assert r.ok is True
    assert r.message_id == 42
    assert r.chat_id == "c"
    assert "bott/sendMessage" in captured["url"] or "/bot" in captured["url"]
    assert captured["body"]["chat_id"] == "c"
    assert captured["body"]["text"] == "hi"
    assert captured["body"]["parse_mode"] == "Markdown"


def test_send_alert_missing_token_returns_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    r = telegram_alert.send_alert(
        "hi", env_path=tmp_path / "none.env",
        access_path=tmp_path / "none.json",
        _post_fn=lambda **_: None,  # never called
    )
    assert r.ok is False
    assert r.error == "config_missing_token"


def test_send_alert_missing_chat_id_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    r = telegram_alert.send_alert(
        "hi", env_path=tmp_path / "none.env",
        access_path=tmp_path / "none.json",
        _post_fn=lambda **_: None,
    )
    assert r.ok is False
    assert r.error == "config_missing_chat_id"


def test_send_alert_empty_message_returns_error():
    r = telegram_alert.send_alert("")
    assert r.ok is False
    assert r.error == "empty_message"

    r = telegram_alert.send_alert("   ")
    assert r.ok is False
    assert r.error == "empty_message"


def test_send_alert_post_exception_does_not_raise(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(**_):
        raise RuntimeError("DNS_FAIL")

    r = telegram_alert.send_alert("hi", _post_fn=boom)
    assert r.ok is False
    assert "post_failed" in r.error
    assert "DNS_FAIL" in r.error
    assert r.chat_id == "c"


def test_send_alert_telegram_api_error_returns_ok_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def fake_post(**_):
        return {"ok": False, "description": "Bad Request: chat not found"}

    r = telegram_alert.send_alert("hi", _post_fn=fake_post)
    assert r.ok is False
    assert "telegram_api_error" in r.error
    assert "chat not found" in r.error


# --- formatters ---------------------------------------------------------


def test_format_tier_fire_includes_severity_and_sell_fraction():
    msg = telegram_alert.format_tier_fire(
        tier=3, action="unwind", drawdown_pct=0.5,
        current_allocation_pct=0.25, sell_fraction=1.0,
        thematic_symbols=["NVDA", "BE"],
        aschenbrenner_override=True, cycle_id="cycle-abc", dry_run=False,
    )
    assert "TIER 3 URGENT" in msg
    assert "50.0%" in msg          # drawdown
    assert "25.0%" in msg          # alloc
    assert "100.0%" in msg         # sell_fraction
    assert "NVDA, BE" in msg
    assert "Aschenbrenner" in msg  # override note
    assert "cycle-abc" in msg


def test_format_tier_fire_marks_dry_run():
    msg = telegram_alert.format_tier_fire(
        tier=1, action="deleverage", drawdown_pct=0.2,
        current_allocation_pct=0.2, sell_fraction=0.125,
        thematic_symbols=["NVDA"], aschenbrenner_override=False,
        cycle_id="c1", dry_run=True,
    )
    assert msg.startswith("[DRY-RUN]")
    assert "TIER 1 WARNING" in msg


def test_format_unprotected_lists_symbols():
    msg = telegram_alert.format_unprotected(
        symbols=["NVDA", "BE"], cycle_id="cyc-001",
    )
    assert "UNPROTECTED" in msg
    assert "NVDA, BE" in msg
    assert "cyc-001" in msg


def test_format_b_silent_no_prior_heartbeat():
    msg = telegram_alert.format_b_silent(
        last_cycle_at=None, minutes_silent=99.5,
    )
    assert "SILENT" in msg
    assert "99.5" in msg
    assert "never" in msg.lower()


def test_format_b_recovered():
    msg = telegram_alert.format_b_recovered(
        last_cycle_at="2026-05-26T10:05:00+00:00", silent_minutes=22.0,
    )
    assert "RECOVERED" in msg
    assert "22.0" in msg
