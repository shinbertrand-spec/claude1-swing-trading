"""Acceptance harness — write-ahead order/ledger reconciliation invariant.

Proves the anti-orphan invariant across >=3 simulated paper sessions:

    N intended  ->  N placed at broker  ->  N ledger rows
                ->  all reconciled to fill-or-cancel
                ->  ZERO orphans  ->  silent-failure alarm GREEN

The real ``place_candidate`` + ``reconcile_intents`` + ``reconcile_today`` +
health-snapshot code paths run unchanged; only the broker SDK client is faked
(the live Tiger paper account is not reachable from CI, and — more importantly —
the broker cannot be told to *crash the process between place and ledger-write*
on demand). Session 1 injects exactly that crash so the orphan-recovery path is
actually exercised, not just asserted about.

Run::

    uv run python -m scripts.acceptance_auto_paper_orphan

Exits 0 iff every assertion holds.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.auto_paper import intent_log, pipeline, screener as screener_mod, state
from tools.auto_paper import reconcile
from tools.auto_paper.pipeline import CandidateInput, place_candidate
from tools.broker.tiger import TigerClient
from tools.observability import health_alerts, health_snapshot


# --------------------------------------------------------------------------- fake broker


class FakeBroker:
    """Deterministic stand-in for the tigeropen trade client (the SDK seam the
    real TigerClient wraps). Holds an order book + holdings the harness drives."""

    def __init__(self):
        self.orders: dict[int, dict] = {}
        self.holdings: dict[str, float] = {}
        self.avg_cost: dict[str, float] = {}
        self._next = 20_000
        self.lying = False        # when True the reads return [] (soft-fail-empty)
        self.reject_next = False  # when True the next place_order raises
        self.reject_stops = False  # when True any STP order placement raises

    # ---- reads
    def get_assets(self, *, account, segment=False, **_):
        # A total blackout zeroes the account too, so account-liveness can't be
        # confirmed (the abandon path must refuse to fire).
        if self.lying:
            return [NS(summary=NS(
                cash=0.0, available_funds=0.0, buying_power=0.0,
                net_liquidation=0.0, gross_position_value=0.0, currency="USD"))]
        return [NS(summary=NS(
            cash=900_000.0, available_funds=900_000.0, buying_power=1_800_000.0,
            net_liquidation=1_000_000.0, gross_position_value=100_000.0,
            currency="USD"))]

    def get_positions(self, *, account, **_):
        if self.lying:
            return []
        return [NS(contract=NS(symbol=s), quantity=q,
                   average_cost=self.avg_cost.get(s, 0.0),
                   market_value=0.0, unrealized_pnl=0.0)
                for s, q in self.holdings.items() if abs(q) >= 1]

    def get_open_orders(self, *, account, **_):
        if self.lying:
            return []
        return [self._as_ns(o) for o in self.orders.values() if o["status"] == "Submitted"]

    def get_filled_orders(self, *, account, start_time=None, end_time=None, symbol=None, **_):
        if self.lying:
            return []
        return [self._as_ns(o) for o in self.orders.values() if o["status"] == "Filled"]

    def get_contract(self, *, symbol, **_):
        return NS(symbol=symbol, sec_type="STK", currency="USD")

    # ---- writes
    def place_order(self, order):
        if self.reject_next:
            self.reject_next = False
            raise RuntimeError("INSUFFICIENT_FUNDS (injected reject)")
        limit = getattr(order, "limit_price", None)
        aux = getattr(order, "aux_price", None)
        if self.reject_stops and aux is not None:
            raise RuntimeError("STP rejected (injected)")
        oid = self._next
        self._next += 1
        order.id = oid
        self.orders[oid] = {
            "order_id": oid,
            "symbol": order.contract.symbol,
            "action": order.action,
            "quantity": float(order.quantity),
            "limit_price": float(limit) if limit is not None else None,
            "order_type": "STP" if aux is not None else "LMT",
            "status": "Submitted",
            "filled": 0.0,
            "avg_fill_price": None,
        }
        return oid

    def cancel_order(self, *, account, id):
        if id in self.orders:
            self.orders[id]["status"] = "Cancelled"
        return id

    # ---- harness drivers
    def fill(self, oid: int, price: float):
        o = self.orders[oid]
        o["status"] = "Filled"
        o["filled"] = o["quantity"]
        o["avg_fill_price"] = price
        if o["action"] == "BUY":
            self.holdings[o["symbol"]] = self.holdings.get(o["symbol"], 0.0) + o["quantity"]

    def partial_fill(self, oid: int, qty: float, price: float):
        o = self.orders[oid]
        o["status"] = "Filled"
        o["filled"] = qty
        o["avg_fill_price"] = price
        if o["action"] == "BUY":
            self.holdings[o["symbol"]] = self.holdings.get(o["symbol"], 0.0) + qty

    def fill_outside_lookback(self, oid: int, price: float):
        """Fill that lands in HOLDINGS but is NOT returned by get_filled_orders
        (status != 'Filled') — simulates a fill older than the reconcile window."""
        o = self.orders[oid]
        o["status"] = "FilledOld"
        o["filled"] = o["quantity"]
        o["avg_fill_price"] = price
        if o["action"] == "BUY":
            self.holdings[o["symbol"]] = self.holdings.get(o["symbol"], 0.0) + o["quantity"]

    def expire(self, oid: int):
        # DAY order expires unfilled: gone from open, never in filled.
        self.orders[oid]["status"] = "Cancelled"

    def inject_open_buy(self, symbol: str, qty: float, limit: float) -> int:
        """Place a resting BUY order directly (simulates an order the broker
        accepted during a crash where our process never recorded the id)."""
        oid = self._next
        self._next += 1
        self.orders[oid] = {
            "order_id": oid, "symbol": symbol, "action": "BUY",
            "quantity": float(qty), "limit_price": float(limit),
            "order_type": "LMT", "status": "Submitted", "filled": 0.0,
            "avg_fill_price": None,
        }
        return oid

    def open_buy_ids(self):
        return [oid for oid, o in self.orders.items()
                if o["status"] == "Submitted" and o["action"] == "BUY"]

    @staticmethod
    def _as_ns(o: dict) -> NS:
        return NS(id=o["order_id"], contract=NS(symbol=o["symbol"]),
                  action=o["action"], order_type=o["order_type"],
                  quantity=o["quantity"], filled=o["filled"],
                  avg_fill_price=o["avg_fill_price"], limit_price=o["limit_price"])


# --------------------------------------------------------------------------- env setup


def _clean_screener(ticker, claimed_sector_etf):
    return screener_mod.ScreenerResult(
        ticker=ticker, blocked=False, blocking_checks=[], corrected_sector_etf=None,
        checks=[screener_mod.CheckResult(check="litigation", passed=True)],
        computed_at="2026-06-10T00:00:00+00:00",
    )


def _setup_isolated_env(root: Path) -> None:
    state.PAPER_AUTO_LEDGER_DIR = str(root / "ledgers" / "paper-auto")
    state.PAPER_AUTO_POSITIONS_JSON = str(root / "journal" / "paper-auto" / "positions.json")
    pipeline.config.is_deployable = lambda t: t == "EP"
    pipeline._resolve_regime_multiplier = lambda: ("stage_2_confirmed", 1.0)
    pipeline._run_screener = _clean_screener


def _cand(ticker, px, sector="XLK") -> CandidateInput:
    return CandidateInput(
        ticker=ticker, setup_type="EP", setup_grade="A",
        pivot_price=px, limit_price=round(px * 1.001, 2), stop_price=round(px * 0.92, 2),
        target_price=round(px * 1.18, 2), shares=5, sector_etf=sector,
    )


def _write_run_artifacts(runs_dir: Path, run_id: str, results: list[dict],
                         started_iso: str) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "07_placement_results.yml").write_text(
        yaml.safe_dump({"results": results,
                        "n_placed": sum(1 for r in results if r.get("placed"))}))
    (d / "_status.yml").write_text(yaml.safe_dump({
        "run_id": run_id, "run_started_at": started_iso,
        "last_phase_completed": "post_panel", "errors": [],
    }))
    return d


# --------------------------------------------------------------------------- harness


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ap_orphan_acc_"))
    _setup_isolated_env(tmp)
    runs_dir = tmp / "ledgers" / "_auto_paper_runs"
    positions_json = Path(state.PAPER_AUTO_POSITIONS_JSON)

    broker = FakeBroker()
    tiger = TigerClient(_trade_client=broker)
    real_write = state.write_submitted_ledger

    # 3 sessions, fresh tickers each so the per-ticker ledger-exists dedup
    # doesn't fire (each session is a distinct trade name).
    sessions = [
        ("RUN1", ["AAA", "BBB", "CCC"], "AAA"),   # AAA's ledger write CRASHES (orphan)
        ("RUN2", ["DDD", "EEE", "FFF"], None),
        ("RUN3", ["GGG", "HHH", "III"], None),
    ]
    # Scheduled-window ET start (Wed 9:35 EDT = 13:35 UTC) so a clean run is
    # eligible to alarm — proving GREEN is meaningful, not vacuous.
    base_day = date(2026, 6, 10)

    total_intended = 0
    total_placed_at_broker_before = 0
    per_session_log: list[str] = []

    for i, (run_id, tickers, crash_ticker) in enumerate(sessions):
        started_iso = f"{(base_day + timedelta(days=i)).isoformat()}T13:35:1{i}+00:00"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []

        # --- pre-session intent recovery (mirrors run_entry.phase_init) ---
        reconcile.reconcile_intents(client=tiger)

        # --- place each candidate ---
        for t in tickers:
            total_intended += 1
            if t == crash_ticker:
                # Inject a ledger-write crash AFTER the broker accepts the order.
                def _boom(**kw):
                    raise state.PaperAutoStateError("injected disk failure (acceptance)")
                state.write_submitted_ledger = _boom
                try:
                    res = place_candidate(_cand(t, 100 + total_intended),
                                          client=tiger, dry_run=False,
                                          auto_paper_run_dir=str(run_dir))
                finally:
                    state.write_submitted_ledger = real_write
            else:
                res = place_candidate(_cand(t, 100 + total_intended),
                                      client=tiger, dry_run=False,
                                      auto_paper_run_dir=str(run_dir))
            results.append({
                "ticker": t, "status": res.status, "placed": res.status == "placed",
                "broker_order_id": res.broker_order_id, "ledger_path": res.ledger_path,
                "reason": res.reason, "recoverable": res.recoverable,
                "client_order_id": res.client_order_id,
            })

        total_placed_at_broker_before = len(broker.open_buy_ids())

        # --- recover the injected orphan (next pre-session sweep equivalent) ---
        rec_intents = reconcile.reconcile_intents(client=tiger)

        # --- settle the broker: fill 2 of 3 BUY orders, expire 1 ---
        buy_ids = sorted(broker.open_buy_ids())
        for oid in buy_ids[:2]:
            broker.fill(oid, broker.orders[oid]["limit_price"])
        for oid in buy_ids[2:]:
            broker.expire(oid)

        # --- EOD reconcile (submitted -> starter on fill / closed_unfilled on expiry) ---
        recs = reconcile.reconcile_today(client=tiger)

        _write_run_artifacts(runs_dir, run_id, results, started_iso)
        per_session_log.append(
            f"  {run_id}: intended={len(tickers)} "
            f"crash_injected={crash_ticker or '-'} "
            f"intent_recoveries={[ (r.ticker, r.action) for r in rec_intents ]} "
            f"reconcile_actions={sorted({r.action for r in recs})}"
        )

    # ----------------------------------------------------------------- assertions
    print("=" * 72)
    print("ACCEPTANCE: auto-paper write-ahead orphan reconciliation")
    print("=" * 72)
    for line in per_session_log:
        print(line)
    print("-" * 72)

    failures: list[str] = []

    # Gather final state.
    all_tickers = [t for _, ts, _ in sessions for t in ts]
    ledgers = {t: state.ledger_exists(t) for t in all_tickers}
    pj = state.load_positions_json().get("positions", [])
    by_ticker_stage = {p["ticker"]: (p.get("stage") or "").lower() for p in pj}
    unresolved = intent_log.unresolved_intents()
    all_intents = intent_log.iter_intents()

    # Broker truth — SIGNED model (2026-07-24 naked-short incident): "held"
    # means broker holds LONG >= 1. A short is never "held"; it is an anomaly
    # asserted empty below so the acceptance harness exercises the new
    # invariant instead of green-lighting the old abs() semantics.
    held = {s for s, q in broker.holdings.items() if q >= 1}
    short_set = {s for s, q in broker.holdings.items() if q <= -1}
    open_buy = {broker.orders[o]["symbol"] for o in broker.open_buy_ids()}

    n_intended = total_intended
    n_placed = total_intended  # every intended candidate reached the broker (incl. the crashed one)
    n_ledgers = sum(1 for v in ledgers.values() if v)

    def check(cond: bool, label: str):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {label}")
        if not cond:
            failures.append(label)

    # 0. Long-only invariant (signed model): the acceptance scenarios must
    #    never leave the fake broker SHORT — a short here means a harness or
    #    reconciler regression toward the 2026-07 incident class.
    check(not short_set, f"no broker shorts (short_set = {sorted(short_set) or '{}'})")

    # 1. N intended -> N placed at broker.
    placed_buy_orders = sum(1 for o in broker.orders.values() if o["action"] == "BUY")
    check(placed_buy_orders == n_intended,
          f"N intended ({n_intended}) == N BUY orders at broker ({placed_buy_orders})")

    # 2. N placed -> N ledger rows (every intended name has a canonical ledger,
    #    including the one whose first ledger-write crashed).
    check(n_ledgers == n_intended,
          f"N placed ({n_intended}) == N ledger rows ({n_ledgers})")

    # 3. All reconciled to fill-or-cancel: no ledger left in `submitted`; every
    #    name is in a terminal/active reconciled state.
    ledger_states = {t: state.load_ledger(t)["meta"]["state"] for t in all_tickers}
    stuck_submitted = [t for t, s in ledger_states.items() if s == "submitted"]
    check(not stuck_submitted,
          f"no ledger stuck in 'submitted' after reconcile (stuck={stuck_submitted})")
    reconciled_states = {"starter", "closed"}
    bad_states = {t: s for t, s in ledger_states.items() if s not in reconciled_states}
    check(not bad_states,
          f"every ledger in a reconciled state (starter/closed) — offenders={bad_states}")

    # closed_unfilled is a CLEAN reconciled terminal in positions.json (not a
    # black hole): the expired orders landed there.
    closed_unfilled = [t for t, s in by_ticker_stage.items() if s == "closed_unfilled"]
    check(len(closed_unfilled) == len(sessions),
          f"legitimate no-fills reconciled cleanly to closed_unfilled "
          f"({len(closed_unfilled)} expected {len(sessions)})")

    # 4. ZERO orphans: every broker holding + open BUY order has a ledger; no
    #    dangling intents.
    held_without_ledger = sorted(s for s in held if not state.ledger_exists(s))
    open_without_ledger = sorted(s for s in open_buy if not state.ledger_exists(s))
    check(not held_without_ledger,
          f"every broker HOLDING has a ledger (orphans={held_without_ledger})")
    check(not open_without_ledger,
          f"every open BUY order has a ledger (orphans={open_without_ledger})")
    check(not unresolved,
          f"ZERO dangling write-ahead intents ({[r.client_order_id for r in unresolved]})")
    check(all(r.is_terminal for r in all_intents),
          "every intent in a terminal state (ledgered/reconciled/abandoned)")

    # 5. Silent-failure alarm GREEN on every entry session.
    alarms = []
    for run_id, _, _ in sessions:
        sf = health_snapshot.compute_silent_failure(
            runs_dir / run_id, positions_json=positions_json)
        if sf.alarm or sf.orphan_intents:
            alarms.append((run_id, sf.reason))
    check(not alarms, f"silent-failure alarm GREEN on all sessions (alarms={alarms})")

    print("-" * 72)
    print(f"  intended={n_intended}  placed_at_broker={placed_buy_orders}  "
          f"ledger_rows={n_ledgers}  held={len(held)}  "
          f"closed_unfilled={len(closed_unfilled)}  dangling_intents={len(unresolved)}")
    print("=" * 72)
    if failures:
        print(f"RESULT: FAIL — {len(failures)} assertion(s) failed")
        return 1
    print("RESULT: PASS — N intended -> N placed -> N ledgers -> all reconciled, "
          "ZERO orphans, alarm GREEN")
    return 0


def run_adversarial() -> int:
    """Extended sessions the always-truthful fake broker could not exercise —
    the failure modes the orphan-recovery review flagged."""
    from datetime import datetime, timedelta, timezone

    from tools.auto_paper import intent_log
    tmp = Path(tempfile.mkdtemp(prefix="ap_orphan_adv_"))
    _setup_isolated_env(tmp)
    positions_json = Path(state.PAPER_AUTO_POSITIONS_JSON)
    broker = FakeBroker()
    tiger = TigerClient(_trade_client=broker)

    print("=" * 72)
    print("ACCEPTANCE (adversarial): orphan-recovery failure modes")
    print("=" * 72)
    fails: list[str] = []

    def check(cond, label):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    # (a) LYING BROKER — a genuinely-live BUY exists, but the broker read returns
    # [] (soft-fail). A status=intent record must NOT be abandoned on that sweep,
    # and a re-fire must NOT double-place. Once the broker tells the truth, the
    # order is adopted.
    broker.inject_open_buy("AAA", 5, 100.10)
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-AAA-adv", ticker="AAA", setup_type="EP", setup_grade="A",
        pivot_price=100.0, limit_price=100.10, stop_price=92.0, target_price=118.0,
        shares=5, sector_etf="XLK", status=intent_log.STATUS_INTENT))
    broker.lying = True
    # TWO consecutive blackout sweeps on a NOT-held resting order → still alive.
    reconcile.reconcile_intents(client=tiger)
    res = reconcile.reconcile_intents(client=tiger)
    rec = intent_log.load_intent("ap-AAA-adv")
    check(res and res[0].action == "unresolved" and rec.status == intent_log.STATUS_INTENT,
          "blackout x2 (not-held resting order): NOT abandoned (stays unresolved + pages)")
    check(not state.ledger_exists("AAA"),
          "lying broker: no ledger fabricated for the unconfirmed order")
    # re-fire place for AAA → blocked by the dangling-intent guard (no duplicate)
    dup = place_candidate(_cand("AAA", 100.0), client=tiger, dry_run=False,
                          auto_paper_run_dir=str(tmp / "RUNADV"))
    check(dup.status == "rejected" and "unresolved" in (dup.reason or ""),
          "lying broker: re-fire BLOCKED by dangling-intent guard (no double-place)")
    check(len(broker.open_buy_ids()) == 1,
          "lying broker: still exactly ONE live BUY order at broker (no duplicate)")
    # broker tells the truth → the order is uniquely adopted
    broker.lying = False
    res2 = reconcile.reconcile_intents(client=tiger)
    check(res2 and res2[0].action == "ledgered_recovered" and state.ledger_exists("AAA"),
          "truth restored: the live order is adopted + ledgered (recovered, not lost)")

    # (b) PARTIAL FILL — submitted order partially fills → starter at filled qty.
    p = place_candidate(_cand("PRT", 50.0), client=tiger, dry_run=False,
                        auto_paper_run_dir=str(tmp / "RUNADV"))
    broker.partial_fill(p.broker_order_id, qty=3, price=50.05)
    reconcile.reconcile_today(client=tiger)
    led = state.load_ledger("PRT")
    check(led["meta"]["state"] == "starter"
          and int(led["position_state"]["starter"]["shares"]) == 3,
          "partial fill: ledger -> starter at the FILLED qty (3), not requested (5)")

    # (c) BROKER REJECT — place_order raises → intent abandoned by the pipeline,
    # no ledger, nothing dangling, and a later re-place is unblocked + single.
    broker.reject_next = True
    rej = place_candidate(_cand("REJ", 70.0), client=tiger, dry_run=False,
                          auto_paper_run_dir=str(tmp / "RUNADV"))
    rej_intent = next((i for i in intent_log.iter_intents() if i.ticker == "REJ"), None)
    check(rej.status == "error" and not state.ledger_exists("REJ")
          and rej_intent is not None and rej_intent.status == intent_log.STATUS_ABANDONED,
          "broker reject: intent ABANDONED cleanly, no ledger, nothing dangling")
    check(not any(o["symbol"] == "REJ" for o in broker.orders.values()),
          "broker reject: NO order at broker (it never placed)")

    # (d) FILL OLDER THAN LOOKBACK — order gone from open+filled window but the
    # broker HOLDS the symbol → must NOT expire to closed_unfilled.
    d = place_candidate(_cand("OLD", 80.0), client=tiger, dry_run=False,
                        auto_paper_run_dir=str(tmp / "RUNADV"))
    broker.fill_outside_lookback(d.broker_order_id, price=80.05)
    recs = reconcile.reconcile_today(client=tiger)
    old_actions = [r.action for r in recs if r.ticker == "OLD"]
    old_state = state.load_ledger("OLD")["meta"]["state"]
    check("held_no_expire" in old_actions and old_state == "submitted",
          "old fill: held symbol NOT expired to closed_unfilled (stays submitted)")

    # (f) HELD-RECOVERY (FIX 1) — status=intent, no order match, broker HOLDS the
    # symbol → reconstruct into STARTER + place a protective STP; visible to a
    # safety net. Never a held, unstopped, invisible position.
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-HELD-adv", ticker="HELD", setup_type="EP", setup_grade="A",
        pivot_price=40.0, limit_price=40.0, stop_price=36.0, target_price=48.0,
        shares=4, sector_etf="XLK", status=intent_log.STATUS_INTENT))
    broker.holdings["HELD"] = 4
    broker.avg_cost["HELD"] = 44.5     # gapped through the 40.0 limit
    reconcile.reconcile_intents(client=tiger)
    held_led = state.load_ledger("HELD")
    stp_placed = any(o["symbol"] == "HELD" and o["order_type"] == "STP"
                     for o in broker.orders.values())
    check(held_led["meta"]["state"] == "starter" and stp_placed
          and held_led["position_state"].get("stop_order_id") is not None,
          "held-recovery: reconstructed into STARTER + protective STP placed (visible, stopped)")
    check(held_led["position_state"]["starter"]["fill_price"] == 44.5,
          "held-recovery (FOLD-IN #7): books broker average_cost (44.5), not the 40.0 limit")
    starters = [p for p in reconcile._starter_positions() if p.get("ticker") == "HELD"]
    check(len(starters) == 1,
          "held-recovery: position visible to a safety net (_starter_positions)")

    # (h) FIX 6 — abandon IS reachable for a never-placed intent WHILE an
    # unrelated position is held (the v3 whole-book-reset wedge). Live broker,
    # unrelated holding, two distinct dates → abandon.
    from datetime import date as _date
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-NEVER-adv", ticker="NEVER", setup_type="EP", setup_grade="A",
        pivot_price=20.0, limit_price=20.0, stop_price=18.0, target_price=24.0,
        shares=1, sector_etf="XLK", status=intent_log.STATUS_INTENT,
        created_at="2026-06-19T00:00:00+00:00"))
    for dd in (_date(2026, 6, 20), _date(2026, 6, 21)):
        reconcile.reconcile_intents(client=tiger, today=dd, account_alive=True)
    check(intent_log.load_intent("ap-NEVER-adv").status == intent_log.STATUS_ABANDONED,
          "FIX 6: never-placed intent IS abandonable while an unrelated position is held")

    # (i) FOLD-IN #1 — a held starter whose stop placement FAILED is surfaced on
    # the pageable health surface (naked, believed-protected-but-isn't).
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-NAKED-adv", ticker="NAKED", setup_type="EP", setup_grade="A",
        pivot_price=10.0, limit_price=10.0, stop_price=9.0, target_price=12.0,
        shares=2, sector_etf="XLK", status=intent_log.STATUS_INTENT))
    broker.holdings["NAKED"] = 2
    broker.avg_cost["NAKED"] = 10.0
    broker.reject_stops = True       # the protective STP placement fails
    reconcile.reconcile_intents(client=tiger)
    broker.reject_stops = False
    naked = health_snapshot.scan_unprotected_starters(
        paper_auto_ledgers=Path(state.PAPER_AUTO_LEDGER_DIR))
    check(any(n["ticker"] == "NAKED" for n in naked),
          "FOLD-IN #1: a held starter with a failed stop is surfaced as NAKED (pageable)")

    # (g) SAME-DAY DOUBLE CALL (FIX 3) — a fresh status=intent created today is
    # NOT abandoned by two same-day live-broker sweeps (min-age + one-per-day).
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-SAME-adv", ticker="SAME", setup_type="EP", setup_grade="A",
        pivot_price=30.0, limit_price=30.0, stop_price=27.0, target_price=36.0,
        shares=1, sector_etf="XLK", status=intent_log.STATUS_INTENT))
    reconcile.reconcile_intents(client=tiger)   # live broker, SAME not held/ordered
    reconcile.reconcile_intents(client=tiger)
    check(intent_log.load_intent("ap-SAME-adv").status == intent_log.STATUS_INTENT,
          "same-day double call: fresh intent NOT abandoned (min-age + one-per-day guard)")

    # (e) PERSISTENT ORPHAN RE-PAGES — a dangling intent on disk pages, suppresses
    # within backoff (same run_id!), then re-pages after the backoff window.
    # Reuse the still-unresolved-then-resolved? Create a fresh dangling intent.
    intent_log.write_intent(intent_log.IntentRecord(
        client_order_id="ap-PAGE-adv", ticker="PAGE", setup_type="EP", setup_grade="A",
        pivot_price=10.0, limit_price=10.0, stop_price=9.0, target_price=12.0,
        shares=1, status=intent_log.STATUS_PLACED, broker_order_id=999_999))
    run_dir = tmp / "ledgers" / "_auto_paper_runs" / "2026-06-20T13-35-00"
    _write_run_artifacts(tmp / "ledgers" / "_auto_paper_runs", "2026-06-20T13-35-00",
                         [{"ticker": "ZZZ", "status": "placed", "placed": True,
                           "broker_order_id": 1}], "2026-06-20T13:35:00+00:00")
    cap: list[str] = []

    def _send(m):
        cap.append(m)
        return NS(ok=True, error=None)
    alert_state = tmp / "alert_state.json"
    t0 = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)

    def _snap_now():
        snap = health_snapshot.build_snapshot(
            run_dir=run_dir, runs_dir=tmp / "ledgers" / "_auto_paper_runs",
            positions_json=positions_json, check_feeds=False, write=False)
        return snap
    snap = _snap_now()
    health_alerts.dispatch_alerts(snap, state_path=alert_state, send=_send, now_utc=t0)
    health_alerts.dispatch_alerts(snap, state_path=alert_state, send=_send,
                                  now_utc=t0 + timedelta(minutes=30))
    health_alerts.dispatch_alerts(
        snap, state_path=alert_state, send=_send,
        now_utc=t0 + timedelta(seconds=health_alerts.ORPHAN_REPAGE_SECONDS + 60))
    check(len(cap) == 2,
          f"persistent orphan: pages, suppresses within backoff, RE-PAGES after "
          f"(pages={len(cap)}, expected 2)")

    # final invariants across the adversarial env. Intentionally-dangling:
    #   PAGE — left for the alarm test; SAME — held unresolved by the same-day
    #   min-age guard (correct: never abandon a fresh intent on one day).
    # AAA recovered, REJ abandoned, HELD recovered.
    intended_dangling = {"PAGE", "SAME"}
    unresolved = intent_log.unresolved_intents()
    stray = [r.client_order_id for r in unresolved if r.ticker not in intended_dangling]
    check(not stray, f"no UNINTENDED dangling intents after adversarial run (stray={stray})")

    print("-" * 72)
    if fails:
        print(f"RESULT: FAIL — {len(fails)} adversarial assertion(s) failed")
        return 1
    print("RESULT: PASS — lying-broker safe, no double-place, partial/reject/old-fill "
          "handled, persistent orphan re-pages")
    return 0


if __name__ == "__main__":
    rc_main = run()
    rc_adv = run_adversarial()
    sys.exit(rc_main or rc_adv)
