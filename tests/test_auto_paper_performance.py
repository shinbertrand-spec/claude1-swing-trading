"""Tests for tools.auto_paper.performance — realized vs backtest dashboard.

Uses tmp_path + monkeypatched module constants so tests don't write to the
real ledgers/paper-auto/ or journal/paper-auto/.

Session 3 dependency: the schema's ``position_state`` block currently has
``additionalProperties: false`` and does NOT include ``exit_price``. Session
3 will add this when it wires the close-out path. To keep these tests
self-contained — and to avoid depending on either Session 1's submitted
state-enum addition or Session 3's exit_price field — every ledger here
is written DIRECTLY as YAML (NOT through ``state.write_submitted_ledger``
which schema-validates). The performance module itself never validates,
so the dashboard's read path is the only thing under test.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from types import SimpleNamespace

import pytest
import yaml

from tools.auto_paper import performance, state


# ---------------------------------------------------------- fixtures


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    """Redirect paper-auto paths into a tmp tree for a single test."""
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    positions_json.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    return ledger_dir, positions_json


@pytest.fixture
def deployable_path(tmp_path):
    """Build a stand-in deployable_setups.yml so tests have a stable baseline.

    Numbers chosen to match the real tools/deployable_setups.yml at write
    time so the comparison logic is exercised against believable values.
    """
    p = tmp_path / "deployable_setups.yml"
    p.write_text(
        "deployable:\n"
        "  - setup: SEPA-VCP\n"
        "    variant: sell-aware\n"
        "    rolling_agg_sharpe: 2.28\n"
        "    rolling_agg_max_dd_pct: -10.96\n"
        "    n: 394\n"
        "    cleared_at: 2026-05-24\n"
        "  - setup: EP\n"
        "    variant: loosened\n"
        "    rolling_agg_sharpe: 2.13\n"
        "    rolling_agg_max_dd_pct: -3.07\n"
        "    n: 43\n"
        "    cleared_at: 2026-05-24\n",
        encoding="utf-8",
    )
    return str(p)


# ---------------------------------------------------------- ledger synth


def _synthetic_ledger_doc(
    *,
    ticker: str,
    meta_state: str,
    setup_type: str = "SEPA-VCP",
    setup_grade: str = "A",
    fill_price: float = 100.0,
    initial_stop: float = 95.0,
    shares: int = 10,
    fill_date: str = "2026-05-01",
    sector_etf: str = "XLK",
    broker_order_id: int = 99000,
    exit_price: float | None = None,
    exit_date: str | None = None,
    exit_reason: str | None = None,
    notes: str | None = None,
) -> dict:
    """Build a ledger dict in the requested state.

    NOT schema-validated — Session 1's ``submitted`` state + ``account_track``
    field and Session 3's ``position_state.exit_price`` may not be on the
    schema in this branch yet. The performance module reads YAML, never
    validates, so the dashboard's read path is what's actually under test.
    """
    starter_stage_map = {
        "submitted": "STARTER",
        "starter": "STARTER",
        "stage-2": "Stage-2",
        "stage-3": "Stage-3",
        "trailing": "trailing",
        "closed": "closed",
    }
    doc: dict = {
        "meta": {
            "schema_version": "1.0",
            "ticker": ticker.upper(),
            "asof": f"{(exit_date or fill_date)}T16:30:00+00:00",
            "state": meta_state,
            "account_track": "paper-auto",
            "ledger_path": f"ledgers/paper-auto/{ticker.upper()}.yml",
            "created_by": "auto_paper/pipeline",
            "created_at": f"{fill_date}T13:30:00+00:00",
        },
        "setup_classification": {
            "type": setup_type,
            "grade": setup_grade,
            "pivot_price": float(fill_price),
            "stop_price": float(initial_stop),
            "stop_distance_pct": (fill_price - initial_stop) / fill_price,
            "trace_refs": [],
            "confluence_checklist": [],
        },
        "position_state": {
            "stage": starter_stage_map.get(meta_state, "STARTER"),
            "intended_full_shares": int(shares),
            "starter": {
                "trigger": "EPGap" if setup_type == "EP" else "VCPBreakout",
                "fill_date": fill_date,
                "shares": int(shares),
                "fill_price": float(fill_price),
                "limit_price_placed": float(fill_price),
                "initial_stop": float(initial_stop),
                "broker_order_id": broker_order_id,
                "broker": "tiger_paper",
            },
            "current_stop": float(initial_stop),
        },
        "regime": {"sector_etf": sector_etf, "computed_at": f"{fill_date}T13:30:00+00:00"},
        "reasoning_trace": [],
    }
    if exit_price is not None:
        doc["position_state"]["exit_price"] = float(exit_price)
        doc["position_state"]["exit_date"] = exit_date
        doc["position_state"]["exit_reason"] = exit_reason or "closed"
        doc["meta"]["updated_by"] = "auto_paper/exits"
        doc["meta"]["updated_at"] = f"{exit_date}T16:30:00+00:00"
    if notes is not None:
        doc["notes"] = notes
    return doc


def _write_synthetic_ledger(ledger_dir, *, ticker: str, **kw) -> str:
    """Synthesize a paper-auto ledger directly (no schema validator)."""
    doc = _synthetic_ledger_doc(ticker=ticker, **kw)
    p = os.path.join(str(ledger_dir), f"{ticker.upper()}.yml")
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return p


def _append_positions_entry(positions_json, entry):
    """Append-or-create the positions.json index."""
    if os.path.isfile(positions_json):
        with open(positions_json, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {
            "_account_track": "paper-auto",
            "_schema_version": "v2",
            "updated": "2026-05-24T16:30:00+00:00",
            "positions": [],
        }
    data.setdefault("positions", []).append(entry)
    with open(positions_json, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _seed_position(
    paper_dirs, *, ticker, meta_state, positions_stage,
    setup_type="SEPA-VCP", setup_grade="A",
    fill_price=100.0, initial_stop=95.0, shares=10,
    fill_date="2026-05-01", sector="XLK",
    broker_order_id=99000,
    exit_price=None, exit_date=None, exit_reason=None, notes=None,
):
    """Helper: write a synthetic paper-auto ledger + positions.json entry."""
    ledger_dir, positions_json = paper_dirs
    p = _write_synthetic_ledger(
        ledger_dir,
        ticker=ticker, meta_state=meta_state,
        setup_type=setup_type, setup_grade=setup_grade,
        fill_price=fill_price, initial_stop=initial_stop,
        shares=shares, fill_date=fill_date, sector_etf=sector,
        broker_order_id=broker_order_id,
        exit_price=exit_price, exit_date=exit_date, exit_reason=exit_reason,
        notes=notes,
    )
    _append_positions_entry(positions_json, {
        "ticker": ticker.upper(),
        "ledger_path": p.replace("\\", "/"),
        "entry_date": fill_date,
        "entry_price": fill_price,
        "shares": shares,
        "stop": initial_stop,
        "target_1": fill_price + 2 * (fill_price - initial_stop),
        "sector": sector,
        "broker_order_id": broker_order_id,
        "broker": "tiger_paper",
        "stage": positions_stage,
        "setup_type": setup_type,
        "setup_grade": setup_grade,
    })


def _seed_closed_position(paper_dirs, *, ticker, fill_price, exit_price,
                          initial_stop=95.0, **kw):
    """Common case: meta.state=closed + positions.json stage=closed."""
    kw.setdefault("fill_date", "2026-05-01")
    exit_date = kw.pop("exit_date", "2026-05-15")
    exit_reason = kw.pop("exit_reason", "target_hit")
    _seed_position(
        paper_dirs,
        ticker=ticker, meta_state="closed", positions_stage="closed",
        fill_price=fill_price, initial_stop=initial_stop,
        exit_price=exit_price, exit_date=exit_date, exit_reason=exit_reason,
        **kw,
    )


# ---------------------------------------------------------- happy-path tests


def test_empty_track_returns_empty_report(paper_dirs, deployable_path):
    """No positions.json + no ledgers → empty report with no crash."""
    report = performance.compute_performance(deployable_path=deployable_path)
    assert report.n_realized == 0
    assert report.n_open == 0
    assert report.n_submitted == 0
    assert report.overall_trade_stats is None
    assert report.overall_return_stats is None
    # Comparisons still get populated for deployable setups with no_data.
    assert any(c.status == "no_data" for c in report.comparisons)


def test_single_closed_winner(paper_dirs, deployable_path):
    """A single winning trade → 100% win rate + positive expectancy."""
    _seed_closed_position(
        paper_dirs, ticker="NVDA",
        fill_price=100.0, exit_price=120.0, initial_stop=95.0,
    )
    report = performance.compute_performance(deployable_path=deployable_path)
    assert report.n_realized == 1
    assert report.n_open == 0
    assert report.overall_trade_stats.n_trades == 1
    assert report.overall_trade_stats.win_rate == 1.0
    assert report.overall_trade_stats.n_wins == 1
    assert report.overall_trade_stats.n_losses == 0
    # R-mul = (120-100)/(100-95) = 4.0
    assert report.realized_trades[0].r_multiple == pytest.approx(4.0)
    assert report.overall_trade_stats.expectancy_r == pytest.approx(4.0)


def test_single_closed_loser(paper_dirs, deployable_path):
    """A single losing trade → 0% win rate + negative R."""
    _seed_closed_position(
        paper_dirs, ticker="AAPL",
        fill_price=100.0, exit_price=94.0, initial_stop=95.0,
    )
    report = performance.compute_performance(deployable_path=deployable_path)
    assert report.n_realized == 1
    assert report.overall_trade_stats.win_rate == 0.0
    assert report.overall_trade_stats.n_losses == 1
    # R-mul = (94-100)/(100-95) = -1.2
    assert report.realized_trades[0].r_multiple == pytest.approx(-1.2)
    assert report.overall_trade_stats.expectancy_r == pytest.approx(-1.2)


def test_mixed_wins_losses_expectancy(paper_dirs, deployable_path):
    """Mix of wins and losses — expectancy + profit factor sanity-check.

    Trades:
        - NVDA: +2R win  (exit_price=110, stop_dist=5 → (110-100)/5 = 2)
        - AAPL: -1R loss (exit_price=95)
        - MSFT: +3R win  (exit_price=115)
    Expected:
        win_rate = 2/3
        expectancy = (2 + -1 + 3) / 3 = 4/3
        profit_factor = 5 / 1 = 5.0
    """
    _seed_closed_position(
        paper_dirs, ticker="NVDA",
        fill_price=100.0, initial_stop=95.0, exit_price=110.0,
        fill_date="2026-05-01", exit_date="2026-05-08",
    )
    _seed_closed_position(
        paper_dirs, ticker="AAPL",
        fill_price=100.0, initial_stop=95.0, exit_price=95.0,
        fill_date="2026-05-02", exit_date="2026-05-09",
    )
    _seed_closed_position(
        paper_dirs, ticker="MSFT",
        fill_price=100.0, initial_stop=95.0, exit_price=115.0,
        fill_date="2026-05-03", exit_date="2026-05-10",
    )
    report = performance.compute_performance(deployable_path=deployable_path)
    assert report.n_realized == 3
    ts = report.overall_trade_stats
    assert ts.n_wins == 2
    assert ts.n_losses == 1
    assert ts.win_rate == pytest.approx(2 / 3)
    assert ts.expectancy_r == pytest.approx(4 / 3)
    assert ts.profit_factor == pytest.approx(5.0)


def test_setup_filter_narrows_realized(paper_dirs, deployable_path):
    """Filtering by setup_type narrows the realized list, leaves counts alone."""
    _seed_closed_position(
        paper_dirs, ticker="NVDA", setup_type="SEPA-VCP",
        fill_price=100.0, initial_stop=95.0, exit_price=110.0,
    )
    _seed_closed_position(
        paper_dirs, ticker="GOOGL", setup_type="EP", setup_grade="Swan",
        fill_price=200.0, initial_stop=190.0, exit_price=220.0,
    )
    full = performance.compute_performance(deployable_path=deployable_path)
    assert full.n_realized == 2

    sepa_only = performance.compute_performance(
        setup_filter="SEPA-VCP", deployable_path=deployable_path,
    )
    assert sepa_only.n_realized == 1
    assert sepa_only.realized_trades[0].ticker == "NVDA"

    ep_only = performance.compute_performance(
        setup_filter="EP", deployable_path=deployable_path,
    )
    assert ep_only.n_realized == 1
    assert ep_only.realized_trades[0].ticker == "GOOGL"


def test_backtest_comparison_status_no_data(paper_dirs, deployable_path):
    """A deployable setup with no realized trades gets status=no_data."""
    report = performance.compute_performance(deployable_path=deployable_path)
    by_setup = {c.setup: c for c in report.comparisons}
    assert "SEPA-VCP" in by_setup
    assert "EP" in by_setup
    assert by_setup["SEPA-VCP"].status == "no_data"
    assert by_setup["EP"].status == "no_data"
    # Backtest expectations still populated from deployable_setups.yml.
    assert by_setup["SEPA-VCP"].backtest_sharpe == pytest.approx(2.28)
    assert by_setup["EP"].backtest_sharpe == pytest.approx(2.13)


def test_backtest_comparison_warn_under_sample(paper_dirs, deployable_path):
    """With <30 trades on a setup, status is always warn (preliminary)."""
    _seed_closed_position(
        paper_dirs, ticker="NVDA", setup_type="SEPA-VCP",
        fill_price=100.0, initial_stop=95.0, exit_price=110.0,
    )
    report = performance.compute_performance(deployable_path=deployable_path)
    by_setup = {c.setup: c for c in report.comparisons}
    assert by_setup["SEPA-VCP"].n_trades == 1
    assert by_setup["SEPA-VCP"].status == "warn"
    assert "preliminary" in by_setup["SEPA-VCP"].status_note.lower()


def test_open_position_excluded_from_realized(paper_dirs):
    """A starter-state position counts toward n_open, not n_realized."""
    _seed_position(
        paper_dirs, ticker="TSLA",
        meta_state="starter", positions_stage="starter",
        setup_type="EP", setup_grade="Swan",
        fill_price=301.0, initial_stop=285.0, shares=5,
        fill_date=_dt.date.today().isoformat(),
        sector="XLY", broker_order_id=88001,
    )
    report = performance.compute_performance()
    assert report.n_realized == 0
    assert report.n_open == 1
    assert report.open_positions[0].ticker == "TSLA"
    assert report.open_positions[0].entry_price == 301.0
    assert report.open_positions[0].shares == 5


def test_submitted_position_counted(paper_dirs):
    """A submitted-state position counts toward n_submitted only."""
    _seed_position(
        paper_dirs, ticker="META",
        meta_state="submitted", positions_stage="submitted",
        setup_type="SEPA-VCP", setup_grade="A",
        fill_price=500.5, initial_stop=475.0, shares=4,
        fill_date=_dt.date.today().isoformat(),
        sector="XLC", broker_order_id=77001,
    )
    report = performance.compute_performance()
    assert report.n_realized == 0
    assert report.n_open == 0
    assert report.n_submitted == 1


def test_closed_without_exit_price_is_flagged_not_realized(paper_dirs):
    """An expired-unfilled ledger (closed + no exit_price) is flagged, not counted."""
    _seed_position(
        paper_dirs, ticker="ORCL",
        meta_state="closed", positions_stage="closed_unfilled",
        setup_type="EP", setup_grade="Duck",
        fill_price=120.5, initial_stop=115.0, shares=8,
        fill_date=_dt.date.today().isoformat(),
        sector="XLK", broker_order_id=66001,
        # No exit_price — mimics DAY-expired order or pre-Session-3 close.
        notes="Order expired unfilled on 2026-05-24",
    )
    report = performance.compute_performance()
    assert report.n_realized == 0
    assert report.n_open == 0
    # The closed_unfilled stage isn't a recognized state — the dashboard
    # silently ignores it (positions.json stage doesn't match submitted /
    # starter etc.) but the ledger meta.state=closed + no exit_price
    # falls through to the flag path.
    assert any("no exit_price" in n for n in report.notes)


# ---------------------------------------------------------- open P&L


class _FakePositionsClient:
    """Stand-in for TigerClient whose positions().output looks like the real one."""

    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return SimpleNamespace(output={
            "account_masked": "...4321",
            "n_positions": len(self._positions),
            "positions": self._positions,
        })


def test_compute_open_pnl_sums_unrealized(paper_dirs):
    """Open P&L = sum of broker unrealized_pnl across paper-auto starter positions."""
    # Two starter-state paper-auto positions.
    _seed_position(
        paper_dirs, ticker="NVDA",
        meta_state="starter", positions_stage="starter",
        setup_type="SEPA-VCP", setup_grade="A",
        fill_price=850.0, initial_stop=807.5, shares=10,
        fill_date=_dt.date.today().isoformat(),
        sector="XLK", broker_order_id=50001,
    )
    _seed_position(
        paper_dirs, ticker="AAPL",
        meta_state="starter", positions_stage="starter",
        setup_type="SEPA-VCP", setup_grade="A",
        fill_price=180.0, initial_stop=171.0, shares=15,
        fill_date=_dt.date.today().isoformat(),
        sector="XLK", broker_order_id=50002,
    )

    # Mock broker returns market_value + unrealized_pnl per symbol.
    client = _FakePositionsClient([
        {"symbol": "NVDA", "quantity": 10, "average_cost": 850.0,
         "market_value": 9000.0, "unrealized_pnl": 500.0},
        {"symbol": "AAPL", "quantity": 15, "average_cost": 180.0,
         "market_value": 2625.0, "unrealized_pnl": -75.0},
        # An unrelated symbol in the account (e.g. human-track holding)
        # must NOT be counted toward paper-auto open P&L.
        {"symbol": "SPY", "quantity": 100, "average_cost": 500.0,
         "market_value": 51000.0, "unrealized_pnl": 1000.0},
    ])

    out = performance.compute_open_pnl(client=client)
    assert out["error"] is None
    assert out["total_unrealized_pnl_usd"] == pytest.approx(500.0 + -75.0)
    assert out["total_market_value_usd"] == pytest.approx(9000.0 + 2625.0)
    tickers = {p["ticker"] for p in out["by_position"]}
    assert tickers == {"NVDA", "AAPL"}
    # Verify per-position fields populated.
    nvda = next(p for p in out["by_position"] if p["ticker"] == "NVDA")
    assert nvda["market_value"] == pytest.approx(9000.0)
    assert nvda["unrealized_pnl_usd"] == pytest.approx(500.0)
    assert nvda["current_price"] == pytest.approx(900.0)
    # entry_price = 850 * 10 = 8500 cost; pnl_pct = 500/8500
    assert nvda["unrealized_pnl_pct"] == pytest.approx(500.0 / 8500.0)


def test_compute_open_pnl_no_open_positions(paper_dirs):
    """No paper-auto open positions → zero totals, broker not consulted."""

    class _Client:
        def positions(self):  # should NOT be called
            raise AssertionError("broker should not be consulted when no positions")

    out = performance.compute_open_pnl(client=_Client())
    assert out["total_unrealized_pnl_usd"] == 0.0
    assert out["total_market_value_usd"] == 0.0
    assert out["by_position"] == []
    assert out["error"] is None


def test_compute_open_pnl_handles_broker_error(paper_dirs):
    """If TigerClient.positions() raises, return error string + zero totals."""
    _seed_position(
        paper_dirs, ticker="NVDA",
        meta_state="starter", positions_stage="starter",
        setup_type="SEPA-VCP", setup_grade="A",
        fill_price=850.0, initial_stop=807.5, shares=10,
        fill_date=_dt.date.today().isoformat(),
        sector="XLK", broker_order_id=40001,
    )

    class _BrokenClient:
        def positions(self):
            raise RuntimeError("HTTP 503")

    out = performance.compute_open_pnl(client=_BrokenClient())
    assert out["error"] is not None
    assert "HTTP 503" in out["error"]
    assert out["total_unrealized_pnl_usd"] == 0.0
    assert "NVDA" in out["missing_quotes"]


# ---------------------------------------------------------- report dict


def test_report_to_dict_round_trips(paper_dirs, deployable_path):
    """Report serializes to a JSON-safe dict (asdict for nested dataclasses)."""
    _seed_closed_position(
        paper_dirs, ticker="NVDA",
        fill_price=100.0, initial_stop=95.0, exit_price=110.0,
    )
    report = performance.compute_performance(deployable_path=deployable_path)
    d = report.to_dict()
    # Round-trips through JSON without TypeError.
    s = json.dumps(d, default=str)
    assert "NVDA" in s
    assert d["n_realized"] == 1
    assert d["overall_trade_stats"]["n_trades"] == 1
