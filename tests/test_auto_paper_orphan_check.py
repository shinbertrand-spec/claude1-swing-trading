"""Tests for tools.auto_paper.orphan_check (Mode A / Mode B detection core)."""
import textwrap

from tools.auto_paper import orphan_check as oc


def _write(dirpath, ticker, state="starter", *, raw=None):
    p = dirpath / f"{ticker}.yml"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
        return p
    p.write_text(textwrap.dedent(f"""
        meta:
          schema_version: "1.0"
          ticker: {ticker}
          state: {state}
          account_track: paper-auto
        position_state:
          stage: STARTER
    """).lstrip(), encoding="utf-8")
    return p


def test_scan_groups_by_state(tmp_path):
    _write(tmp_path, "GO", "starter")
    _write(tmp_path, "MO", "starter")
    _write(tmp_path, "MXL", "closed")
    _write(tmp_path, "COIN", "pending_close")
    scan = oc.scan_ledgers(str(tmp_path))
    assert scan.starter == {"GO", "MO"}
    assert scan.tickers_in("closed", "pending_close") == {"MXL", "COIN"}
    assert scan.corrupt == []


def test_corrupt_ledger_isolated_not_starter(tmp_path):
    _write(tmp_path, "GO", "starter")
    # broken: bare sequence after a mapping key (the NVDA-2026-06-06 failure shape)
    _write(tmp_path, "BAD", raw="meta:\n  state: starter\nnotes: >\n  hi\n- id: 12\n  x: 1\n")
    scan = oc.scan_ledgers(str(tmp_path))
    assert scan.starter == {"GO"}
    assert len(scan.corrupt) == 1
    assert scan.corrupt[0][0].endswith("BAD.yml")
    assert "BAD" not in scan.docs


def test_orphan_when_held_without_starter(tmp_path):
    _write(tmp_path, "GO", "starter")
    rep = oc.compute_orphans({"GO": 100, "ZZZ": 50}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == ["ZZZ"]
    assert rep.starter_tickers == ["GO"]
    assert rep.protect_set == ["GO"]          # dynamic PROTECT == starter
    assert rep.is_clean is False


def test_no_orphan_when_all_held_are_starter(tmp_path):
    _write(tmp_path, "GO", "starter")
    _write(tmp_path, "MO", "starter")
    rep = oc.compute_orphans({"GO": 100, "MO": 50}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == []
    assert rep.is_clean is True


def test_short_position_is_short_anomaly_not_orphan(tmp_path):
    # Long-only invariant: a broker SHORT is NEVER an orphan-long (that was the
    # 2026-07 NFLX naked-short doubling bug — a short read as abs() long shares).
    # It is surfaced in short_set and makes is_clean False.
    _write(tmp_path, "GO", "starter")
    rep = oc.compute_orphans({"GO": 100, "COIN": -213}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == []
    assert rep.short_set == ["COIN"]
    assert rep.is_clean is False


def test_closed_ledger_not_protecting_its_broker_position(tmp_path):
    # A closed ledger does NOT make a still-held broker position safe -> orphan.
    _write(tmp_path, "MXL", "closed")
    rep = oc.compute_orphans({"MXL": 503}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == ["MXL"]


def test_sub_one_share_ignored(tmp_path):
    _write(tmp_path, "GO", "starter")
    rep = oc.compute_orphans({"GO": 100, "FRAC": 0.4}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == []


def test_stuck_closing_candidates(tmp_path):
    _write(tmp_path, "GO", "starter")
    _write(tmp_path, "MXL", "closed")
    _write(tmp_path, "COIN", "pending_close")
    _write(tmp_path, "WMT", "closed")
    holdings = {"GO": 100, "MXL": 503, "COIN": -213}  # WMT closed+flat; COIN SHORT
    stuck = oc.stuck_closing_candidates(holdings, scan=oc.scan_ledgers(str(tmp_path)))
    assert stuck == ["MXL"]   # COIN (short) is a short_anomaly, never a stuck LONG to flip


def test_corrupt_ledger_blocks_is_clean(tmp_path):
    _write(tmp_path, "GO", "starter")
    _write(tmp_path, "BAD", raw="meta:\n  state: starter\n: : :\n")
    rep = oc.compute_orphans({"GO": 100}, scan=oc.scan_ledgers(str(tmp_path)))
    assert rep.orphan_set == []
    assert rep.corrupt_ledgers          # non-empty
    assert rep.is_clean is False


def test_classify_holdings_buckets_every_category(tmp_path):
    _write(tmp_path, "GO", "starter")          # healthy
    _write(tmp_path, "MXL", "closed")          # stuck (Mode A)
    _write(tmp_path, "COIN", "pending_close")  # SHORT held -> short_anomaly (NOT stuck)
    _write(tmp_path, "SUB", "submitted")       # submitted-held (reconcile_today's job)
    _write(tmp_path, "BAD", raw="meta:\n  state: starter\nx:\n- a\n: b\n")  # corrupt
    holdings = {"GO": 100, "MXL": 503, "COIN": -213, "SUB": 10, "BAD": 5, "GKOS": 338}
    cls = oc.classify_holdings(holdings, scan=oc.scan_ledgers(str(tmp_path)))
    assert cls.healthy == ["GO"]
    assert cls.stuck_closing == ["MXL"]         # COIN is SHORT -> short_anomaly, not stuck
    assert cls.short_anomaly == ["COIN"]        # long-only breach: gated, never flipped
    assert cls.submitted_held == ["SUB"]
    assert cls.corrupt_held == ["BAD"]
    assert cls.orphans == ["GKOS"]             # no ledger file at all (Mode B); a LONG


def test_classify_stuck_distinct_from_orphan(tmp_path):
    # A held closed ledger is STUCK, not an orphan -- the distinction Step 2's
    # compute_orphans deliberately lumped together.
    _write(tmp_path, "MXL", "closed")
    cls = oc.classify_holdings({"MXL": 503}, scan=oc.scan_ledgers(str(tmp_path)))
    assert cls.stuck_closing == ["MXL"]
    assert cls.orphans == []
