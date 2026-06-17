"""Tests for tools.fundamentals.insider_track_record — opportunistic classifier.

Pure scoring is exercised with synthetic date-indexed close series (no network).
The anti-look-ahead invariant gets the heaviest coverage: a prior buy may only
score if its forward window closed on/before the scoring date.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tools.fundamentals.insider_track_record import (
    DateClose,
    InsiderTier,
    classify_insider,
    compute_track_record,
    fetch_insider_history,
    forward_abnormal_return,
    profitability_tier,
)


def _series(start: date, closes: list[float]) -> DateClose:
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    return DateClose(dates=dates, closes=[float(c) for c in closes])


def _fn(mapping):
    return lambda t: mapping.get(t)


# ---- DateClose ----------------------------------------------------------


def test_dateclose_next_bar_strictly_after():
    s = _series(date(2024, 1, 1), [10, 11, 12, 13])
    assert s.next_bar_index_after(date(2024, 1, 1)) == 1   # strictly after
    assert s.next_bar_index_after(date(2024, 1, 3)) == 3
    assert s.next_bar_index_after(date(2024, 1, 4)) is None


def test_dateclose_asof_prior():
    s = _series(date(2024, 1, 1), [10, 11, 12])
    assert s.asof(date(2024, 1, 2)) == 11
    assert s.asof(date(2024, 1, 5)) == 12   # most recent prior
    assert s.asof(date(2023, 12, 31)) is None


# ---- forward_abnormal_return -------------------------------------------


def test_forward_abnormal_basic():
    stk = _series(date(2024, 1, 1), [100, 100, 110, 115, 120, 130])
    spy = _series(date(2024, 1, 1), [100, 100, 101, 102, 105, 110])
    fr = forward_abnormal_return("STK", date(2024, 1, 1), horizon_days=3,
                                 close_series_fn=_fn({"STK": stk, "SPY": spy}))
    assert fr is not None
    assert fr.entry_date == "2024-01-02"   # next bar after event
    assert fr.exit_date == "2024-01-05"    # +3 bars
    assert fr.stock_return == pytest.approx(0.20)
    assert fr.benchmark_return == pytest.approx(0.05)
    assert fr.abnormal_return == pytest.approx(0.15)


def test_forward_abnormal_none_when_window_unrealized():
    stk = _series(date(2024, 1, 1), [100, 100, 110])  # too short for horizon 3
    fr = forward_abnormal_return("STK", date(2024, 1, 1), horizon_days=3,
                                 close_series_fn=_fn({"STK": stk}))
    assert fr is None


def test_forward_abnormal_missing_benchmark_treated_zero():
    stk = _series(date(2024, 1, 1), [100, 100, 110, 115, 120])
    fr = forward_abnormal_return("STK", date(2024, 1, 1), horizon_days=3,
                                 close_series_fn=_fn({"STK": stk}))  # no SPY
    assert fr is not None
    assert fr.benchmark_return == 0.0
    assert fr.abnormal_return == pytest.approx(fr.stock_return)


def test_forward_abnormal_none_for_unknown_ticker():
    assert forward_abnormal_return("ZZZ", date(2024, 1, 1), horizon_days=3,
                                   close_series_fn=_fn({})) is None


# ---- profitability_tier (un-tuned priors) ------------------------------


@pytest.mark.parametrize("n,hit,mean,expected", [
    (2, 1.0, 0.5, InsiderTier.UNRATED),       # below min_history
    (5, 0.7, 0.15, InsiderTier.ELITE),
    (5, 0.55, 0.05, InsiderTier.GOOD),
    (5, 0.45, 0.05, InsiderTier.NEUTRAL),     # mean ok but hit < 0.5 → not GOOD
    (5, 0.3, -0.02, InsiderTier.POOR),
    (5, 0.35, 0.01, InsiderTier.POOR),        # hit < 0.40 → POOR
])
def test_profitability_tier(n, hit, mean, expected):
    tier, _ = profitability_tier(n, hit, mean, min_history=3)
    assert tier == expected


def test_profitability_tier_none_inputs_unrated():
    tier, _ = profitability_tier(0, None, None)
    assert tier == InsiderTier.UNRATED


# ---- compute_track_record + ANTI-LOOK-AHEAD ----------------------------


def _buy(ticker, ev: date):
    return SimpleNamespace(ticker=ticker, event_date=ev.isoformat())


def test_track_record_excludes_unrealized_window():
    """A buy whose forward window hasn't closed by asof must NOT score —
    even though the price source contains the future data."""
    stk = _series(date(2024, 1, 1), [100] + [100, 110, 115, 120, 130, 140])
    fn = _fn({"STK": stk, "SPY": _series(date(2024, 1, 1), [100] * 8)})
    buys = [_buy("STK", date(2024, 1, 1))]   # window exits 2024-01-05 (entry 01-02 + 3)

    before = compute_track_record(buys, date(2024, 1, 4), close_series_fn=fn, horizon_days=3)
    assert before.n_scored == 0           # exit 01-05 > asof 01-04 → excluded
    assert before.tier == InsiderTier.UNRATED.value

    after = compute_track_record(buys, date(2024, 1, 5), close_series_fn=fn, horizon_days=3)
    assert after.n_scored == 1            # exit 01-05 <= asof 01-05 → realized


def test_track_record_excludes_buys_on_or_after_asof():
    stk = _series(date(2024, 1, 1), [100] * 20)
    fn = _fn({"STK": stk, "SPY": _series(date(2024, 1, 1), [100] * 20)})
    buys = [_buy("STK", date(2024, 1, 10))]
    stats = compute_track_record(buys, date(2024, 1, 10), close_series_fn=fn, horizon_days=3)
    assert stats.n_prior_buys == 0        # event_date == asof is not strictly prior


def test_track_record_elite_end_to_end():
    # Four early buys, all strong winners vs flat benchmark → ELITE.
    closes = [100, 100, 130, 160, 190, 220, 250, 280, 310, 340, 370, 400]
    stk = _series(date(2024, 1, 1), closes)
    spy = _series(date(2024, 1, 1), [100] * len(closes))
    fn = _fn({"STK": stk, "SPY": spy})
    buys = [_buy("STK", date(2024, 1, 1) + timedelta(days=i)) for i in range(4)]
    stats = compute_track_record(buys, date(2025, 1, 1), close_series_fn=fn,
                                 horizon_days=2, min_history=3,
                                 insider_cik="123", insider_name="Ace")
    assert stats.n_scored == 4
    assert stats.hit_rate == 1.0
    assert stats.mean_abnormal > 0.10
    assert stats.tier == InsiderTier.ELITE.value
    assert len(stats.samples) == 4


def test_track_record_poor_when_losers():
    closes = [100, 100, 90, 80, 70, 60, 50, 40]
    stk = _series(date(2024, 1, 1), closes)
    spy = _series(date(2024, 1, 1), [100] * len(closes))
    fn = _fn({"STK": stk, "SPY": spy})
    buys = [_buy("STK", date(2024, 1, 1) + timedelta(days=i)) for i in range(4)]
    stats = compute_track_record(buys, date(2025, 1, 1), close_series_fn=fn,
                                 horizon_days=2, min_history=3)
    assert stats.mean_abnormal < 0
    assert stats.tier == InsiderTier.POOR.value


def test_track_record_unrated_insufficient_history():
    stk = _series(date(2024, 1, 1), [100, 100, 120, 140])
    fn = _fn({"STK": stk, "SPY": _series(date(2024, 1, 1), [100] * 4)})
    buys = [_buy("STK", date(2024, 1, 1))]   # only 1 realized
    stats = compute_track_record(buys, date(2025, 1, 1), close_series_fn=fn,
                                 horizon_days=2, min_history=3)
    assert stats.n_scored == 1
    assert stats.tier == InsiderTier.UNRATED.value


# ---- classify_insider composition (no network) -------------------------


def test_classify_insider_no_history_unrated():
    stats = classify_insider(
        "0001977231", date(2026, 6, 15),
        close_series_fn=_fn({}),
        _filings_factory=lambda cik: [],   # no filings
    )
    assert stats.insider_cik == "0001977231"
    assert stats.n_prior_buys == 0
    assert stats.tier == InsiderTier.UNRATED.value


def test_fetch_insider_history_never_raises_on_factory_error():
    def boom(cik):
        raise RuntimeError("edgar down")
    assert fetch_insider_history("123", _filings_factory=boom) == []
