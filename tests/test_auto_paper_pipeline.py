"""Tests for tools.auto_paper.pipeline — placement orchestration with mocked broker."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.auto_paper import screener as _screener_mod
from tools.auto_paper import state
from tools.auto_paper.pipeline import (
    CandidateInput,
    _check_track_limits,
    place_candidate,
)


# ------------------------------------------------------- fakes


class FakeTradeClient:
    """Minimal stand-in matching TigerClient's surface (injection seam)."""
    def __init__(self, *, net_liq=1_000_000.0, cash=950_000.0, is_paper=True):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": is_paper,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.summary_assets = [
            SimpleNamespace(summary=SimpleNamespace(
                cash=cash,
                available_funds=cash,
                buying_power=cash * 2,
                net_liquidation=net_liq,
                gross_position_value=net_liq - cash,
                currency="USD",
            ))
        ]
        self.next_order_id = 10_000
        self.calls: list[tuple] = []

    def get_assets(self, *, account, segment=False, **_):
        return self.summary_assets

    def get_positions(self, *, account, **_):
        return []

    def get_open_orders(self, *, account, **_):
        return []

    def get_contract(self, *, symbol, **_):
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        order_id = self.next_order_id
        self.next_order_id += 1
        order.id = order_id
        self.calls.append((
            "place_order", order.account, order.action, order.quantity,
            order.limit_price, order.contract.symbol,
        ))
        return order_id


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    # After the 2026-05-26 8y extension, no SETUP_REPLAY_REGISTRY setup is on
    # the live deployable list (EP + SEPA-VCP both parked). The pipeline tests
    # use EP as the candidate setup_type because it is the only currently-
    # schema-valid name (the schema enum at ledgers/_schema/ledger.schema.json
    # only allows SEPA-VCP / EP / Pullback-20SMA / RSI-Divergence /
    # Resistance-Breakout / Manual for setup_classification.type). We force
    # the deployable filter to treat EP as deployable so the tests exercise
    # the rest of the pipeline (limit checks, broker mock, persistence).
    # Pullback-20SMA stays not-deployable so test_rejects_non_deployable works.
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(_pipeline.config, "is_deployable", lambda t: t == "EP")
    # Lever D — regime-conditional sizing reads SPY's live trend-template.
    # Default tests to stage_2_confirmed (full size, no multiplier effect)
    # so existing tests behave identically to the pre-lever-D world.
    # Tests that exercise other regimes monkeypatch this themselves.
    monkeypatch.setattr(
        _pipeline, "_resolve_regime_multiplier",
        lambda: ("stage_2_confirmed", 1.0),
    )
    # Screener — replace the network-touching call with a "clean" stub that
    # passes every check + returns no sector correction. Tests that exercise
    # screener-block paths re-monkeypatch this themselves.
    def _clean_screener(ticker, claimed_sector_etf):
        return _screener_mod.ScreenerResult(
            ticker=ticker,
            blocked=False,
            blocking_checks=[],
            corrected_sector_etf=None,
            checks=[
                _screener_mod.CheckResult(check="litigation", passed=True),
                _screener_mod.CheckResult(check="dilution", passed=True),
                _screener_mod.CheckResult(check="earnings_blackout", passed=True),
                _screener_mod.CheckResult(check="sector_lookup", passed=True),
            ],
            computed_at="2026-05-27T00:00:00+00:00",
        )
    monkeypatch.setattr(_pipeline, "_run_screener", _clean_screener)
    return ledger_dir, positions_json


@pytest.fixture
def paper_client(paper_dirs):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient())


def _vcp_cand(**over):
    base = dict(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
    )
    base.update(over)
    return CandidateInput(**base)


# ------------------------------------------------------- limit-check unit


def test_track_limits_clean():
    assert _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=950_000.0,
    ) is None


def test_track_limits_position_count():
    fake_positions = [{"ticker": f"T{i}", "sector": "XLU"} for i in range(8)]
    reason = _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=1_000_000.0,
        existing_positions=fake_positions,
        existing_cash=950_000.0,
    )
    assert "position count limit" in reason


def test_track_limits_per_position():
    # 10000 shares × $850 = $8.5M > 5% of $1M
    reason = _check_track_limits(
        cand=_vcp_cand(shares=10_000),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=950_000.0,
    )
    assert "5% cap" in reason


def test_track_limits_sector_cap():
    # Engineer: per-position passes (4.25%) but combined sector trips 20%.
    # NVDA add = 50 × $850.50 = $42,525 = 4.25% (under 5%)
    # Existing XLK: 1600 MSFT × $105 = $168,000 = 16.8%
    # Combined sector = $210,525 = 21.05% > 20%
    existing = [{"ticker": "MSFT", "shares": 1600, "entry_price": 105.00, "sector": "XLK"}]
    reason = _check_track_limits(
        cand=_vcp_cand(shares=50),
        account_net_liq=1_000_000.0,
        existing_positions=existing,
        existing_cash=950_000.0,
    )
    assert "XLK" in reason and "20% cap" in reason


def test_track_limits_cash_buffer():
    # Engineer: per-position passes (4.25%) but cash drops under 15%.
    # cost = 50 × $850.50 = $42,525
    # existing_cash = $180,000 (= 18% of $1M); after = $137,475 = 13.75% < 15%
    reason = _check_track_limits(
        cand=_vcp_cand(shares=50),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=180_000.0,
    )
    assert "15% cash buffer" in reason


def test_track_limits_zero_net_liq():
    reason = _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=0.0,
        existing_positions=[],
        existing_cash=0.0,
    )
    assert "net_liquidation" in reason


# ------------------------------------------------------- place_candidate


def test_rejects_non_deployable(paper_client):
    cand = _vcp_cand(setup_type="Pullback-20SMA")
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert "not on deployable list" in result.reason


def test_rejects_existing_ledger(paper_client, paper_dirs):
    state.write_submitted_ledger(
        ticker="NVDA", setup_type="EP", setup_grade=None,
        pivot_price=850, limit_price=850.5, stop_price=820,
        shares=10, broker_order_id=1, broker="tiger_paper",
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert "already exists" in result.reason


def test_dry_run_does_not_place_or_write(paper_client, paper_dirs):
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    assert "would place limit-buy" in result.reason
    assert result.cost_estimate_usd == 10 * 850.50
    # No file should have been written
    assert not state.ledger_exists("NVDA")
    assert paper_client._tc.calls == []


def test_real_run_places_and_writes(paper_client, paper_dirs):
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "placed"
    assert result.broker_order_id == 10_000
    assert result.ledger_path is not None
    assert state.ledger_exists("NVDA")
    # Positions.json append
    pj = state.load_positions_json()
    assert len(pj["positions"]) == 1
    assert pj["positions"][0]["ticker"] == "NVDA"
    assert pj["positions"][0]["broker_order_id"] == 10_000
    assert pj["positions"][0]["broker"] == "tiger_paper"
    # Broker actually got called
    place_calls = [c for c in paper_client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1


def test_rejects_when_count_limit_hit(paper_client, paper_dirs):
    # Pre-load 8 positions so count limit binds
    for i in range(8):
        state.append_to_positions_json({"ticker": f"T{i}", "sector": "XLU"})
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "rejected"
    assert "position count" in result.reason


def test_returns_error_on_broker_summary_failure(paper_client, paper_dirs):
    def _boom(*a, **kw):
        raise RuntimeError("HTTP 503")
    paper_client._tc.get_assets = _boom
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "error"
    assert "account_summary" in result.reason


def test_returns_error_on_broker_place_failure(paper_client, paper_dirs):
    def _boom(order):
        raise RuntimeError("INSUFFICIENT_FUNDS")
    paper_client._tc.place_order = _boom
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "error"
    assert "INSUFFICIENT_FUNDS" in result.reason


# ---------------------------------------------------------- Lever D — regime sizing
#
# place_candidate reads the SPY broad-market regime via
# _resolve_regime_multiplier() and re-applies the multiplier to cand.shares
# before the broker call. Stage 4 halts; lower regimes shrink size.
# The paper_dirs fixture patches this to stage_2_confirmed (1.0) by default;
# these tests override that.


def test_regime_stage_4_halts_new_entries(paper_client, paper_dirs, monkeypatch):
    """Stage 4 (broad market broken) → refuse entry per CLAUDE.md circuit breaker."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_resolve_regime_multiplier",
        lambda: ("stage_4", 0.0),
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert "stage_4" in result.reason
    assert "halt" in result.reason


def test_regime_stage_3_halves_position_size(paper_client, paper_dirs, monkeypatch):
    """Stage 3 (transitional) multiplier 0.5 → shares should be halved."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_resolve_regime_multiplier",
        lambda: ("stage_3_transitional", 0.5),
    )
    cand = _vcp_cand(shares=10)
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    # 10 shares × 0.5 = 5 shares
    assert "5 NVDA" in result.reason


def test_regime_stage_2_weakening_three_quarter_size(paper_client, paper_dirs, monkeypatch):
    """Stage 2 weakening multiplier 0.75 → shares × 0.75 rounded down."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_resolve_regime_multiplier",
        lambda: ("stage_2_weakening", 0.75),
    )
    cand = _vcp_cand(shares=12)
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    # 12 shares × 0.75 = 9 shares
    assert "9 NVDA" in result.reason


def test_regime_stage_2_confirmed_no_size_change(paper_client, paper_dirs):
    """Default stage_2_confirmed (1.0 multiplier) leaves shares unchanged.
    The paper_dirs fixture patches this regime by default."""
    cand = _vcp_cand(shares=10)
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    assert "10 NVDA" in result.reason


def test_regime_check_failure_fails_closed(paper_client, paper_dirs, monkeypatch):
    """If regime_check raises (e.g. yfinance hiccup), refuse entry rather
    than placing without the regime safety check."""
    from tools.auto_paper import pipeline as _pipeline
    def _boom():
        raise RuntimeError("yfinance unreachable")
    monkeypatch.setattr(_pipeline, "_resolve_regime_multiplier", _boom)
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "rejected"
    assert "regime_check failed" in result.reason
    assert "fail-closed" in result.reason


def test_regime_floor_at_one_share(paper_client, paper_dirs, monkeypatch):
    """When the multiplier × original shares rounds to 0, floor to 1 share
    (not 0) — entry passes the limit-checks but at minimum size."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_resolve_regime_multiplier",
        lambda: ("stage_3_transitional", 0.5),
    )
    cand = _vcp_cand(shares=1)
    result = place_candidate(cand, client=paper_client, dry_run=True)
    # 1 × 0.5 = 0.5 → floored to 1
    assert result.status == "dry_run"
    assert "1 NVDA" in result.reason


# ---------------------------------------------------------- Phase 1 screener
#
# The screener (litigation, dilution, earnings-forward-10d, sector lookup)
# runs after the deployable filter and before broker construction. The
# paper_dirs fixture patches _run_screener to a "clean" stub by default;
# these tests override that to exercise block + correction paths.


def _screener_blocked(*, blocking, corrected_sector=None, first_reason="active class action found"):
    """Build a ScreenerResult that triggers a screener-block path."""
    check_passes = {
        "litigation": "litigation" not in blocking,
        "dilution": "dilution" not in blocking,
        "earnings_blackout": "earnings_blackout" not in blocking,
    }
    return _screener_mod.ScreenerResult(
        ticker="ANY",
        blocked=len(blocking) > 0,
        blocking_checks=list(blocking),
        corrected_sector_etf=corrected_sector,
        checks=[
            _screener_mod.CheckResult(
                check="litigation",
                passed=check_passes["litigation"],
                reason=(first_reason if not check_passes["litigation"] else None),
            ),
            _screener_mod.CheckResult(
                check="dilution",
                passed=check_passes["dilution"],
                reason=(first_reason if not check_passes["dilution"] else None),
            ),
            _screener_mod.CheckResult(
                check="earnings_blackout",
                passed=check_passes["earnings_blackout"],
                reason=(first_reason if not check_passes["earnings_blackout"] else None),
            ),
            _screener_mod.CheckResult(check="sector_lookup", passed=True),
        ],
        computed_at="2026-05-27T00:00:00+00:00",
    )


def test_screener_litigation_blocks_placement(paper_client, paper_dirs, monkeypatch):
    """The GO regression case: active class action → screener:litigation block."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_run_screener",
        lambda t, sec: _screener_blocked(
            blocking=["litigation"],
            first_reason="Active litigation / SEC concern detected - 3 headline(s)",
        ),
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "rejected"
    assert result.reason.startswith("screener:litigation")
    assert "Active litigation" in result.reason
    # Broker should NOT have been called
    place_calls = [c for c in paper_client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 0
    # screener_trace is attached to the rejection
    assert result.screener_trace is not None
    assert result.screener_trace["blocked"] is True


def test_screener_sector_correction_applied(paper_client, paper_dirs, monkeypatch):
    """Mismatched sector (XLK claimed, XLP actual) is corrected pre-placement.
    The corrected sector is what lands in positions.json."""
    from tools.auto_paper import pipeline as _pipeline
    def _stub(ticker, claimed):
        return _screener_mod.ScreenerResult(
            ticker=ticker,
            blocked=False,
            blocking_checks=[],
            corrected_sector_etf="XLP",  # the correction
            checks=[
                _screener_mod.CheckResult(check="litigation", passed=True),
                _screener_mod.CheckResult(check="dilution", passed=True),
                _screener_mod.CheckResult(check="earnings_blackout", passed=True),
                _screener_mod.CheckResult(
                    check="sector_lookup", passed=True,
                    reason="sector mismatch: claimed=XLK, actual=XLP",
                    evidence={
                        "yfinance_sector": "Consumer Defensive",
                        "claimed_sector_etf": "XLK",
                        "actual_sector_etf": "XLP",
                        "mismatch": True,
                    },
                ),
            ],
            computed_at="2026-05-27T00:00:00+00:00",
        )
    monkeypatch.setattr(_pipeline, "_run_screener", _stub)
    cand = _vcp_cand(sector_etf="XLK")  # wrong
    result = place_candidate(cand, client=paper_client, dry_run=False)
    assert result.status == "placed"
    pj = state.load_positions_json()
    assert pj["positions"][0]["sector"] == "XLP"  # corrected
    # The trace records the correction
    assert result.screener_trace["corrected_sector_etf"] == "XLP"


def test_screener_dilution_blocks_placement(paper_client, paper_dirs, monkeypatch):
    """Recent dilutive offering surfaces as screener:dilution block."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_run_screener",
        lambda t, sec: _screener_blocked(
            blocking=["dilution"],
            first_reason="Recent dilutive-raise announcement - 1 headline(s)",
        ),
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert result.reason.startswith("screener:dilution")


def test_screener_crash_fails_open(paper_client, paper_dirs, monkeypatch):
    """A top-level screener exception (rare) should NOT block placement.
    The crash is logged in screener_trace for operator audit."""
    from tools.auto_paper import pipeline as _pipeline
    def _crash(t, sec):
        raise RuntimeError("DNS failure on finviz")
    monkeypatch.setattr(_pipeline, "_run_screener", _crash)
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "placed"
    assert result.screener_trace == {"crashed": True, "error": "DNS failure on finviz"}


def test_screener_multiple_blocks_reason_lists_all(paper_client, paper_dirs, monkeypatch):
    """When >1 check fires, all blocking_checks appear in the reason string."""
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(
        _pipeline, "_run_screener",
        lambda t, sec: _screener_blocked(
            blocking=["litigation", "earnings_blackout"],
            first_reason="Active litigation detected",
        ),
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "rejected"
    # Both surface in the human-readable reason
    assert "litigation" in result.reason and "earnings_blackout" in result.reason


# ---------------------------------------------------------- Phase 3 panel sizing
#
# The Phase 3 multi-rater critic panel produces a PanelVerdict per candidate.
# Its sizing_multiplier (0.0/0.5/0.8/1.0) is threaded onto CandidateInput by
# the /auto-paper skill orchestrator. place_candidate applies it ONLY when
# apply_panel_sizing=True (Phase 3 lives after shadow-mode lifts ~2026-06-10).
# Default is shadow mode — multiplier logged but not applied.


def _panel_verdict_dict(*, action, multiplier):
    """Build a minimal PanelVerdict-shaped dict for the panel_verdict field."""
    return {
        "ticker": "NVDA",
        "action": action,
        "sizing_multiplier": multiplier,
        "n_critics_total": 3,
        "n_critics_hold": 0 if action != "preserve" else 3,
        "n_critics_minus_20": 2 if action == "reduce_20" else 0,
        "n_critics_minus_50": 1 if action == "half_size_review" else 0,
        "n_critics_structural_risk": 1 if action == "defer" else 0,
        "structural_risk_critics": ["risk_manager"] if action == "defer" else [],
        "minus_50_critics": ["setup_quality_hawk"] if action == "half_size_review" else [],
        "minus_20_critics": (
            ["risk_manager", "macro_skeptic"] if action == "reduce_20" else []
        ),
        "rationale": f"test verdict {action}",
        "total_cost_usd": 0.30,
        "shadow_mode": True,
        "computed_at": "2026-05-27T22-15:00+00:00",
        "panel_call_id": "test-panel",
    }


def test_panel_shadow_mode_logs_but_does_not_apply(paper_client, paper_dirs):
    """Default apply_panel_sizing=False. Multiplier is recorded in result
    but cand.shares is unchanged."""
    cand = CandidateInput(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
        sizing_multiplier=0.5,  # would halve if applied
        panel_verdict=_panel_verdict_dict(action="half_size_review", multiplier=0.5),
    )
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    # SHADOW MODE: shares should be UNCHANGED in the dry-run reason.
    assert "10 NVDA" in result.reason
    # Panel verdict is logged on the result
    assert result.panel_verdict is not None
    assert result.panel_verdict["action"] == "half_size_review"
    assert result.panel_sizing_applied is False


def test_panel_live_mode_reduce_20_shrinks_shares(paper_client, paper_dirs):
    """apply_panel_sizing=True + reduce_20 → shares × 0.8 floored."""
    cand = CandidateInput(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
        sizing_multiplier=0.8,
        panel_verdict=_panel_verdict_dict(action="reduce_20", multiplier=0.8),
    )
    result = place_candidate(
        cand, client=paper_client, dry_run=True, apply_panel_sizing=True,
    )
    assert result.status == "dry_run"
    # 10 × 0.8 = 8 shares
    assert "8 NVDA" in result.reason
    assert result.panel_sizing_applied is True


def test_panel_live_mode_half_size_halves_shares(paper_client, paper_dirs):
    """apply_panel_sizing=True + half_size_review → shares × 0.5 floored."""
    cand = CandidateInput(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
        sizing_multiplier=0.5,
        panel_verdict=_panel_verdict_dict(action="half_size_review", multiplier=0.5),
    )
    result = place_candidate(
        cand, client=paper_client, dry_run=True, apply_panel_sizing=True,
    )
    assert result.status == "dry_run"
    # 10 × 0.5 = 5 shares
    assert "5 NVDA" in result.reason
    assert result.panel_sizing_applied is True


def test_panel_live_mode_defer_rejects(paper_client, paper_dirs):
    """apply_panel_sizing=True + sizing_multiplier=0.0 → rejected."""
    cand = CandidateInput(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
        sizing_multiplier=0.0,
        panel_verdict=_panel_verdict_dict(action="defer", multiplier=0.0),
    )
    result = place_candidate(
        cand, client=paper_client, dry_run=False, apply_panel_sizing=True,
    )
    assert result.status == "rejected"
    assert "panel:defer" in result.reason
    assert result.panel_verdict["action"] == "defer"
    # Broker should NOT have been called
    place_calls = [c for c in paper_client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 0


def test_panel_no_verdict_passes_through_unchanged(paper_client, paper_dirs):
    """Candidate with panel_verdict=None (default) is unaffected by Phase 3
    plumbing — same behavior as pre-Phase-3."""
    cand = CandidateInput(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
    )
    result = place_candidate(
        cand, client=paper_client, dry_run=True, apply_panel_sizing=True,
    )
    assert result.status == "dry_run"
    assert "10 NVDA" in result.reason
    assert result.panel_verdict is None
    assert result.panel_sizing_applied is False
