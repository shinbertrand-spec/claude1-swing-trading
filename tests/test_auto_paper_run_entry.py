"""Unit tests for tools.auto_paper.run_entry — 3-phase orchestrator.

Per §4.1 of [auto-paper LLM/Python boundary refactor 2026-05-28]:
- test_phase_init_writes_all_expected_artifacts
- test_phase_post_skeptic_raises_on_missing_envelope
- test_phase_post_panel_raises_on_all_critics_missing_for_ticker
- test_phase_post_panel_aggregates_partial_votes_when_some_critics_present
- test_out_of_order_phase_raises
- test_run_status_yaml_round_trips

Plus orchestrator-internal helpers: _build_skeptic_prompt, _new_run_dir,
_latest_run_dir, _require_prerequisite_phase.

Strategy: mock the broker, scanner, screener, and yfinance; write canned
envelope JSON files between phases to simulate the LLM steps.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tools.auto_paper import run_entry, state
from tools.auto_paper.critic_panel import CriticVote
from tools.auto_paper.errors import (
    MissingEnvelopeError,
    OutOfOrderPhaseError,
    RunDirCorruptError,
)
from tools.auto_paper.screener import ScreenerResult


# ---------------------------------------------------------------- canned data


@dataclass
class _FakeScanCand:
    """Minimal duck for quant_scanner's CandidateInput shape."""
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
class _FakeScanReport:
    candidates: list[_FakeScanCand]


def _ok_screener_result(ticker: str, sector="XLV", *, blocked=False) -> ScreenerResult:
    """Build a passing ScreenerResult with one earnings check."""
    from tools.auto_paper.screener import CheckResult
    checks = [
        CheckResult(check="litigation", passed=True, reason="ok",
                    evidence={"headlines": []}),
        CheckResult(check="dilution", passed=True, reason="ok",
                    evidence={"offerings": []}),
        CheckResult(check="earnings_blackout", passed=True, reason="ok",
                    evidence={"next_earnings_date": "2026-08-15"}),
        CheckResult(check="sector_correction", passed=True, reason="ok",
                    evidence={"yfinance_sector": sector}),
    ]
    return ScreenerResult(
        ticker=ticker, blocked=blocked,
        blocking_checks=([] if not blocked else ["litigation"]),
        corrected_sector_etf=None,
        checks=checks,
        computed_at="2026-05-29T00:00:00+00:00",
    )


def _fake_tiger_client(positions=None):
    """Stand-in TigerClient with just what run_entry's init phase reads.

    ``positions`` feeds the pre-session orphan sweep (Priority 2); default empty
    so the sweep is a clean no-op.
    """
    return SimpleNamespace(
        account_summary=lambda: SimpleNamespace(
            output={"net_liquidation": 1_000_000.0, "cash": 750_000.0}
        ),
        positions=lambda: SimpleNamespace(
            output={"positions": positions or []}
        ),
    )


@pytest.fixture
def _isolated_run_root(tmp_path, monkeypatch):
    """Redirect run-root, panel ledger dir, and candidate dir into tmp."""
    run_root = tmp_path / "ledgers" / "_auto_paper_runs"
    panel_dir = tmp_path / "ledgers" / "swing-critics"
    cand_dir = tmp_path / "ledgers" / "candidates"
    monkeypatch.setattr(run_entry, "RUN_ROOT", run_root)

    from tools.auto_paper import critic_panel
    monkeypatch.setattr(critic_panel, "_PANEL_LEDGER_DIR", panel_dir)
    monkeypatch.setattr(critic_panel, "_CALIBRATION_DIR", panel_dir / "_calibration")

    from tools.auto_paper import shell_ledger
    monkeypatch.setattr(shell_ledger, "_CANDIDATES_DIR", cand_dir)

    # Stub TigerClient + trend_template.compute_from_ticker so init runs without network
    monkeypatch.setattr(run_entry, "TigerClient", lambda *a, **kw: _fake_tiger_client())

    from tools import trend_template
    monkeypatch.setattr(
        trend_template,
        "compute_from_ticker",
        lambda ticker, include_rs=False, **kw: SimpleNamespace(
            output={"trend_template_passes": 7}
        ),
    )

    # Stub atr_compute so we don't fetch OHLCV. Real TraceEntry needed so
    # shell_ledger.build_quant_shell_ledger can call .to_dict() on it.
    from tools import atr_compute
    from tools.contract import TraceEntry
    monkeypatch.setattr(
        atr_compute,
        "compute_from_ticker",
        lambda ticker, period=14: TraceEntry(
            tool="tools/atr_compute.py",
            inputs={"ticker": ticker, "period": period},
            output={"atr": 4.0, "adr_pct": 4.0, "rows": 60},
        ),
    )
    # shell_ledger also calls regime_check.compute internally; stub it.
    from tools import regime_check
    monkeypatch.setattr(
        regime_check, "compute",
        lambda candidate_ticker, sector_etf=None, **kw: TraceEntry(
            tool="tools/regime_check.py",
            inputs={"candidate_ticker": candidate_ticker, "sector_etf": sector_etf},
            output={
                "broad_market": {"ticker": "SPY", "trend_template_passes": 7, "stage_class": "stage_2_confirmed"},
                "sector": {"ticker": sector_etf, "trend_template_passes": 6, "stage_class": "stage_2_confirmed", "qualifies_for_long": True},
                "candidate": {"ticker": candidate_ticker, "trend_template_passes": 6, "stage_class": "stage_2_confirmed"},
                "regime_multiplier": 1.0,
                "candidate_qualifies_for_entry": True,
                "circuit_breaker_stage_4": False,
            },
        ),
    )

    # paper-auto positions index lives under tmp too
    pos_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    pos_json.parent.mkdir(parents=True, exist_ok=True)
    pos_json.write_text('{"positions": []}', encoding="utf-8")
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(pos_json))

    return tmp_path


# ---------------------------------------------------------------- helpers


def _make_run_dir(root: Path) -> Path:
    """Manually create a run dir like phase_init would (for testing post phases)."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / "2026-05-29T00-00-00"
    p.mkdir()
    return p


def _seed_init_artifacts(
    run_dir: Path,
    tickers: list[str],
    *,
    skeptic_envelopes: dict[str, dict] | None = None,
) -> None:
    """Stage 00/01/02/03 artifacts as if phase_init had completed."""
    invocations = []
    for t in tickers:
        invocations.append({
            "ticker": t,
            "subagent": "trade-skeptic",
            "prompt": f"skeptic for {t}",
            "envelope_path": str(run_dir / "04_skeptic_envelopes" / f"{t}.json"),
            "ledger_path": f"ledgers/candidates/2026-05-29/{t}.yml",
            "bull_md_path": f"ledgers/candidates/2026-05-29/{t}.md",
            "atr_14": 4.0,
            "next_earnings_date": "2026-08-15",
            "sector_industry": "Medical Devices",
            "sector_etf": "XLV",
            "setup_type": "xs_short_term_reversal_liquid_us",
            "setup_grade": "B",
            "pivot_price": 100.0,
            "limit_price": 100.1,
            "stop_price": 92.0,
            "target_price": 116.0,
            "shares": 500,
            "screener_summary": {
                "blocked": False,
                "corrected_sector_etf": None,
                "blocking_checks": [],
            },
        })
    run_dir.joinpath("04_skeptic_envelopes").mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("06_panel_envelopes").mkdir(parents=True, exist_ok=True)

    with open(run_dir / "00_candidates.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({
            "candidates": [{
                "ticker": t, "setup_type": "xs_short_term_reversal_liquid_us",
                "setup_grade": "B", "pivot_price": 100.0, "limit_price": 100.1,
                "stop_price": 92.0, "target_price": 116.0, "shares": 500,
                "sector_etf": "XLV",
            } for t in tickers],
            "loaded_at": "2026-05-29T00:00:00+00:00",
            "account": {"net_liquidation": 1_000_000.0, "cash": 750_000.0,
                        "regime_class": "stage_2_confirmed"},
        }, fh, sort_keys=False)

    with open(run_dir / "01_screener.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({"results": {}, "n_in": len(tickers), "n_surviving": len(tickers)}, fh)

    with open(run_dir / "03_skeptic_invocations.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({"invocations": invocations,
                        "envelope_dir": str(run_dir / "04_skeptic_envelopes")}, fh)

    # Status: init completed
    state.write_run_status(run_dir, phase="init", started_at="2026-05-29T00:00:00+00:00")
    state.write_run_status(run_dir, phase="init", completed_at="2026-05-29T00:00:05+00:00",
                           candidates_in=len(tickers),
                           candidates_surviving_screener=len(tickers),
                           skeptic_invocations_written=len(tickers))

    if skeptic_envelopes:
        for ticker, env in skeptic_envelopes.items():
            with open(run_dir / "04_skeptic_envelopes" / f"{ticker}.json", "w") as fh:
                json.dump(env, fh)


def _seed_post_skeptic(run_dir: Path, tickers: list[str]) -> None:
    """Stage 05 panel invocations as if post_skeptic had completed."""
    invocations = []
    for t in tickers:
        panel_input = {
            "candidate": {
                "ticker": t, "setup_type": "xs_short_term_reversal_liquid_us",
                "setup_grade": "B", "pivot_price": 100.0, "stop_price": 92.0,
                "stop_distance_pct": 0.08, "sector_etf": "XLV",
                "sector_industry": "Medical Devices",
                "shares": 500, "source": "quant_scanner",
            },
            "ledger_context": {
                "ledger_path": f"ledgers/candidates/2026-05-29/{t}.yml",
                "bull_report_path": f"ledgers/candidates/2026-05-29/{t}.md",
                "bear_report_path": f"ledgers/candidates/2026-05-29/{t}-bear.md",
                "regime_summary": {"broad_market_stage_class": "stage_2_confirmed",
                                   "sector_etf": "XLV"},
                "atr_14": 4.0, "next_earnings_date": "2026-08-15",
                "screener_summary": {"blocked": False, "corrected_sector_etf": None,
                                     "blocking_checks": []},
            },
            "portfolio_context": {
                "existing_positions": [], "net_liquidation": 1_000_000.0,
                "cash_buffer_pct": 0.75, "position_count": 0,
            },
            "panel_metadata": {
                "panel_call_id": f"{run_dir.name}__{t}",
                "panel_firing_date": "2026-05-29", "shadow_mode": True,
            },
        }
        envelope_dir = run_dir / "06_panel_envelopes" / t
        envelope_dir.mkdir(parents=True, exist_ok=True)
        invocations.append({
            "ticker": t, "panel_input": panel_input,
            "critics_to_fire": ["risk-manager", "setup-quality-hawk",
                                "macro-skeptic", "quant-insight"],
            "envelope_dir": str(envelope_dir),
        })
    with open(run_dir / "05_panel_invocations.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({"invocations": invocations}, fh, sort_keys=False)
    state.write_run_status(run_dir, phase="post_skeptic",
                           completed_at="2026-05-29T00:01:00+00:00",
                           skeptic_envelopes_read=len(tickers),
                           panel_invocations_written=len(tickers))


def _seed_panel_votes(
    run_dir: Path, ticker: str, adjustments: dict[str, str]
) -> None:
    """Write 06_panel_envelopes/<TICKER>/<critic>.json for each critic."""
    env_dir = run_dir / "06_panel_envelopes" / ticker
    env_dir.mkdir(parents=True, exist_ok=True)
    for critic, adj in adjustments.items():
        # critic agent name is hyphenated; CriticVote.critic field is underscored
        critic_field = critic.replace("-", "_")
        vote = CriticVote(
            critic=critic_field, candidate_ticker=ticker,
            panel_call_id=f"{run_dir.name}__{ticker}",
            panel_firing_date="2026-05-29",
            risks=[] if adj == "hold" else [
                {"risk": "test", "severity": "high", "grounding_evidence": "e"}
            ],
            confidence_adjustment=adj,
            adjustment_rationale="test",
            estimated_cost_usd=0.05,
        )
        with open(env_dir / f"{critic}.json", "w", encoding="utf-8") as fh:
            json.dump(vote.to_dict(), fh)


# =============================================================== status round-trip


def test_run_status_yaml_round_trips(_isolated_run_root):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    state.write_run_status(run_dir, phase="init", started_at="2026-05-29T00:00:00+00:00")
    state.write_run_status(
        run_dir, phase="init", completed_at="2026-05-29T00:00:05+00:00",
        candidates_in=8, candidates_surviving_screener=3,
        skeptic_invocations_written=3,
    )
    doc = state.read_run_status(run_dir)
    assert doc["run_id"] == run_dir.name
    assert doc["last_phase_completed"] == "init"
    assert doc["run_started_at"] == "2026-05-29T00:00:00+00:00"
    entry = doc["phases_completed"][0]
    assert entry["phase"] == "init"
    assert entry["candidates_in"] == 8
    assert entry["candidates_surviving_screener"] == 3
    assert entry["skeptic_invocations_written"] == 3


# =============================================================== phase_init


def test_phase_init_writes_all_expected_artifacts(_isolated_run_root, monkeypatch, capsys):
    run_dir = _isolated_run_root / "ledgers" / "_auto_paper_runs" / "init-test"
    monkeypatch.setattr(
        run_entry, "scan_today",
        lambda **kw: [_FakeScanReport(candidates=[
            _FakeScanCand(ticker="GKOS"),
            _FakeScanCand(ticker="PDD", sector_etf="XLY"),
        ])],
    )
    monkeypatch.setattr(run_entry.screener_mod, "screen",
                        lambda ticker, claimed_sector_etf=None: _ok_screener_result(ticker, sector=claimed_sector_etf or "XLV"))
    monkeypatch.setattr(run_entry, "_fetch_industry", lambda t: "Medical Devices")

    rc = run_entry.phase_init(run_dir)
    assert rc == 0

    # Required artifacts exist
    assert (run_dir / "00_candidates.yml").exists()
    assert (run_dir / "01_screener.yml").exists()
    assert (run_dir / "02_shell_ledgers" / "GKOS.yml").exists()
    assert (run_dir / "02_shell_ledgers" / "PDD.yml").exists()
    assert (run_dir / "03_skeptic_invocations.yml").exists()
    assert (run_dir / "04_skeptic_envelopes").is_dir()
    assert (run_dir / "06_panel_envelopes").is_dir()
    assert (run_dir / "_status.yml").exists()

    # Status structure
    status = state.read_run_status(run_dir)
    assert status["last_phase_completed"] == "init"
    completed = status["phases_completed"][-1]
    assert completed["candidates_in"] == 2
    assert completed["candidates_surviving_screener"] == 2
    assert completed["skeptic_invocations_written"] == 2

    # Invocation count matches surviving count
    invs = yaml.safe_load((run_dir / "03_skeptic_invocations.yml").read_text())["invocations"]
    assert len(invs) == 2
    assert {inv["ticker"] for inv in invs} == {"GKOS", "PDD"}

    # Stdout marker
    out = capsys.readouterr().out
    assert "PHASE_INIT_OK" in out


# =============================================================== phase_post_skeptic


def test_phase_post_skeptic_raises_on_missing_envelope(_isolated_run_root):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS", "PDD"], skeptic_envelopes={
        "GKOS": {"verdict": "STRONG"},
        # PDD envelope intentionally missing
    })
    with pytest.raises(MissingEnvelopeError) as exc:
        run_entry.phase_post_skeptic(run_dir)
    assert "PDD" in str(exc.value)


def test_phase_post_skeptic_succeeds_when_all_envelopes_present(_isolated_run_root, capsys):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS", "PDD"], skeptic_envelopes={
        "GKOS": {"verdict": "STRONG"},
        "PDD": {"verdict": "PARTIAL"},
    })
    rc = run_entry.phase_post_skeptic(run_dir)
    assert rc == 0
    assert (run_dir / "05_panel_invocations.yml").exists()
    doc = yaml.safe_load((run_dir / "05_panel_invocations.yml").read_text())
    assert len(doc["invocations"]) == 2
    # Each invocation has critics_to_fire (4 for non-semi quant: 3 core + quant_insight)
    for inv in doc["invocations"]:
        assert len(inv["critics_to_fire"]) == 4
    assert "PHASE_POST_SKEPTIC_OK" in capsys.readouterr().out


# =============================================================== phase_post_panel


def test_phase_post_panel_raises_on_all_critics_missing_for_ticker(_isolated_run_root):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS"], skeptic_envelopes={"GKOS": {"verdict": "STRONG"}})
    _seed_post_skeptic(run_dir, ["GKOS"])
    # No critic votes written

    with pytest.raises(MissingEnvelopeError) as exc:
        run_entry.phase_post_panel(
            run_dir,
            place_fn=lambda *a, **kw: pytest.fail("should not call place"),
            client_factory=lambda *a, **kw: object(),
        )
    assert "GKOS" in str(exc.value)


def test_phase_post_panel_aggregates_partial_votes_when_some_critics_present(_isolated_run_root, capsys):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS"], skeptic_envelopes={"GKOS": {"verdict": "STRONG"}})
    _seed_post_skeptic(run_dir, ["GKOS"])

    # Only 3 of 4 critics return votes — partial
    _seed_panel_votes(run_dir, "GKOS", {
        "risk-manager": "hold",
        "setup-quality-hawk": "hold",
        "macro-skeptic": "hold",
        # quant-insight intentionally missing
    })

    placed_calls: list[Any] = []
    def _fake_place(cand, *, client, dry_run, apply_panel_sizing, auto_paper_run_dir, already_regime_sized=False):
        placed_calls.append((cand.ticker, cand.shares, str(auto_paper_run_dir)))
        return SimpleNamespace(
            status="placed", broker_order_id=12345,
            reason=None, ledger_path="x.yml",
        )

    rc = run_entry.phase_post_panel(
        run_dir, place_fn=_fake_place, client_factory=lambda: object(),
    )
    assert rc == 0
    assert placed_calls == [("GKOS", 500, str(run_dir))]
    out = capsys.readouterr().out
    assert "PLACE: GKOS placed 500" in out
    assert "PHASE_POST_PANEL_OK" in out

    # 07 results landed
    results = yaml.safe_load((run_dir / "07_placement_results.yml").read_text())
    assert results["n_placed"] == 1
    assert results["results"][0]["ticker"] == "GKOS"


def test_phase_post_panel_dry_run_threads_to_place_fn(_isolated_run_root, capsys):
    """--dry-run must reach place_fn as dry_run=True; nothing is 'placed'.

    Mirrors place_candidate's real dry-run contract: it returns status='dry_run'
    BEFORE the broker call, so n_placed stays 0 and the row records dry_run.
    """
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS"], skeptic_envelopes={"GKOS": {"verdict": "STRONG"}})
    _seed_post_skeptic(run_dir, ["GKOS"])
    _seed_panel_votes(run_dir, "GKOS", {
        "risk-manager": "hold",
        "setup-quality-hawk": "hold",
        "macro-skeptic": "hold",
        "quant-insight": "hold",
    })

    seen_dry_run: list[bool] = []
    def _fake_place(cand, *, client, dry_run, apply_panel_sizing, auto_paper_run_dir, already_regime_sized=False):
        seen_dry_run.append(dry_run)
        # Real place_candidate short-circuits to dry_run status when dry_run=True
        return SimpleNamespace(
            status="dry_run" if dry_run else "placed",
            broker_order_id=None if dry_run else 12345,
            reason=None, ledger_path=None if dry_run else "x.yml",
        )

    rc = run_entry.phase_post_panel(
        run_dir, dry_run=True, place_fn=_fake_place, client_factory=lambda: object(),
    )
    assert rc == 0
    assert seen_dry_run == [True]            # flag reached the placer
    out = capsys.readouterr().out
    assert "PLACE: GKOS dry_run" in out
    assert "PHASE_POST_PANEL_OK placed=0" in out

    results = yaml.safe_load((run_dir / "07_placement_results.yml").read_text())
    assert results["n_placed"] == 0
    assert results["results"][0]["status"] == "dry_run"
    assert results["results"][0]["placed"] is False
    assert results["results"][0]["broker_order_id"] is None


def test_phase_post_panel_defers_on_structural_risk(_isolated_run_root, capsys):
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS"], skeptic_envelopes={"GKOS": {"verdict": "STRONG"}})
    _seed_post_skeptic(run_dir, ["GKOS"])
    _seed_panel_votes(run_dir, "GKOS", {
        "risk-manager": "structural_risk",
        "setup-quality-hawk": "minus_50",
        "macro-skeptic": "minus_20",
        "quant-insight": "hold",
    })

    def _fake_place(*a, **kw):
        pytest.fail("place_candidate must not be called on defer verdict")

    rc = run_entry.phase_post_panel(
        run_dir, place_fn=_fake_place, client_factory=lambda: object(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PLACE: GKOS defer 0" in out
    results = yaml.safe_load((run_dir / "07_placement_results.yml").read_text())
    assert results["n_placed"] == 0
    assert results["results"][0]["status"] == "defer"


# =============================================================== out-of-order


def test_out_of_order_phase_raises_when_no_status(_isolated_run_root):
    """Calling post_skeptic without prior init must fail loud."""
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    # no _status.yml written
    with pytest.raises(RunDirCorruptError):
        run_entry.phase_post_skeptic(run_dir)


def test_out_of_order_phase_raises_when_status_mismatched(_isolated_run_root):
    """Calling post_panel when only init is completed (not post_skeptic) raises."""
    run_dir = _make_run_dir(run_entry.RUN_ROOT)
    _seed_init_artifacts(run_dir, ["GKOS"], skeptic_envelopes={"GKOS": {"verdict": "STRONG"}})
    # Status shows only init complete — post_panel expects post_skeptic
    with pytest.raises(OutOfOrderPhaseError):
        run_entry.phase_post_panel(
            run_dir, place_fn=lambda *a, **kw: None, client_factory=lambda: object(),
        )


# =============================================================== _latest_run_dir


def test_latest_run_dir_returns_alphabetically_last(_isolated_run_root):
    root = run_entry.RUN_ROOT
    root.mkdir(parents=True, exist_ok=True)
    (root / "2026-05-29T00-00-00").mkdir()
    (root / "2026-05-29T22-30-00").mkdir()
    (root / "2026-05-29T10-15-00").mkdir()
    p = run_entry._latest_run_dir()
    assert p.name == "2026-05-29T22-30-00"


def test_latest_run_dir_raises_when_empty(_isolated_run_root):
    with pytest.raises(RunDirCorruptError):
        run_entry._latest_run_dir()


# =============================================================== _build_skeptic_prompt


def test_build_skeptic_prompt_contains_ledger_and_bull_path():
    p = run_entry._build_skeptic_prompt(
        ledger_path="ledgers/candidates/2026-05-29/GKOS.yml",
        bull_md_path="ledgers/candidates/2026-05-29/GKOS.md",
    )
    assert "ledgers/candidates/2026-05-29/GKOS.yml" in p
    assert "ledgers/candidates/2026-05-29/GKOS.md" in p
    assert "Skeptic pass" in p
