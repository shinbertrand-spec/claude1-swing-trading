"""Integration test for tools.auto_paper.run_entry — full 3-phase cycle.

Per §4.2 of [auto-paper LLM/Python boundary refactor 2026-05-28]:

Drives a full init → post_skeptic → post_panel cycle with:
- Mocked broker (Tiger client returns canned account summary + place_limit_buy)
- Mocked scanner (returns 2 synthetic candidates)
- Mocked screener (passes both)
- Mocked yfinance industry lookup
- Synthetic skeptic + critic envelope JSONs written between phases

Verifies:
- Run directory exists at expected path
- _status.yml reflects phase-by-phase completion with timestamps
- 07_placement_results.yml is consistent with input candidates × panel verdicts
- All 7 expected artifacts written per §2
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tools.auto_paper import run_entry, state
from tools.auto_paper.critic_panel import CriticVote
from tools.auto_paper.screener import CheckResult, ScreenerResult
from tools.contract import TraceEntry


# --------------------------------------------------------- fixtures


@dataclass
class _FakeCand:
    ticker: str
    setup_type: str = "xs_short_term_reversal_liquid_us"
    setup_grade: str = "B"
    pivot_price: float = 100.0
    limit_price: float = 100.1
    stop_price: float = 92.0
    target_price: float = 116.0
    shares: int = 500
    sector_etf: str = "XLV"


@dataclass
class _FakeReport:
    candidates: list[_FakeCand]


def _ok_screener(ticker: str, sector: str) -> ScreenerResult:
    return ScreenerResult(
        ticker=ticker, blocked=False, blocking_checks=[],
        corrected_sector_etf=None,
        checks=[
            CheckResult(check="litigation", passed=True, reason="ok",
                        evidence={"headlines": []}),
            CheckResult(check="dilution", passed=True, reason="ok",
                        evidence={"offerings": []}),
            CheckResult(check="earnings_blackout", passed=True, reason="ok",
                        evidence={"next_earnings_date": "2026-08-15",
                                  "trading_days_to_earnings": 55}),
            CheckResult(check="sector_correction", passed=True, reason="ok",
                        evidence={"yfinance_sector": sector}),
        ],
        computed_at="2026-05-29T00:00:00+00:00",
    )


def _placed_result(ticker: str) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker, status="placed",
        reason=None, broker_order_id=1_000_000 + hash(ticker) % 1_000_000,
        ledger_path=f"ledgers/paper-auto/{ticker}.yml",
    )


@pytest.fixture
def _harness(tmp_path, monkeypatch):
    """Isolated tmp dirs + stubbed network calls. Returns a small ns of helpers."""
    run_root = tmp_path / "ledgers" / "_auto_paper_runs"
    panel_dir = tmp_path / "ledgers" / "swing-critics"
    cand_dir = tmp_path / "ledgers" / "candidates"
    monkeypatch.setattr(run_entry, "RUN_ROOT", run_root)

    from tools.auto_paper import critic_panel
    monkeypatch.setattr(critic_panel, "_PANEL_LEDGER_DIR", panel_dir)
    monkeypatch.setattr(critic_panel, "_CALIBRATION_DIR", panel_dir / "_calibration")
    from tools.auto_paper import shell_ledger
    monkeypatch.setattr(shell_ledger, "_CANDIDATES_DIR", cand_dir)

    monkeypatch.setattr(run_entry, "TigerClient",
        lambda *a, **kw: SimpleNamespace(
            account_summary=lambda: SimpleNamespace(
                output={"net_liquidation": 1_000_000.0, "cash": 750_000.0}
            ),
        ))

    from tools import trend_template
    monkeypatch.setattr(
        trend_template, "compute_from_ticker",
        lambda ticker, include_rs=False, **kw: SimpleNamespace(
            output={"trend_template_passes": 7}
        ),
    )
    from tools import atr_compute
    monkeypatch.setattr(
        atr_compute, "compute_from_ticker",
        lambda ticker, period=14: TraceEntry(
            tool="tools/atr_compute.py",
            inputs={"ticker": ticker, "period": period},
            output={"atr": 4.0, "adr_pct": 4.0, "rows": 60},
        ),
    )
    from tools import regime_check
    monkeypatch.setattr(regime_check, "compute",
        lambda candidate_ticker, sector_etf=None, **kw: TraceEntry(
            tool="tools/regime_check.py",
            inputs={"candidate_ticker": candidate_ticker, "sector_etf": sector_etf},
            output={
                "broad_market": {"ticker": "SPY", "trend_template_passes": 7,
                                 "stage_class": "stage_2_confirmed"},
                "sector": {"ticker": sector_etf, "trend_template_passes": 6,
                           "stage_class": "stage_2_confirmed", "qualifies_for_long": True},
                "candidate": {"ticker": candidate_ticker,
                              "trend_template_passes": 6,
                              "stage_class": "stage_2_confirmed"},
                "regime_multiplier": 1.0,
                "candidate_qualifies_for_entry": True,
                "circuit_breaker_stage_4": False,
            },
        ),
    )

    pos_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    pos_json.parent.mkdir(parents=True, exist_ok=True)
    pos_json.write_text('{"positions": []}', encoding="utf-8")
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(pos_json))

    return SimpleNamespace(tmp_path=tmp_path, run_root=run_root,
                           cand_dir=cand_dir, panel_dir=panel_dir)


def _write_skeptic_envelopes(run_dir, tickers, verdict="WEAK"):
    env_dir = run_dir / "04_skeptic_envelopes"
    env_dir.mkdir(parents=True, exist_ok=True)
    for t in tickers:
        with open(env_dir / f"{t}.json", "w", encoding="utf-8") as fh:
            json.dump({"ticker": t, "verdict": verdict,
                       "risk_triggers": [], "bull_counterpoints": []}, fh)


def _write_panel_envelopes(run_dir, ticker, votes: dict[str, str]):
    env_dir = run_dir / "06_panel_envelopes" / ticker
    env_dir.mkdir(parents=True, exist_ok=True)
    for critic, adj in votes.items():
        critic_field = critic.replace("-", "_")
        v = CriticVote(
            critic=critic_field, candidate_ticker=ticker,
            panel_call_id=f"{run_dir.name}__{ticker}",
            panel_firing_date="2026-05-29",
            risks=[] if adj == "hold" else [
                {"risk": "test", "severity": "high", "grounding_evidence": "e"}
            ],
            confidence_adjustment=adj, adjustment_rationale="test",
            estimated_cost_usd=0.05,
        )
        with open(env_dir / f"{critic}.json", "w", encoding="utf-8") as fh:
            json.dump(v.to_dict(), fh)


# =============================================================== integration


def test_full_cycle_init_post_skeptic_post_panel(_harness, monkeypatch, capsys):
    """End-to-end init → post_skeptic → post_panel against mocked broker."""
    # 1. Stub scanner + screener + industry
    monkeypatch.setattr(
        run_entry, "scan_today",
        lambda **kw: [_FakeReport(candidates=[
            _FakeCand(ticker="ALPHA", sector_etf="XLV"),
            _FakeCand(ticker="BETA",  sector_etf="XLY"),
        ])],
    )
    monkeypatch.setattr(
        run_entry.screener_mod, "screen",
        lambda t, claimed_sector_etf=None: _ok_screener(t, claimed_sector_etf or "XLV"),
    )
    monkeypatch.setattr(run_entry, "_fetch_industry", lambda t: "Test Industry")

    # 2. Phase init
    run_dir = _harness.run_root / "2026-05-29T22-30-00"
    rc = run_entry.phase_init(run_dir)
    assert rc == 0

    # Required artifacts
    for f in ("00_candidates.yml", "01_screener.yml", "03_skeptic_invocations.yml",
              "_status.yml"):
        assert (run_dir / f).exists(), f"missing {f}"
    assert (run_dir / "02_shell_ledgers" / "ALPHA.yml").exists()
    assert (run_dir / "02_shell_ledgers" / "BETA.yml").exists()
    assert (run_dir / "04_skeptic_envelopes").is_dir()
    assert (run_dir / "06_panel_envelopes").is_dir()

    status = state.read_run_status(run_dir)
    assert status["last_phase_completed"] == "init"
    assert status["run_started_at"] is not None

    # 3. LLM simulates writing skeptic envelopes
    _write_skeptic_envelopes(run_dir, ["ALPHA", "BETA"], verdict="WEAK")

    # 4. Phase post_skeptic
    rc = run_entry.phase_post_skeptic(run_dir)
    assert rc == 0
    assert (run_dir / "05_panel_invocations.yml").exists()
    status = state.read_run_status(run_dir)
    assert status["last_phase_completed"] == "post_skeptic"

    panel_invs = yaml.safe_load((run_dir / "05_panel_invocations.yml").read_text())
    assert len(panel_invs["invocations"]) == 2
    # Non-semi tickers → 4 critics each
    for inv in panel_invs["invocations"]:
        assert set(inv["critics_to_fire"]) == {
            "risk-manager", "setup-quality-hawk", "macro-skeptic", "quant-insight"
        }

    # 5. LLM writes panel envelopes — ALPHA = clean hold, BETA = defer
    _write_panel_envelopes(run_dir, "ALPHA", {
        "risk-manager": "hold", "setup-quality-hawk": "hold",
        "macro-skeptic": "hold", "quant-insight": "hold",
    })
    _write_panel_envelopes(run_dir, "BETA", {
        "risk-manager": "structural_risk", "setup-quality-hawk": "minus_50",
        "macro-skeptic": "hold", "quant-insight": "hold",
    })

    # 6. Phase post_panel — ALPHA places, BETA defers
    place_calls: list[Any] = []
    def _fake_place(cand, *, client, dry_run, apply_panel_sizing, auto_paper_run_dir, already_regime_sized=False):
        place_calls.append((cand.ticker, cand.shares, str(auto_paper_run_dir)))
        return _placed_result(cand.ticker)

    rc = run_entry.phase_post_panel(
        run_dir, place_fn=_fake_place, client_factory=lambda: object(),
    )
    assert rc == 0

    # ALPHA placed; BETA never called place_candidate (defer)
    assert len(place_calls) == 1
    assert place_calls[0][0] == "ALPHA"
    assert place_calls[0][1] == 500   # shadow mode keeps full size
    assert place_calls[0][2] == str(run_dir)

    # 7. Inspect 07_placement_results.yml
    results = yaml.safe_load((run_dir / "07_placement_results.yml").read_text())
    assert results["n_placed"] == 1
    by_ticker = {r["ticker"]: r for r in results["results"]}
    assert by_ticker["ALPHA"]["status"] == "placed"
    assert by_ticker["ALPHA"]["placed"] is True
    assert by_ticker["BETA"]["status"] == "defer"
    assert by_ticker["BETA"]["placed"] is False
    assert by_ticker["BETA"]["sizing_multiplier"] == 0.0

    # 8. Final status reflects post_panel completion
    status = state.read_run_status(run_dir)
    assert status["last_phase_completed"] == "post_panel"
    phases = {p["phase"]: p for p in status["phases_completed"]}
    assert {"init", "post_skeptic", "post_panel"} <= set(phases.keys())
    assert phases["post_panel"]["candidates_placed"] == 1

    # 9. Output markers present
    out = capsys.readouterr().out
    assert "PHASE_INIT_OK" in out
    assert "PHASE_POST_SKEPTIC_OK" in out
    assert "PHASE_POST_PANEL_OK" in out
    assert "PLACE: ALPHA placed" in out
    assert "PLACE: BETA defer" in out


def test_empty_universe_completes_each_phase_cleanly(_harness, monkeypatch, capsys):
    """If scanner returns no candidates, phases short-circuit + exit 0."""
    monkeypatch.setattr(
        run_entry, "scan_today",
        lambda **kw: [_FakeReport(candidates=[])],
    )
    monkeypatch.setattr(run_entry.screener_mod, "screen",
                        lambda t, claimed_sector_etf=None: _ok_screener(t, "XLV"))
    monkeypatch.setattr(run_entry, "_fetch_industry", lambda t: "")

    run_dir = _harness.run_root / "empty-test"

    assert run_entry.phase_init(run_dir) == 0
    assert run_entry.phase_post_skeptic(run_dir) == 0
    assert run_entry.phase_post_panel(
        run_dir, place_fn=lambda *a, **kw: pytest.fail("no place"),
        client_factory=lambda: object(),
    ) == 0

    results = yaml.safe_load((run_dir / "07_placement_results.yml").read_text())
    assert results["n_placed"] == 0
    out = capsys.readouterr().out
    assert "PHASE_INIT_OK" in out
    assert "PHASE_POST_PANEL_OK placed=0" in out
