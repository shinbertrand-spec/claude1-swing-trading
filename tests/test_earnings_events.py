"""Tests for the earnings-events pipeline (frozen filter + §3b session alignment).

The three Berkman-Truong alignment cases are pinned here per spec §3b:
"Unit tests must pin all three cases" — BMO, AMC, unknown.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tools.fundamentals.earnings_events import (
    EarningsEvent,
    acceptance_to_et,
    classify_timing,
    compute_ear,
    extract_raw_events,
    is_earnings_filing,
    load_events,
    rank_and_history,
    write_events,
    RANK_MIN_COMPARATORS,
)

ET = ZoneInfo("America/New_York")


# ---- frozen filter (strict 2.02 — committed before the grid) -------------


def test_filter_accepts_8k_with_202():
    assert is_earnings_filing("8-K", "2.02,9.01")
    assert is_earnings_filing("8-K/A", "2.02")


def test_filter_rejects_untagged_earnings_shapes():
    # The PTC/MOS/URBN class: genuine earnings 8-Ks tagged without 2.02.
    # The FROZEN filter deliberately does NOT widen to catch these.
    assert not is_earnings_filing("8-K", "9.01")
    assert not is_earnings_filing("8-K", "8.01,9.01")
    assert not is_earnings_filing("8-K", "")


def test_filter_rejects_other_forms():
    assert not is_earnings_filing("10-Q", "2.02")
    assert not is_earnings_filing("6-K", "2.02")


# ---- timing classification ----------------------------------------------


def test_timing_boundaries():
    mk = lambda h, m: datetime(2025, 4, 30, h, m, tzinfo=ET)
    assert classify_timing(mk(9, 29)) == "bmo"
    assert classify_timing(mk(9, 30)) == "unknown"
    assert classify_timing(mk(15, 59)) == "unknown"
    assert classify_timing(mk(16, 0)) == "amc"
    assert classify_timing(mk(6, 45)) == "bmo"


def test_acceptance_utc_to_et():
    # 20:05 UTC during DST = 16:05 ET -> AMC
    dt = acceptance_to_et("2025-04-30T20:05:12.000Z")
    assert dt.hour == 16 and dt.minute == 5
    assert classify_timing(dt) == "amc"


# ---- amendment collapse --------------------------------------------------


def _sub_with(filings):
    """Fake submissions doc: list of (form, items, acceptanceDateTime)."""
    return {"filings": {"recent": {
        "form": [f for f, _, _ in filings],
        "items": [i for _, i, _ in filings],
        "acceptanceDateTime": [a for _, _, a in filings],
        "filingDate": [a[:10] for _, _, a in filings],
    }}}


def test_collapse_within_3_days_keeps_earliest():
    sub = _sub_with([
        ("8-K", "2.02,9.01", "2024-05-01T20:10:00.000Z"),
        ("8-K/A", "2.02", "2024-05-03T14:00:00.000Z"),      # amendment 2d later
        ("8-K", "2.02", "2024-08-01T20:10:00.000Z"),         # next quarter
    ])
    raws = extract_raw_events(sub, "ABC")
    assert [r["announce_date"] for r in raws] == [date(2024, 5, 1), date(2024, 8, 1)]


# ---- §3b session alignment (THE three pinned cases) ----------------------

# trading days Mon 2024-06-03 .. Fri 2024-06-07 (+ Mon 2024-06-10)
DATES = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5),
         date(2024, 6, 6), date(2024, 6, 7), date(2024, 6, 10)]
CLOSES = [100.0, 102.0, 105.0, 103.0, 106.0, 110.0]
SPY_CLOSES = [500.0, 500.0, 505.0, 505.0, 505.0, 505.0]


def _ear(timing, announce):
    raw = {"announce_date": announce, "timing": timing}
    return compute_ear(raw, DATES, CLOSES, DATES, SPY_CLOSES)


def test_bmo_reaction_is_announce_day():
    # BMO Wed 06-05: reaction = Wed; ear = close(Tue)->close(Wed) minus SPY same
    rd, ear = _ear("bmo", date(2024, 6, 5))
    assert rd == date(2024, 6, 5)
    assert ear == pytest.approx((105.0 / 102.0 - 1) - (505.0 / 500.0 - 1))


def test_amc_reaction_is_next_day():
    # AMC Wed 06-05: reaction = Thu; ear = close(Wed)->close(Thu) minus SPY same
    rd, ear = _ear("amc", date(2024, 6, 5))
    assert rd == date(2024, 6, 6)
    assert ear == pytest.approx((103.0 / 105.0 - 1) - (505.0 / 505.0 - 1))


def test_unknown_treated_as_amc():
    assert _ear("unknown", date(2024, 6, 5)) == _ear("amc", date(2024, 6, 5))


def test_amc_friday_reaction_is_monday():
    rd, ear = _ear("amc", date(2024, 6, 7))
    assert rd == date(2024, 6, 10)
    assert ear == pytest.approx((110.0 / 106.0 - 1) - 0.0)


def test_weekend_acceptance_falls_to_next_session():
    # Saturday acceptance (late-Friday release): last trading day <= Sat is Fri,
    # not equal to announce -> AMC-style close(Fri)->close(Mon)
    rd, ear = _ear("bmo", date(2024, 6, 8))
    assert rd == date(2024, 6, 10)
    assert ear == pytest.approx((110.0 / 106.0 - 1) - 0.0)


def test_never_enters_reaction_window_it_cannot_know():
    # AMC on the last available bar: no T+1 close exists -> None (never fabricate)
    assert _ear("amc", date(2024, 6, 10)) is None


# ---- ranking (strictly-prior, trailing window) + history ------------------


def _mk_event(ticker, rd, ear):
    return EarningsEvent(ticker=ticker, announce_date=rd, timing="amc",
                         reaction_date=rd, ear=ear, ear_rank_pct=None,
                         prior_pos_ear_count=None)


def test_rank_requires_min_comparators():
    events = [_mk_event(f"T{i}", f"2024-03-{(i % 28) + 1:02d}", 0.01 * i)
              for i in range(RANK_MIN_COMPARATORS - 5)]
    rank_and_history(events)
    assert all(e.ear_rank_pct is None for e in events)


def test_rank_is_strictly_prior_percentile():
    # 40 comparators on earlier days, then one event that beats 30 of them
    events = [_mk_event(f"C{i}", "2024-03-01", (i - 20) / 1000.0) for i in range(40)]
    target = _mk_event("TGT", "2024-03-15", 0.0095)   # beats i<30 (ear<0.0095... i-20<9.5 -> i<=29)
    events.append(target)
    rank_and_history(events)
    assert target.ear_rank_pct == pytest.approx(30 / 40 * 100.0)
    # comparators on the SAME day never rank against each other (strictly prior)
    assert all(e.ear_rank_pct is None for e in events if e.ticker != "TGT")


def test_history_counts_last_four_observed():
    evs = [_mk_event("AAA", f"2024-0{m}-01", ear) for m, ear in
           [(1, 0.02), (2, -0.01), (3, 0.03), (4, 0.01), (5, 0.02), (6, -0.02)]]
    rank_and_history(evs)
    by_date = sorted(evs, key=lambda e: e.reaction_date)
    assert by_date[0].prior_pos_ear_count == 0          # first event: no priors
    assert by_date[4].prior_pos_ear_count == 3          # of (+,-,+,+) -> 3
    assert by_date[5].prior_pos_ear_count == 3          # of (-,+,+,+) -> 3


# ---- persistence roundtrip ----------------------------------------------


def test_write_then_load_roundtrip(tmp_path):
    path = tmp_path / "events.yml"
    events = [
        EarningsEvent("ABC", "2024-05-01", "amc", "2024-05-02", 0.031, 92.5, 3),
        EarningsEvent("XYZ", "2024-05-01", "bmo", "2024-05-01", -0.01, None, 0),
    ]
    write_events(path, events, meta={"universe": "test"})
    loaded = load_events(path)
    assert set(loaded.keys()) == {"ABC", "XYZ"}
    assert loaded["ABC"][0].ear == pytest.approx(0.031)
    assert loaded["ABC"][0].ear_rank_pct == pytest.approx(92.5)
    assert loaded["XYZ"][0].ear_rank_pct is None


def test_load_filters_universe(tmp_path):
    path = tmp_path / "events.yml"
    write_events(path, [
        EarningsEvent("INU", "2024-05-01", "amc", "2024-05-02", 0.02, 90.0, 2),
        EarningsEvent("OUT", "2024-05-01", "amc", "2024-05-02", 0.02, 90.0, 2),
    ], meta={})
    loaded = load_events(path, universe={"INU"})
    assert set(loaded.keys()) == {"INU"}
