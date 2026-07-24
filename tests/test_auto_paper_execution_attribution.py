"""Tests for B2 signal-vs-execution attribution."""
from __future__ import annotations

import json

import pandas as pd
import pytest
import yaml

from tools.auto_paper import execution_attribution as ea


def _ledger(
    ticker="ABC", state="starter", pivot=100.0, fill=100.5, limit=100.6,
    fill_date="2026-06-02", shares=50, order_id=111,
) -> dict:
    return {
        "meta": {"ticker": ticker, "state": state,
                 "created_at": "2026-06-02T14:00:00+00:00"},
        "setup_classification": {"type": "ts_momentum", "pivot_price": pivot},
        "position_state": {
            "stage": "STARTER",
            "starter": {
                "fill_date": fill_date, "shares": shares, "fill_price": fill,
                "limit_price_placed": limit, "broker_order_id": order_id,
            },
        },
        "reasoning_trace": [
            {"inputs": {"ticker": ticker, "signal_date": "2026-06-01T00:00:00-04:00"}},
        ],
    }


def _price_loader_factory(open_price=100.2):
    idx = pd.DatetimeIndex([pd.Timestamp("2026-06-02")])
    df = pd.DataFrame({"Open": [open_price], "Close": [open_price + 1]}, index=idx)
    return lambda ticker: df


class TestComputeRow:
    def test_decomposition_identity(self):
        # pivot 100, open 100.2, fill 100.5:
        # total = 50 bps, delay = 20 bps, resid = 30 bps
        row = ea.compute_row(_ledger(), price_loader=_price_loader_factory())
        assert row.total_slippage_bps == 50.0
        assert row.delay_cost_bps == 20.0
        assert row.execution_residual_bps == 30.0
        assert row.delay_cost_bps + row.execution_residual_bps == row.total_slippage_bps
        assert row.drag_usd == pytest.approx(0.005 * 100.5 * 50, abs=0.02)
        assert row.signal_date == "2026-06-01"

    def test_submitted_seeded_fill_excluded(self):
        assert ea.compute_row(_ledger(state="submitted")) is None

    def test_closed_unfilled_excluded(self):
        assert ea.compute_row(_ledger(state="closed_unfilled")) is None

    def test_missing_fill_excluded(self):
        doc = _ledger()
        del doc["position_state"]["starter"]["fill_price"]
        assert ea.compute_row(doc) is None

    def test_missing_bar_degrades_to_total_only(self):
        row = ea.compute_row(_ledger(), price_loader=lambda t: None)
        assert row.total_slippage_bps == 50.0
        assert row.delay_cost_bps is None
        assert row.execution_residual_bps is None

    def test_suspect_flag_on_corrupted_row(self):
        # fill 6% above pivot -> 600 bps > 500 bps sanity bound
        row = ea.compute_row(
            _ledger(fill=106.0), price_loader=_price_loader_factory(),
        )
        assert row.suspect is True

    def test_favorable_slippage_is_negative(self):
        row = ea.compute_row(
            _ledger(fill=99.5), price_loader=_price_loader_factory(),
        )
        assert row.total_slippage_bps == -50.0


class TestSeriesAndSummary:
    def test_dir_scan_and_series_dedup(self, tmp_path):
        ldir = tmp_path / "ledgers"
        ldir.mkdir()
        for i, t in enumerate(["AAA", "BBB"]):
            (ldir / f"{t}.yml").write_text(
                yaml.safe_dump(_ledger(ticker=t, order_id=100 + i)), encoding="utf-8",
            )
        rows = ea.compute_rows(ldir, price_loader=_price_loader_factory())
        assert len(rows) == 2
        series = tmp_path / "drag.jsonl"
        _, n1 = ea.append_series(rows, path=series)
        _, n2 = ea.append_series(rows, path=series)  # idempotent
        assert (n1, n2) == (2, 0)
        recs = [json.loads(x) for x in series.read_text().splitlines()]
        assert len(recs) == 2

    def test_monthly_summary_weighting_and_flag(self):
        rows = [
            ea.compute_row(
                _ledger(ticker="AAA", fill=100.5, shares=100),   # 50 bps, 10050 notional
                price_loader=_price_loader_factory(),
            ),
            ea.compute_row(
                _ledger(ticker="BBB", fill=100.1, shares=300, order_id=2),  # 10 bps, 30030
                price_loader=_price_loader_factory(),
            ),
        ]
        m = ea.monthly_summary(rows)
        assert len(m) == 1
        # weighted: (50*10050 + 10*30030)/(10050+30030) = 20.03...
        assert 19.5 < m[0]["weighted_total_bps"] < 20.5
        assert m[0]["flag"] is False  # 20 bps < 25 threshold

    def test_suspect_rows_excluded_from_summary(self):
        rows = [
            ea.compute_row(_ledger(fill=100.5), price_loader=_price_loader_factory()),
            ea.compute_row(
                _ledger(ticker="BAD", fill=110.0, order_id=9),
                price_loader=_price_loader_factory(),
            ),
        ]
        m = ea.monthly_summary(rows)
        assert m[0]["n_trades"] == 1
        md = ea.render_markdown(rows)
        assert "Suspect rows" in md
        assert "BAD" in md
