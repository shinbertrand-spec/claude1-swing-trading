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


# ---------------------------------------------------------------------------
# Skip rows (fill-fidelity Task 0, 2026-08-06)
# ---------------------------------------------------------------------------

def _unfilled_ledger(ticker="XYZ", pivot=100.0, limit=103.0, attempt="2026-08-13",
                     order_id=222):
    doc = _ledger(ticker=ticker, state="closed", pivot=pivot, limit=limit,
                  fill_date=attempt, order_id=order_id)
    doc["position_state"]["unfilled"] = True
    doc["position_state"]["starter"]["fill_price"] = limit  # seeded, not real
    return doc


def _ohlc_loader(o=104.0, h=106.0, low=103.5, c=105.0, day="2026-08-13"):
    idx = pd.DatetimeIndex([pd.Timestamp(day)])
    df = pd.DataFrame({"Open": [o], "High": [h], "Low": [low], "Close": [c]},
                      index=idx)
    return lambda ticker: df


_NO_INTRADAY = lambda t, d: None


class TestComputeSkipRow:
    def test_filled_ledger_returns_none(self):
        assert ea.compute_skip_row(_ledger(), intraday_loader=_NO_INTRADAY) is None

    def test_explained_when_day_low_above_limit(self):
        row = ea.compute_skip_row(
            _unfilled_ledger(), price_loader=_ohlc_loader(low=103.5),
            intraday_loader=_NO_INTRADAY)
        assert row.skip_explained is True
        assert row.explain_basis == "day_low_above_limit"
        assert row.attempt_day_low == 103.5
        assert row.key.endswith("|skip")
        assert row.to_dict()["row_type"] == "skip"

    def test_unexplained_when_day_low_within_limit(self):
        row = ea.compute_skip_row(
            _unfilled_ledger(), price_loader=_ohlc_loader(low=102.0),
            intraday_loader=_NO_INTRADAY)
        assert row.skip_explained is False
        assert row.explain_basis == "UNEXPLAINED_day_low_within_limit"

    def test_no_price_data_degrades_honestly(self):
        row = ea.compute_skip_row(
            _unfilled_ledger(), price_loader=lambda t: None,
            intraday_loader=_NO_INTRADAY)
        assert row.skip_explained is None
        assert row.explain_basis == "no_price_data"

    def test_closed_unfilled_state_also_emits(self):
        doc = _ledger(state="closed_unfilled")
        row = ea.compute_skip_row(doc, price_loader=_ohlc_loader(low=104.0, day="2026-06-02"),
                                  intraday_loader=_NO_INTRADAY)
        assert row is not None
        assert row.skip_explained is True  # low 104 > limit 100.6

    def test_intraday_evidence_recorded_when_available(self):
        row = ea.compute_skip_row(
            _unfilled_ledger(), price_loader=_ohlc_loader(low=103.5),
            intraday_loader=lambda t, d: 104.25)
        assert row.price_0935 == 104.25


class TestSkipSeriesAndRender:
    def test_append_series_mixed_rows_dedup(self, tmp_path):
        p = tmp_path / "drag.jsonl"
        fill = ea.compute_row(_ledger(), price_loader=_price_loader_factory())
        skip = ea.compute_skip_row(_unfilled_ledger(),
                                   price_loader=_ohlc_loader(low=103.5),
                                   intraday_loader=_NO_INTRADAY)
        _, n1 = ea.append_series([fill, skip], path=p)
        _, n2 = ea.append_series([fill, skip], path=p)
        assert (n1, n2) == (2, 0)
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        assert [r.get("row_type", "fill") for r in rows] == ["fill", "skip"]

    def test_render_marks_unexplained_as_fault(self):
        skip = ea.compute_skip_row(_unfilled_ledger(),
                                   price_loader=_ohlc_loader(low=102.0),
                                   intraday_loader=_NO_INTRADAY)
        md = ea.render_skips_markdown([skip])
        assert "**NO — FAULT**" in md

    def test_render_empty(self):
        assert "none recorded" in ea.render_skips_markdown([])


class TestArchiveScan:
    """_archive/ inclusion (2026-08-18): closed ledgers archived out of the
    flat dir on ticker re-selection must stay visible to both scanners."""

    def test_fill_rows_found_in_archive_subdir(self, tmp_path):
        ldir = tmp_path / "ledgers"
        (ldir / "_archive").mkdir(parents=True)
        (ldir / "AAA.yml").write_text(
            yaml.safe_dump(_ledger(ticker="AAA")), encoding="utf-8")
        (ldir / "_archive" / "BBB-2026-06-02-closed.yml").write_text(
            yaml.safe_dump(_ledger(ticker="BBB", state="closed", order_id=2)),
            encoding="utf-8")
        rows = ea.compute_rows(ldir, price_loader=_price_loader_factory())
        assert {r.ticker for r in rows} == {"AAA", "BBB"}

    def test_skip_rows_found_in_archive_subdir(self, tmp_path):
        ldir = tmp_path / "ledgers"
        (ldir / "_archive").mkdir(parents=True)
        (ldir / "_archive" / "CCC-2026-08-13-closed.yml").write_text(
            yaml.safe_dump(_unfilled_ledger(ticker="CCC", order_id=3)),
            encoding="utf-8")
        skips = ea.compute_skip_rows(ldir, price_loader=_ohlc_loader(low=103.5),
                                     intraday_loader=_NO_INTRADAY)
        assert [s.ticker for s in skips] == ["CCC"]
