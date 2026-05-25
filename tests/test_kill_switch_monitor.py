"""Tests for tools.thematic_portfolio.kill_switch.monitor — Process B cycle
+ run_forever (dry-run only)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.broker.tiger import BrokerOrderError, TigerClient
from tools.thematic_portfolio.kill_switch import monitor, state
from tools.thematic_portfolio.kill_switch.clock import SessionState


# --- fake TradeClient -----------------------------------------------------


class FakeTradeClient:
    """Mimics tigeropen TradeClient just enough for TigerClient + monitor.cycle."""

    def __init__(
        self,
        *,
        net_liquidation=1_000_000.0,
        positions: list[dict[str, Any]] | None = None,
        raise_on_positions=False,
    ):
        self.account_full = "PAPER12345"
        self.config_info = {
            "tiger_id_masked": "...1234",
            "account_masked": "...2345",
            "license": "TBSG",
            "is_paper": True,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self._net_liq = net_liquidation
        self._positions = positions or []
        self._raise_on_positions = raise_on_positions

    def get_assets(self, *, account, segment=False):
        summary = SimpleNamespace(
            cash=self._net_liq * 0.5,
            available_funds=self._net_liq * 0.5,
            buying_power=self._net_liq * 2.0,
            net_liquidation=self._net_liq,
            gross_position_value=self._net_liq * 0.5,
            currency="USD",
        )
        return [SimpleNamespace(summary=summary)]

    def get_positions(self, *, account):
        if self._raise_on_positions:
            raise RuntimeError("BOOM")
        out = []
        for p in self._positions:
            contract = SimpleNamespace(symbol=p["symbol"])
            out.append(SimpleNamespace(
                contract=contract,
                quantity=p["quantity"],
                average_cost=p.get("average_cost", 100.0),
                market_value=p.get("market_value", 0.0),
                unrealized_pnl=p.get("unrealized_pnl", 0.0),
            ))
        return out


def _client(**kw):
    return TigerClient(_trade_client=FakeTradeClient(**kw))


def _write_thematic_index(path: Path, tickers: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "positions": [{"ticker": t, "shares": 100} for t in tickers],
    }), encoding="utf-8")


# --- happy-path single cycle ----------------------------------------------


def test_cycle_with_no_thematic_positions_writes_hold_event(tmp_path):
    index_path = tmp_path / "thematic_index.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, [])

    tiger = _client(positions=[
        # Tiger has AAPL but thematic index is empty -> no thematic positions
        {"symbol": "AAPL", "quantity": 50, "market_value": 8_000.0},
    ])

    result = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert result.ok is True
    assert result.decision.action == "hold"
    assert result.thematic_market_value == 0.0
    assert result.thematic_symbols == []

    events = state.read_events(state_dir=state_dir)
    assert len(events) == 1
    assert events[0]["action"] == "hold"
    assert events[0]["dry_run"] is True

    hb = state.load_heartbeat(state_dir=state_dir)
    assert hb.cycle_number == 1
    assert hb.last_action == "hold"


def test_cycle_at_peak_no_drawdown(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA", "BE"])
    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 100, "market_value": 150_000.0,
         "average_cost": 1000.0},
        {"symbol": "BE",   "quantity": 1000, "market_value": 100_000.0,
         "average_cost": 80.0},
    ])
    result = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert result.ok is True
    assert result.thematic_market_value == 250_000.0
    assert result.peak_thematic_value == 250_000.0
    assert result.decision.action == "hold"


# --- tier transitions in the loop -----------------------------------------


def test_two_cycles_peak_then_drawdown_fires_tier1(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])

    # Cycle 1: at peak, 25% allocation, no drawdown
    tiger_peak = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r1 = monitor.cycle(
        tiger=tiger_peak, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r1.decision.action == "hold"
    assert r1.peak_thematic_value == 250_000.0

    # Cycle 2: -20% drawdown, 20% allocation
    tiger_dd = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 200_000.0},
    ])
    r2 = monitor.cycle(
        tiger=tiger_dd, state_dir=state_dir, index_path=index_path,
        cycle_number=2, dry_run=True,
    )
    assert r2.decision.action == "deleverage"
    assert r2.decision.tier == 1
    assert r2.peak_thematic_value == 250_000.0  # peak preserved

    events = state.read_events(state_dir=state_dir)
    assert len(events) == 2
    assert events[1]["tier"] == 1
    # Dry-run warning surfaced
    assert any("DRY-RUN" in w for w in events[1]["warnings"])


def test_aschenbrenner_kill_event_flag_fires_tier3_unwind(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])

    # Set the flag externally
    state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we exited",
        state_dir=state_dir,
    )

    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r.aschenbrenner_kill_event is True
    assert r.decision.action == "unwind"
    assert r.decision.tier == 3
    assert r.decision.aschenbrenner_override is True


# --- dry-run gate ---------------------------------------------------------


def test_no_dry_run_raises_on_non_hold(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    # Seed a peak so we get a drawdown
    state.update_peak(250_000.0, state_dir=state_dir)
    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 125_000.0},
    ])
    with pytest.raises(NotImplementedError, match="Session 2"):
        monitor.cycle(
            tiger=tiger, state_dir=state_dir, index_path=index_path,
            cycle_number=1, dry_run=False,
        )


def test_no_dry_run_with_hold_decision_does_not_raise(tmp_path):
    # Hold decisions should be safe to invoke under no-dry-run.
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=False,
    )
    assert r.decision.action == "hold"
    # No error, event still appended
    events = state.read_events(state_dir=state_dir)
    assert events[0]["dry_run"] is False


# --- error paths ---------------------------------------------------------


def test_cycle_returns_error_on_tiger_read_failure(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(raise_on_positions=True)
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r.ok is False
    assert "tiger_read_failed" in r.error
    # No event log entry because we bailed before write
    assert state.read_events(state_dir=state_dir) == []


# --- run_forever ---------------------------------------------------------


def test_run_forever_max_cycles_exits_after_n(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, [])
    tiger = _client(positions=[])

    sleeps: list[int] = []
    def fake_sleep(s):
        sleeps.append(s)

    fake_clock_state = SessionState(
        is_rth_open=True,
        reason="open",
        now_et_iso="2026-05-26T10:00:00-04:00",
        suggested_sleep_seconds=60,
    )
    n = monitor.run_forever(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        dry_run=True, max_cycles=3,
        sleep_fn=fake_sleep, clock_fn=lambda: fake_clock_state,
    )
    assert n == 3
    # 3 cycles -> 3 sleeps (1 per cycle)
    assert len(sleeps) == 3
    assert all(s == 60 for s in sleeps)
    events = state.read_events(state_dir=state_dir)
    assert len(events) == 3


def test_run_forever_uses_off_hours_cadence_after_error(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(raise_on_positions=True)

    sleeps: list[int] = []
    monitor.run_forever(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        dry_run=True, max_cycles=2,
        sleep_fn=lambda s: sleeps.append(s),
        clock_fn=lambda: SessionState(True, "open", "x", 60),
    )
    # Both cycles errored at Tiger read -> 300s sleep used both times
    assert sleeps == [300, 300]
