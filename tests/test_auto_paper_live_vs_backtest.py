"""Tests for B3 live-vs-backtest report (tools.auto_paper.live_vs_backtest)."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
import yaml

from tools.auto_paper import live_vs_backtest as lvb


def _write_ledger(dir_, ticker, *, state="starter", fill="2026-06-02",
                  fill_price=100.0, shares=50, exit_price=None, exit_date=None,
                  setup="ts_momentum_liquid_us"):
    doc = {
        "meta": {"ticker": ticker, "state": state,
                 "created_at": f"{fill}T14:00:00+00:00",
                 "updated_at": "2026-06-06T00:00:00+00:00"},
        "setup_classification": {"type": setup, "pivot_price": fill_price},
        "position_state": {
            "stage": "STARTER",
            "starter": {"fill_date": fill, "shares": shares,
                        "fill_price": fill_price, "limit_price_placed": fill_price,
                        "broker_order_id": 1},
        },
    }
    if exit_price is not None:
        doc["position_state"]["exit_price"] = exit_price
        if exit_date:
            doc["position_state"]["exit_date"] = exit_date
    (dir_ / f"{ticker}.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def _price_loader(closes: dict[str, list[float]], dates: list[str]):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    frames = {
        t: pd.DataFrame({"Close": vals, "Open": vals}, index=idx)
        for t, vals in closes.items()
    }
    return lambda ticker: frames.get(ticker)


DATES = ["2026-06-02", "2026-06-03", "2026-06-04"]


class TestTradesFromLedgers:
    def test_submitted_and_unfilled_skipped(self, tmp_path):
        _write_ledger(tmp_path, "AAA", state="submitted")
        _write_ledger(tmp_path, "BBB", state="closed_unfilled")
        trades, _ = lvb.trades_from_ledgers(tmp_path)
        assert trades == []

    def test_closed_without_exit_price_skipped_with_note(self, tmp_path):
        _write_ledger(tmp_path, "AAA", state="closed")
        trades, notes = lvb.trades_from_ledgers(tmp_path)
        assert trades == []
        assert any("without exit_price" in n for n in notes)

    def test_exit_date_fallback_carries_caveat(self, tmp_path):
        _write_ledger(tmp_path, "AAA", state="closed", exit_price=105.0)
        trades, _ = lvb.trades_from_ledgers(tmp_path)
        assert len(trades) == 1
        assert trades[0].exit_date == date(2026, 6, 6)
        assert any("updated_at" in c for c in trades[0].caveats)


class TestReconstruction:
    def test_hand_computed_daily_pnl(self, tmp_path):
        # fill 100 on 06-02; closes 101, 103; exit 104 on 06-04. 10 shares.
        _write_ledger(tmp_path, "AAA", fill_price=100.0, shares=10,
                      state="closed", exit_price=104.0, exit_date="2026-06-04")
        trades, _ = lvb.trades_from_ledgers(tmp_path)
        loader = _price_loader({"AAA": [101.0, 103.0, 999.0]}, DATES)
        pnl, excluded = lvb.reconstruct_daily_pnl(
            trades, base_equity=100_000, asof=date(2026, 6, 4), price_loader=loader,
        )
        assert excluded == []
        col = pnl["ts_momentum_liquid_us"]
        assert col.loc[date(2026, 6, 2)] == pytest.approx(10.0)   # (101-100)*10
        assert col.loc[date(2026, 6, 3)] == pytest.approx(20.0)   # (103-101)*10
        # exit day marks at exit_price 104, NOT the 999 close
        assert col.loc[date(2026, 6, 4)] == pytest.approx(10.0)   # (104-103)*10
        assert pnl["_total"].sum() == pytest.approx(40.0)         # (104-100)*10

    def test_corrupt_notional_excluded(self, tmp_path):
        # 29,760 sh * $81 = $2.4M > 10% of 1M base -> guard fires
        _write_ledger(tmp_path, "NFLX", fill_price=81.0, shares=29_760)
        trades, _ = lvb.trades_from_ledgers(tmp_path)
        loader = _price_loader({"NFLX": [82.0, 83.0, 84.0]}, DATES)
        pnl, excluded = lvb.reconstruct_daily_pnl(
            trades, base_equity=1_000_000, asof=date(2026, 6, 4), price_loader=loader,
        )
        assert pnl.empty
        assert len(excluded) == 1
        assert "corrupt-ledger guard" in excluded[0][1]

    def test_missing_history_excluded_not_crashed(self, tmp_path):
        _write_ledger(tmp_path, "AAA")
        trades, _ = lvb.trades_from_ledgers(tmp_path)
        pnl, excluded = lvb.reconstruct_daily_pnl(
            trades, base_equity=100_000, asof=date(2026, 6, 4),
            price_loader=lambda t: None,
        )
        assert excluded[0][1] == "no cached price history"


class TestReportAndBenchmarks:
    def _roster(self, tmp_path):
        roster = tmp_path / "roster.yml"
        roster.write_text(yaml.safe_dump({
            "deployable": [
                {"setup": "ts_momentum_liquid_us",
                 "rolling_agg_sharpe": 2.13, "live_benchmark_sharpe": 1.23},
            ],
            "parked_by_tightened_gate": [
                {"setup": "old_setup", "rolling_agg_sharpe": 0.9},
            ],
        }), encoding="utf-8")
        return roster

    def test_benchmark_prefers_net_of_cost(self, tmp_path):
        b = lvb.load_benchmarks(self._roster(tmp_path))
        assert b["ts_momentum_liquid_us"]["sharpe"] == 1.23
        assert "net-of-cost" in b["ts_momentum_liquid_us"]["source"]
        assert b["old_setup"]["source"].startswith("rolling_agg_sharpe")

    def test_full_report_with_psr_and_cone(self, tmp_path):
        ldir = tmp_path / "ledgers"
        ldir.mkdir()
        # 30 trading days of history so PSR is computable
        dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-02", periods=30)]
        rng = np.random.default_rng(5)
        closes = list(100.0 * np.cumprod(1 + rng.normal(0.002, 0.01, size=30)))
        _write_ledger(ldir, "AAA", fill="2026-06-02", fill_price=100.0, shares=50)
        report = lvb.compute_report(
            base_equity=100_000, asof=date(2026, 7, 14),
            ledger_dir=ldir, roster_path=self._roster(tmp_path),
            price_loader=_price_loader({"AAA": closes}, dates),
        )
        assert report["sleeve"]["n_days"] == 30
        assert "psr_vs_backtest" in report["sleeve"]
        assert 0.0 <= report["sleeve"]["psr_vs_backtest"] <= 1.0
        assert report["cone"], "cone rows expected"
        md = lvb.render_markdown(report)
        assert "PSR vs backtest benchmark" in md
        assert "Expectation cone" in md

    def test_report_empty_ledgers_degrades(self, tmp_path):
        ldir = tmp_path / "empty"
        ldir.mkdir()
        report = lvb.compute_report(
            base_equity=100_000, ledger_dir=ldir, roster_path=self._roster(tmp_path),
        )
        assert report["sleeve"]["note"] == "no reconstructable trades"
        lvb.render_markdown(report)  # must not raise
