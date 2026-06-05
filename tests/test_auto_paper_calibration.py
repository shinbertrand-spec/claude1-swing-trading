"""Tests for the Phase-3 calibration loop closure:
record_calibration_outcome (write-back) + calibration_analysis (join + flip gate).
"""
from __future__ import annotations

import json

from tools.auto_paper import calibration_analysis as ca
from tools.auto_paper.critic_panel import record_calibration_outcome


def _write_panel(panel_dir, entry_date, ticker, *, action, multiplier, call_id):
    d = panel_dir / entry_date / ticker
    d.mkdir(parents=True, exist_ok=True)
    (d / "_panel.json").write_text(json.dumps({
        "ticker": ticker, "action": action,
        "sizing_multiplier": multiplier, "panel_call_id": call_id,
    }))


def _read_cal(panel_dir, day):
    p = panel_dir / "_calibration" / f"{day}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ----------------------------------------------------- record_calibration_outcome


def test_outcome_joins_verdict_and_computes_r(tmp_path):
    _write_panel(tmp_path, "2026-06-05", "NFLX",
                 action="half_size_review", multiplier=0.5, call_id="RID__NFLX")
    record_calibration_outcome(
        "NFLX", entry_price=100.0, exit_price=110.0, stop_price=90.0,
        shares=10, exit_reason="target", entry_date="2026-06-05",
        closed_at="2026-06-06T00:00:00+00:00", panel_dir=tmp_path,
    )
    rows = _read_cal(tmp_path, "2026-06-06")
    assert len(rows) == 1
    r = rows[0]
    assert r["record_type"] == "outcome"
    assert r["ticker"] == "NFLX"
    assert r["panel_call_id"] == "RID__NFLX"
    assert r["verdict_action"] == "half_size_review"
    assert r["sizing_multiplier"] == 0.5
    assert r["realized_pnl"] == 100.0          # (110-100)*10
    assert r["realized_r"] == 1.0              # (110-100)/(100-90)


def test_outcome_loss_negative_r(tmp_path):
    _write_panel(tmp_path, "2026-06-05", "ABC",
                 action="preserve", multiplier=1.0, call_id="RID__ABC")
    record_calibration_outcome(
        "ABC", entry_price=100.0, exit_price=95.0, stop_price=90.0,
        shares=5, exit_reason="stop", entry_date="2026-06-05",
        closed_at="2026-06-06T00:00:00+00:00", panel_dir=tmp_path,
    )
    r = _read_cal(tmp_path, "2026-06-06")[0]
    assert r["realized_pnl"] == -25.0          # (95-100)*5
    assert r["realized_r"] == -0.5             # (95-100)/(100-90)


def test_outcome_without_verdict_still_records(tmp_path):
    # No _panel.json written -> verdict fields null, but P&L is never lost.
    record_calibration_outcome(
        "XYZ", entry_price=50.0, exit_price=55.0, stop_price=47.0,
        shares=10, exit_reason="target", entry_date="2026-06-05",
        closed_at="2026-06-06T00:00:00+00:00", panel_dir=tmp_path,
    )
    r = _read_cal(tmp_path, "2026-06-06")[0]
    assert r["panel_call_id"] is None
    assert r["verdict_action"] is None
    assert r["realized_pnl"] == 50.0


def test_outcome_zero_risk_r_is_none(tmp_path):
    # stop == entry -> risk_per_share 0 -> realized_r None (no division).
    record_calibration_outcome(
        "ZRO", entry_price=100.0, exit_price=105.0, stop_price=100.0,
        shares=1, exit_reason="target", entry_date="2026-06-05",
        closed_at="2026-06-06T00:00:00+00:00", panel_dir=tmp_path,
    )
    r = _read_cal(tmp_path, "2026-06-06")[0]
    assert r["realized_r"] is None
    assert r["realized_pnl"] == 5.0


# ----------------------------------------------------- calibration_analysis


def _seed(cal_dir, *, verdicts, outcomes):
    cal_dir.mkdir(parents=True, exist_ok=True)
    with open(cal_dir / "seed.jsonl", "w") as fh:
        for v in verdicts:
            fh.write(json.dumps(v) + "\n")
        for o in outcomes:
            o = {"record_type": "outcome", **o}
            fh.write(json.dumps(o) + "\n")


def test_analysis_insufficient_data(tmp_path):
    cal = tmp_path / "_calibration"
    _seed(cal,
          verdicts=[{"panel_call_id": "c1", "action": "preserve"}],
          outcomes=[{"panel_call_id": "c1", "realized_r": 1.0, "realized_pnl": 100}])
    rep = ca.compute(cal)
    assert rep.n_joined == 1
    assert rep.ready_to_flip is False
    assert "INSUFFICIENT DATA" in rep.discrimination


def test_analysis_panel_discriminates(tmp_path):
    cal = tmp_path / "_calibration"
    verdicts, outcomes = [], []
    # 12 preserve winners (avg R +1.5), 12 half_size_review losers (avg R -0.5)
    for i in range(12):
        verdicts.append({"panel_call_id": f"p{i}", "action": "preserve"})
        outcomes.append({"panel_call_id": f"p{i}", "realized_r": 1.5, "realized_pnl": 150})
        verdicts.append({"panel_call_id": f"h{i}", "action": "half_size_review"})
        outcomes.append({"panel_call_id": f"h{i}", "realized_r": -0.5, "realized_pnl": -50})
    _seed(cal, verdicts=verdicts, outcomes=outcomes)
    rep = ca.compute(cal)
    assert rep.n_joined == 24
    assert rep.by_action["preserve"].avg_realized_r == 1.5
    assert rep.by_action["preserve"].win_rate == 1.0
    assert rep.by_action["half_size_review"].avg_realized_r == -0.5
    assert rep.ready_to_flip is True
    assert "DISCRIMINATES" in rep.discrimination


def test_analysis_no_discrimination_blocks_flip(tmp_path):
    cal = tmp_path / "_calibration"
    verdicts, outcomes = [], []
    # preserve LOSES, half_size_review WINS -> panel inverted -> do not flip
    for i in range(12):
        verdicts.append({"panel_call_id": f"p{i}", "action": "preserve"})
        outcomes.append({"panel_call_id": f"p{i}", "realized_r": -0.3, "realized_pnl": -30})
        verdicts.append({"panel_call_id": f"h{i}", "action": "half_size_review"})
        outcomes.append({"panel_call_id": f"h{i}", "realized_r": 0.8, "realized_pnl": 80})
    _seed(cal, verdicts=verdicts, outcomes=outcomes)
    rep = ca.compute(cal)
    assert rep.n_joined == 24
    assert rep.ready_to_flip is False
    assert "DOES NOT DISCRIMINATE" in rep.discrimination


def test_analysis_unmatched_outcome_counted(tmp_path):
    cal = tmp_path / "_calibration"
    _seed(cal, verdicts=[],
          outcomes=[{"panel_call_id": None, "verdict_action": None,
                     "realized_r": 1.0, "realized_pnl": 10}])
    rep = ca.compute(cal)
    assert rep.n_unmatched_outcomes == 1
    assert rep.n_joined == 0


def test_analysis_fallback_to_outcome_verdict_action(tmp_path):
    # Outcome has no matching verdict record, but carries verdict_action itself.
    cal = tmp_path / "_calibration"
    _seed(cal, verdicts=[],
          outcomes=[{"panel_call_id": "x", "verdict_action": "preserve",
                     "realized_r": 1.0, "realized_pnl": 10}])
    rep = ca.compute(cal)
    assert rep.n_joined == 1
    assert "preserve" in rep.by_action
