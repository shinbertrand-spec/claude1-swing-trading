"""Tests for the write-ahead intent journal + crash-recovery reconciliation.

Covers the anti-orphan invariant for the v2 entry path: the framework records
an order intent durably BEFORE placing it, and any crash between place and
ledger-write leaves a recoverable intent that reconcile_intents drives to a
terminal state. No silent live order the ledger doesn't know about.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.auto_paper import intent_log, screener as _screener_mod, state
from tools.auto_paper.pipeline import CandidateInput, place_candidate
from tools.auto_paper.reconcile import reconcile_intents


# --------------------------------------------------------------------------- fakes


class FakeTradeClient:
    """SDK-trade-client stand-in (wrapped by the real TigerClient seam)."""
    def __init__(self, *, net_liq=1_000_000.0, cash=950_000.0):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678", "account_masked": "...4321",
            "license": "TBSG", "is_paper": True,
            "server_url": "https://mock", "props_dir": "/mock",
        }
        self.summary_assets = [SimpleNamespace(summary=SimpleNamespace(
            cash=cash, available_funds=cash, buying_power=cash * 2,
            net_liquidation=net_liq, gross_position_value=net_liq - cash,
            currency="USD",
        ))]
        self.next_order_id = 10_000
        self.calls: list = []

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
        self.calls.append(("place_order", order.contract.symbol, order.quantity,
                           order.limit_price))
        return order_id


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    from tools.auto_paper import pipeline as _pipeline
    monkeypatch.setattr(_pipeline.config, "is_deployable", lambda t: t == "EP")
    monkeypatch.setattr(_pipeline, "_resolve_regime_multiplier",
                        lambda: ("stage_2_confirmed", 1.0))

    def _clean_screener(ticker, claimed_sector_etf):
        return _screener_mod.ScreenerResult(
            ticker=ticker, blocked=False, blocking_checks=[],
            corrected_sector_etf=None,
            checks=[_screener_mod.CheckResult(check="litigation", passed=True)],
            computed_at="2026-05-27T00:00:00+00:00",
        )
    monkeypatch.setattr(_pipeline, "_run_screener", _clean_screener)
    # B1 gate chain: stub to pass-through so these tests stay hermetic —
    # without this, place_candidate runs the real chain, which appends fixture
    # verdicts to the PRODUCTION ledgers/paper-auto/_gates/*.jsonl and rewrites
    # journal/paper-auto/sleeve_breaker.json on every pytest run (pollution
    # found 2026-08-13). Chain wiring is covered by
    # test_auto_paper_pipeline_gate_chain.py.
    monkeypatch.setattr(_pipeline, "_run_gate_chain",
                        lambda cand, **kw: (None, None))
    return ledger_dir, positions_json


@pytest.fixture
def paper_client(paper_dirs):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient())


def _cand(**over):
    base = dict(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
    )
    base.update(over)
    return CandidateInput(**base)


# --------------------------------------------------------------------------- intent_log unit


def test_make_cloid_deterministic():
    assert intent_log.make_cloid("nvda", "run-1") == intent_log.make_cloid("NVDA", "run-1")
    assert intent_log.make_cloid("NVDA", "run-1") != intent_log.make_cloid("NVDA", "run-2")


def test_write_load_update_roundtrip(paper_dirs):
    rec = intent_log.IntentRecord(
        client_order_id="ap-NVDA-x", ticker="NVDA", setup_type="EP",
        setup_grade="A", pivot_price=850.0, limit_price=850.5, stop_price=820.0,
        target_price=910.0, shares=10, sector_etf="XLK",
    )
    intent_log.write_intent(rec)
    loaded = intent_log.load_intent("ap-NVDA-x")
    assert loaded.ticker == "NVDA" and loaded.status == intent_log.STATUS_INTENT
    assert loaded.created_at and loaded.updated_at
    intent_log.mark("ap-NVDA-x", intent_log.STATUS_PLACED, broker_order_id=42)
    again = intent_log.load_intent("ap-NVDA-x")
    assert again.status == "placed" and again.broker_order_id == 42
    assert again.is_unresolved and not again.is_terminal


def test_write_intent_refuses_clobber(paper_dirs):
    rec = intent_log.IntentRecord(
        client_order_id="dup", ticker="T", setup_type="EP", setup_grade=None,
        pivot_price=1, limit_price=1, stop_price=0.9, target_price=2, shares=1,
    )
    intent_log.write_intent(rec)
    with pytest.raises(FileExistsError):
        intent_log.write_intent(rec)


def test_has_unresolved_intent(paper_dirs):
    rec = intent_log.IntentRecord(
        client_order_id="ap-T-1", ticker="T", setup_type="EP", setup_grade=None,
        pivot_price=1, limit_price=1, stop_price=0.9, target_price=2, shares=1,
        status=intent_log.STATUS_PLACED,
    )
    intent_log.write_intent(rec)
    assert intent_log.has_unresolved_intent("T")
    intent_log.mark("ap-T-1", intent_log.STATUS_LEDGERED)
    assert not intent_log.has_unresolved_intent("T")


# --------------------------------------------------------------------------- write-ahead happy path


def test_place_happy_path_intent_ends_ledgered(paper_client, paper_dirs):
    result = place_candidate(_cand(), client=paper_client, dry_run=False,
                             auto_paper_run_dir="ledgers/_auto_paper_runs/RUN1")
    assert result.status == "placed"
    assert result.client_order_id
    rec = intent_log.load_intent(result.client_order_id)
    assert rec.status == intent_log.STATUS_LEDGERED
    assert rec.broker_order_id == result.broker_order_id
    assert intent_log.unresolved_intents() == []   # nothing dangling


def test_dry_run_writes_no_intent(paper_client, paper_dirs):
    result = place_candidate(_cand(), client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    assert intent_log.iter_intents() == []


# --------------------------------------------------------------------------- the orphan window


def test_crash_between_place_and_ledger_is_recoverable_not_orphan(
    paper_client, paper_dirs, monkeypatch,
):
    """THE load-bearing case. The broker accepts the order, then the ledger
    write throws (disk/permission/crash). The order is NOT orphaned: a durable
    intent (status=placed, broker_order_id set) knows about it."""
    from tools.auto_paper import pipeline as _pipeline

    def _boom(**kw):
        raise state.PaperAutoStateError("simulated disk failure on ledger write")
    monkeypatch.setattr(_pipeline.state, "write_submitted_ledger", _boom)

    result = place_candidate(_cand(), client=paper_client, dry_run=False,
                             auto_paper_run_dir="ledgers/_auto_paper_runs/RUN1")

    assert result.status == "error"
    assert result.recoverable is True
    assert result.broker_order_id is not None      # the order is live at broker
    # No canonical ledger was written...
    assert not state.ledger_exists("NVDA")
    # ...but the intent durably knows about the live order.
    rec = intent_log.load_intent(result.client_order_id)
    assert rec.status == intent_log.STATUS_PLACED
    assert rec.broker_order_id == result.broker_order_id


def _failing_ledger_writer(flag):
    """Wrapper that raises while flag[0] is True, else delegates to the real
    state.write_submitted_ledger. Lets a single test fail the write then
    recover — without monkeypatch.undo() (which would revert fixture patches)."""
    real = state.write_submitted_ledger

    def _w(**kw):
        if flag[0]:
            raise state.PaperAutoStateError("simulated disk failure on ledger write")
        return real(**kw)
    return _w


def test_recovery_reconstructs_ledger_from_placed_intent(
    paper_client, paper_dirs, monkeypatch,
):
    """After the crash above, reconcile_intents reconstructs the ledger and
    drives the intent to ledgered — closing the orphan."""
    from tools.auto_paper import pipeline as _pipeline
    flag = [True]
    monkeypatch.setattr(_pipeline.state, "write_submitted_ledger",
                        _failing_ledger_writer(flag))
    result = place_candidate(_cand(), client=paper_client, dry_run=False)
    oid = result.broker_order_id
    flag[0] = False   # ledger writes work again for the recovery

    # The broker shows the order resting open.
    open_orders = [{"order_id": oid, "symbol": "NVDA", "action": "BUY",
                    "quantity": 10, "limit_price": 850.50, "status": "Submitted"}]
    res = reconcile_intents(open_orders=open_orders, filled_orders=[], holdings={})

    assert len(res) == 1
    assert res[0].action == "ledgered_recovered"
    assert res[0].broker_order_id == oid
    # Ledger + positions.json now exist; intent terminal.
    assert state.ledger_exists("NVDA")
    led = state.load_ledger("NVDA")
    assert led["meta"]["state"] == "submitted"
    assert led["position_state"]["starter"]["broker_order_id"] == oid
    pj = state.load_positions_json()
    assert any(p["ticker"] == "NVDA" and p["broker_order_id"] == oid
               for p in pj["positions"])
    assert intent_log.load_intent(result.client_order_id).status == intent_log.STATUS_LEDGERED
    assert intent_log.unresolved_intents() == []


def test_recovery_is_idempotent(paper_client, paper_dirs, monkeypatch):
    from tools.auto_paper import pipeline as _pipeline
    flag = [True]
    monkeypatch.setattr(_pipeline.state, "write_submitted_ledger",
                        _failing_ledger_writer(flag))
    result = place_candidate(_cand(), client=paper_client, dry_run=False)
    oid = result.broker_order_id
    flag[0] = False
    open_orders = [{"order_id": oid, "symbol": "NVDA", "action": "BUY",
                    "quantity": 10, "limit_price": 850.50}]
    first = reconcile_intents(open_orders=open_orders, filled_orders=[], holdings={})
    assert first[0].action == "ledgered_recovered"
    # Second pass: intent now terminal → no work.
    second = reconcile_intents(open_orders=open_orders, filled_orders=[], holdings={})
    assert second == []


def test_broker_reject_marks_intent_abandoned(paper_client, paper_dirs, monkeypatch):
    """Broker rejects the order → it never reached the book → intent abandoned,
    no ledger, nothing to recover."""
    def _reject(order):
        raise RuntimeError("INSUFFICIENT_FUNDS")
    paper_client._tc.place_order = _reject
    result = place_candidate(_cand(), client=paper_client, dry_run=False)
    assert result.status == "error"
    assert "INSUFFICIENT_FUNDS" in result.reason
    rec = intent_log.load_intent(result.client_order_id)
    assert rec.status == intent_log.STATUS_ABANDONED
    assert not state.ledger_exists("NVDA")
    assert intent_log.unresolved_intents() == []


def test_double_place_guard_blocks_on_dangling_intent(paper_client, paper_dirs, monkeypatch):
    """A second placement for the same ticker is refused while a prior intent
    is dangling — the cron-re-fire double-place guard."""
    from tools.auto_paper import pipeline as _pipeline
    flag = [True]
    monkeypatch.setattr(_pipeline.state, "write_submitted_ledger",
                        _failing_ledger_writer(flag))
    first = place_candidate(_cand(), client=paper_client, dry_run=False)
    assert first.recoverable
    # Re-fire: same ticker, dangling intent present → reject (no undo needed;
    # the guard fires before the ledger write).
    # Re-fire: same ticker, dangling intent present → reject.
    second = place_candidate(_cand(), client=paper_client, dry_run=False)
    assert second.status == "rejected"
    assert "unresolved write-ahead intent" in second.reason
    # Only ONE broker order was ever placed.
    place_calls = [c for c in paper_client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1


# --------------------------------------------------------------------------- status=intent recovery


def test_status_intent_no_match_needs_quorum_to_abandon(paper_dirs):
    """A status=intent record is only abandoned after ABANDON_QUORUM confirmed-
    empty sweeps ON DISTINCT DATES, and ONLY when the broker is proven alive
    (account_alive=True) — never on a blackout."""
    from datetime import date
    from tools.auto_paper.reconcile import ABANDON_QUORUM
    assert ABANDON_QUORUM >= 2
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-AAA-1", ticker="AAA", setup_type="EP",
        setup_grade=None, pivot_price=10, limit_price=10.05, stop_price=9,
        target_price=12, shares=5, status=intent_log.STATUS_INTENT,
        created_at="2026-06-19T00:00:00+00:00",
    ))
    # distinct post-creation dates; broker proven alive but genuinely empty.
    days = [date(2026, 6, 20 + i) for i in range(ABANDON_QUORUM)]
    for i, d in enumerate(days[:-1], start=1):
        res = reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                                account_alive=True, today=d)
        assert res[0].action == "unresolved"
        rec = intent_log.load_intent("ap-AAA-1")
        assert rec.status == intent_log.STATUS_INTENT
        assert rec.empty_sweep_count == i
    res = reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                            account_alive=True, today=days[-1])
    assert res[0].action == "abandoned"
    assert intent_log.load_intent("ap-AAA-1").status == intent_log.STATUS_ABANDONED


# ---- FIX 1 (load-bearing): held-recovery must place a protective stop AND be
# visible to a safety net — never a held, unstopped, invisible position.


def test_held_recovery_places_stop_and_is_visible(paper_dirs):
    from tools.broker.tiger import TigerClient
    tiger = TigerClient(_trade_client=FakeTradeClient())
    intent_log.write_intent(_intent("ap-HS-1", "HS", stop_price=90.0,
                                    limit_price=100.0, shares=5))
    res = reconcile_intents(client=tiger, open_orders=[], filled_orders=[],
                            holdings={"HS": 5})
    assert res[0].action == "ledgered_recovered"
    # (a) a protective STP SELL was actually placed on the held qty
    led = state.load_ledger("HS")
    assert led["position_state"].get("stop_order_id") is not None
    place_calls = [c for c in tiger._tc.calls if c[0] == "place_order"]
    assert place_calls, "no broker order placed — held position left UNSTOPPED"
    # (b) visible to a safety net: starter state + positions.json starter row
    assert led["meta"]["state"] == "starter"
    pj = {p["ticker"]: p for p in state.load_positions_json()["positions"]}
    assert pj.get("HS", {}).get("stage") == "starter"


# ---- FIX 2 (load-bearing): never abandon on a broker blackout.


def test_blackout_never_abandons_not_held_resting_order(paper_dirs):
    """Total blackout (all broker reads empty AND account not provably alive)
    across >quorum sweeps on a NOT-held order → never abandoned (stays
    unresolved + would page). This is the v1-orphan-recreation path."""
    from datetime import date
    from tools.auto_paper.reconcile import ABANDON_QUORUM
    intent_log.write_intent(_intent("ap-BL-1", "BL", created_at="2026-06-19T00:00:00+00:00"))
    for i in range(ABANDON_QUORUM + 2):
        res = reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                                account_alive=False, today=date(2026, 6, 20 + i))
        assert res[0].action == "unresolved"
        assert intent_log.load_intent("ap-BL-1").status == intent_log.STATUS_INTENT


# ---- FIX 3: consecutive (not cumulative) counting — reset on recovery; no
# same-day double-count.


def test_unrelated_book_data_does_NOT_reset_counter(paper_dirs):
    """FIX 6: unrelated book data is NOT a reset — it's a CORROBORATED absence
    (broker demonstrably live; THIS order genuinely nowhere) → the tally advances.
    Resetting on unrelated data made abandon unreachable in any live book."""
    from datetime import date
    intent_log.write_intent(_intent("ap-RS-1", "RS", created_at="2026-06-19T00:00:00+00:00"))
    reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                      account_alive=True, today=date(2026, 6, 20))
    assert intent_log.load_intent("ap-RS-1").empty_sweep_count == 1
    # day2: broker shows REAL (unrelated) orders → still a corroborated absence of
    # OUR order → count advances to 2 (does NOT reset to 0).
    reconcile_intents(open_orders=[{"order_id": 1, "symbol": "OTHER", "action": "BUY",
                                    "quantity": 9, "limit_price": 5.0}],
                      filled_orders=[], holdings={}, account_alive=True, today=date(2026, 6, 21))
    assert intent_log.load_intent("ap-RS-1").status == intent_log.STATUS_ABANDONED


def test_blackout_resets_counter(paper_dirs):
    """FIX 6: a blackout (NOT broker_alive) resets the tally — don't accumulate
    confirmed-absence during an outage."""
    from datetime import date
    intent_log.write_intent(_intent("ap-BR-1", "BR", created_at="2026-06-19T00:00:00+00:00"))
    reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                      account_alive=True, today=date(2026, 6, 20))
    assert intent_log.load_intent("ap-BR-1").empty_sweep_count == 1
    reconcile_intents(open_orders=[], filled_orders=[], holdings={},
                      account_alive=False, today=date(2026, 6, 21))  # blackout
    assert intent_log.load_intent("ap-BR-1").empty_sweep_count == 0
    assert intent_log.load_intent("ap-BR-1").status == intent_log.STATUS_INTENT


def test_abandon_reachable_with_unrelated_position_held(paper_dirs):
    """FIX 6 regression: a never-placed intent MUST still be abandonable while an
    UNRELATED position is held — the v3 whole-book reset wedged this forever."""
    from datetime import date
    intent_log.write_intent(_intent("ap-UR-1", "UR", created_at="2026-06-19T00:00:00+00:00"))
    for d in (date(2026, 6, 20), date(2026, 6, 21)):
        reconcile_intents(open_orders=[], filled_orders=[], holdings={"OTHER": 10},
                          account_alive=True, today=d)
    assert intent_log.load_intent("ap-UR-1").status == intent_log.STATUS_ABANDONED


def test_resting_order_not_abandoned_within_window(paper_dirs):
    """FIX 6 / v2-#1 guard: across the in-window sweeps (creation day + first
    counted day) a soft-emptied resting order is NOT yet abandoned."""
    from datetime import date
    intent_log.write_intent(_intent("ap-RST-1", "RST", created_at="2026-06-20T00:00:00+00:00"))
    reconcile_intents(open_orders=[], filled_orders=[], holdings={"OTHER": 5},
                      account_alive=True, today=date(2026, 6, 20))   # creation day → not counted
    reconcile_intents(open_orders=[], filled_orders=[], holdings={"OTHER": 5},
                      account_alive=True, today=date(2026, 6, 21))   # count 1 < quorum
    assert intent_log.load_intent("ap-RST-1").status == intent_log.STATUS_INTENT


def test_held_recovery_books_average_cost_not_limit(paper_dirs):
    """FOLD-IN #7: a gap-through held-recovery books the broker's average_cost as
    the entry, not the limit_price placeholder (which would corrupt calibration)."""
    from tools.broker.tiger import TigerClient
    tiger = TigerClient(_trade_client=FakeTradeClient())
    intent_log.write_intent(_intent("ap-GAP-1", "GAP", limit_price=100.0, stop_price=90.0, shares=5))
    reconcile_intents(client=tiger, open_orders=[], filled_orders=[],
                      holdings={"GAP": 5}, holdings_avg_cost={"GAP": 85.0})
    led = state.load_ledger("GAP")
    assert led["position_state"]["starter"]["fill_price"] == 85.0
    pj = {p["ticker"]: p for p in state.load_positions_json()["positions"]}
    assert pj["GAP"]["entry_price"] == 85.0


def test_same_day_double_call_does_not_double_count(paper_dirs):
    from datetime import date
    intent_log.write_intent(_intent("ap-SD-1", "SD", created_at="2026-06-19T00:00:00+00:00"))
    d = date(2026, 6, 20)
    reconcile_intents(open_orders=[], filled_orders=[], holdings={}, account_alive=True, today=d)
    reconcile_intents(open_orders=[], filled_orders=[], holdings={}, account_alive=True, today=d)
    # two same-day calls → counted once, not abandoned
    rec = intent_log.load_intent("ap-SD-1")
    assert rec.empty_sweep_count == 1
    assert rec.status == intent_log.STATUS_INTENT


def test_status_intent_with_broker_match_adopted(paper_dirs):
    """A `intent`-status record whose order DID reach the broker (id unknown)
    is matched by EXACT symbol+BUY+qty+limit and adopted."""
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-BBB-1", ticker="BBB", setup_type="EP",
        setup_grade=None, pivot_price=10, limit_price=10.05, stop_price=9,
        target_price=12, shares=5, status=intent_log.STATUS_INTENT,
    ))
    open_orders = [{"order_id": 777, "symbol": "BBB", "action": "BUY",
                    "quantity": 5, "limit_price": 10.05}]
    res = reconcile_intents(open_orders=open_orders, filled_orders=[], holdings={})
    assert res[0].action == "ledgered_recovered"
    assert res[0].broker_order_id == 777
    assert state.ledger_exists("BBB")


def _intent(cloid, ticker, **over):
    base = dict(client_order_id=cloid, ticker=ticker, setup_type="EP",
                setup_grade=None, pivot_price=10, limit_price=10.05, stop_price=9,
                target_price=12, shares=5, status=intent_log.STATUS_INTENT)
    base.update(over)
    return intent_log.IntentRecord(**base)


def test_match_rejects_blank_action_and_missing_limit(paper_dirs):
    """FIX 2: a blank/missing action or a missing limit_price must NOT match —
    so an order without those fields can't be wrongly adopted."""
    intent_log.write_intent(_intent("ap-X-1", "XX"))
    # blank action + no limit: previously a wildcard match, now rejected → no
    # match → first sweep is unresolved (quorum), NOT an adoption.
    bad = [{"order_id": 1, "symbol": "XX", "action": "", "quantity": 5}]
    res = reconcile_intents(open_orders=bad, filled_orders=[], holdings={})
    assert res[0].action == "unresolved"
    assert not state.ledger_exists("XX")


def test_match_ambiguous_does_not_adopt(paper_dirs):
    """FIX 2: two orders matching (symbol/qty/limit) → ambiguous → refuse to
    guess; leave unresolved, never adopt one arbitrarily."""
    intent_log.write_intent(_intent("ap-Y-1", "YY"))
    two = [
        {"order_id": 11, "symbol": "YY", "action": "BUY", "quantity": 5, "limit_price": 10.05},
        {"order_id": 12, "symbol": "YY", "action": "BUY", "quantity": 5, "limit_price": 10.05},
    ]
    res = reconcile_intents(open_orders=two, filled_orders=[], holdings={})
    assert res[0].action == "unresolved"
    assert "multiple" in res[0].reason.lower()
    assert not state.ledger_exists("YY")


def test_match_by_user_mark_tag_is_origin_proof(paper_dirs):
    """FIX 4: an order carrying the intent's cloid as user_mark is adopted by
    EXACT TAG ECHO — origin proof that can't collide with a human order."""
    intent_log.write_intent(_intent("ap-TAG-1", "TG"))
    # A heuristically-matching order but WRONG/absent qty+limit, yet the tag
    # matches → adopted on the tag alone.
    tagged = [{"order_id": 4242, "symbol": "TG", "action": "BUY",
               "quantity": 999, "limit_price": 1.23, "user_mark": "ap-TAG-1"}]
    res = reconcile_intents(open_orders=tagged, filled_orders=[], holdings={})
    assert res[0].action == "ledgered_recovered"
    assert res[0].broker_order_id == 4242
    assert state.ledger_exists("TG")


def test_match_excludes_human_track_order(paper_dirs, monkeypatch):
    """FIX 2 origin guard: an order id belonging to the human-discretionary
    track is never adopted, even on an exact symbol/qty/limit match."""
    from tools.auto_paper import reconcile as _rec
    monkeypatch.setattr(_rec, "_human_track_order_ids", lambda: {999})
    intent_log.write_intent(_intent("ap-Z-1", "ZZ"))
    human = [{"order_id": 999, "symbol": "ZZ", "action": "BUY", "quantity": 5, "limit_price": 10.05}]
    res = reconcile_intents(open_orders=human, filled_orders=[], holdings={})
    assert res[0].action == "unresolved"   # excluded → no match → quorum
    assert not state.ledger_exists("ZZ")


def test_held_symbol_reconstructs_instead_of_abandoning(paper_dirs):
    """FIX 4 spirit: a status=intent with no matching order BUT the broker holds
    the symbol → the fill landed (id lost) → reconstruct, never abandon."""
    intent_log.write_intent(_intent("ap-H-1", "HH"))
    res = reconcile_intents(open_orders=[], filled_orders=[], holdings={"HH": 5})
    assert res[0].action == "ledgered_recovered"
    assert state.ledger_exists("HH")
    assert intent_log.load_intent("ap-H-1").status == intent_log.STATUS_LEDGERED


def test_ledger_already_present_advances_intent(paper_dirs):
    """If a ledger already exists (write succeeded; only the intent flip was
    lost), the sweep just advances the intent to terminal — no duplicate."""
    state.write_submitted_ledger(
        ticker="CCC", setup_type="EP", setup_grade=None, pivot_price=10,
        limit_price=10.05, stop_price=9, shares=5, broker_order_id=555,
        broker="tiger_paper",
    )
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-CCC-1", ticker="CCC", setup_type="EP",
        setup_grade=None, pivot_price=10, limit_price=10.05, stop_price=9,
        target_price=12, shares=5, broker_order_id=555,
        status=intent_log.STATUS_PLACED,
    ))
    res = reconcile_intents(open_orders=[], filled_orders=[], holdings={})
    assert res[0].action == "ledgered_already"
    assert intent_log.load_intent("ap-CCC-1").status == intent_log.STATUS_LEDGERED
