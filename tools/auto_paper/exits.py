"""Per-bar sell-decision composer auto-exit for the paper-auto track.

For each paper-auto position in the ``starter`` state:

1. Fetch recent OHLCV via :func:`tools.data.fetch_ohlcv`
2. Run the four OHLCV-derivable sell-discipline detectors:
   * :mod:`tools.climax_top_detect` (6 climax-top patterns)
   * :mod:`tools.violations_detect` (5 post-entry violations)
   * :mod:`tools.base_stage_detect`  (base count + new-high flag)
   * :mod:`tools.sell_into_strength` (10-15% in 2-3 days)
3. Compose via :func:`tools.sell_decision.compute` (P/E expansion is
   fundamentals-only — passed as False for v1)
4. If the composer returns a non-hold action (``sell_50`` /
   ``sell_75`` / ``sell_100``), auto-place a limit-sell via
   :meth:`tools.broker.tiger.TigerClient.place_limit_sell` at
   bid − 0.1% (per CLAUDE.md execution rules), then cancel the resting
   broker-side stop so we don't end up with a stale stop after exit.
5. Append the per-position evaluation to the ledger's
   ``sell_eval_history``. If a sell action was placed, transition the
   ledger state to ``closed`` and record the exit price + reason.

v1 simplifications (documented for the next session to revisit):

* Partial sells (``sell_50`` / ``sell_75``) close the WHOLE position.
  Pyramid management is a future-session enhancement.
* Bid price used for the limit-sell offset comes from the last bar's
  ``Close`` (we have no live L1 in this module — the limit lands close
  to last and the broker fills against the current book).

Session 3 scope; PE-expansion wiring added 2026-05-25.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import asdict, dataclass
from typing import Any, Optional

import pandas as pd
import yaml

from ..base_stage_detect import compute_from_ohlcv as base_stage_compute
from ..broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from ..climax_top_detect import compute_from_ohlcv as climax_compute
from ..data import fetch_ohlcv
from ..pe_expansion_check import compute_from_ticker as pe_expansion_from_ticker
from ..sell_decision import compute as sell_decision_compute
from ..sell_into_strength import compute as sis_compute
from ..violations_detect import compute_from_ohlcv as violations_compute
from . import state

# Actions that the sell-decision composer can return that should trigger an
# auto-exit at the paper broker. ``tighten_stop`` is non-trivial in v1 —
# trailing-stop management is post-MVP — so it's intentionally NOT included.
SELL_ACTIONS = frozenset({"sell_50", "sell_75", "sell_100"})

# Per CLAUDE.md execution rules: limit-sell at bid - 0.1%.
SELL_LIMIT_OFFSET_PCT = 0.001


def _open_sell_tickers(client: TigerClient) -> set[str]:
    """Return the set of symbols (upper-case) that have an OPEN SELL order at the broker.

    Used by the idempotency guard so a multi-bar SELL signal does not stack
    multiple pending limit-sells against the same ticker. Bug-class fix
    (Bug 2 of 2026-06-02 exits.py diagnostic): COIN went net short -639 sh
    because four 213-sh SELL limits fired on the same 213-sh long.

    Counts BOTH ``LMT SELL`` (the exits.py exit leg) and ``STP SELL`` (the
    protective stop is not a competing sell — the BOTH-pending case is fine;
    we count it anyway because if there's ANY active SELL, placing another
    creates an over-sell race once one of them fills).
    """
    try:
        oo = client.open_orders()
    except BrokerOrderError:
        # If we can't read open orders, refuse to place any new sell — safer
        # to skip an exit cycle than to risk a duplicate-sell.
        return set()
    return {
        (o.get("symbol") or "").upper()
        for o in oo.output.get("orders", [])
        if (o.get("action") or "").upper() == "SELL"
        and (o.get("symbol") or "")
    }

# Default OHLCV window. base_stage_detect requires PRIOR_HIGH_LOOKBACK +
# SWING_WINDOW = 262 bars; 1y of daily bars (~252) is the minimum sensible.
# Use 14 months to be safe across the requirement.
DEFAULT_OHLCV_PERIOD = "14mo"


@dataclass
class ExitResult:
    """Per-position evaluation outcome from :func:`evaluate_exits`."""
    ticker: str
    action: str               # "hold" / "sell_50" / "sell_75" / "sell_100" / "skipped" / "error"
    placed: bool = False      # True when a paper limit-sell was placed
    sell_order_id: Optional[int] = None
    sell_limit_price: Optional[float] = None
    cancelled_stop_order_id: Optional[int] = None
    sell_shares: Optional[int] = None
    climax_patterns_firing: Optional[int] = None
    violations_firing: Optional[int] = None
    base_stage: Optional[int] = None
    sell_into_strength_triggered: Optional[bool] = None
    pe_doubled_late_stage: Optional[bool] = None
    confidence: Optional[str] = None
    contributing_triggers: Optional[list[str]] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _starter_positions() -> list[dict[str, Any]]:
    """Return paper-auto positions currently in the ``starter`` state."""
    data = state.load_positions_json()
    return [
        p for p in data.get("positions", [])
        if p.get("stage") == "starter"
    ]


def _read_ledger(ticker: str) -> dict[str, Any] | None:
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _setup_grade_from_ledger(doc: dict[str, Any]) -> str:
    """Pull setup grade from the ledger; default to ``C`` if absent.

    ``sell_decision.compute`` requires a known grade key. paper-auto
    ledgers usually have one (EP / SEPA-VCP both grade) but for safety we
    default conservatively (low grade ⇒ lower thresholds ⇒ more
    aggressive exits, which is the safer default for an autonomous
    track).
    """
    sc = doc.get("setup_classification") or {}
    grade = sc.get("grade")
    return grade if isinstance(grade, str) and grade else "C"


def _starter_fill_info(doc: dict[str, Any]) -> tuple[float | None, _dt.date | None]:
    """Return (starter fill_price, fill_date) from the position ledger."""
    ps = doc.get("position_state") or {}
    starter = ps.get("starter") or {}
    price = starter.get("fill_price")
    fill_date_raw = starter.get("fill_date")
    if isinstance(fill_date_raw, _dt.date):
        fill_date = fill_date_raw
    elif isinstance(fill_date_raw, str):
        try:
            fill_date = _dt.date.fromisoformat(fill_date_raw)
        except ValueError:
            fill_date = None
    else:
        fill_date = None
    return (float(price) if isinstance(price, (int, float)) else None), fill_date


def _append_sell_eval(
    doc: dict[str, Any],
    *,
    climax_count: int,
    violations_count: int,
    base_stage: int,
    sis_triggered: bool,
    action: str,
    confidence: str,
    new_stop: float | None,
    pe_warning: bool = False,
) -> None:
    """Append a sell_eval_history entry to the in-memory ledger doc."""
    eval_entry: dict[str, Any] = {
        "date": _today_iso(),
        "evaluated_at": _now_iso(),
        "climax_top_patterns_firing": int(climax_count),
        "violations_firing": int(violations_count),
        "base_stage": int(base_stage),
        "sell_into_strength_triggered": bool(sis_triggered),
        "pe_doubled_late_stage": bool(pe_warning),
        "action": action,
        "confidence": confidence,
        "v1_preliminary_flag": True,
    }
    if new_stop is not None:
        eval_entry["new_stop"] = float(new_stop)
    doc.setdefault("sell_eval_history", []).append(eval_entry)


def _mark_ledger_pending_close(
    doc: dict[str, Any],
    *,
    pending_sell_order_id: int | None,
    sell_limit_price: float,
    exit_reason: str,
) -> None:
    """Mutate the in-memory ledger doc to a pending_close state.

    Bug-class fix (Bug 1 of 2026-06-02 diagnostic): the prior ``_close_ledger``
    was called immediately after ``place_limit_sell`` returned an order_id,
    NOT after confirming the order filled. Tiger paper limit-sells are
    DAY-TIF. A late-afternoon SELL signal that landed at, say, 15:00 ET
    with a limit BELOW the current bid would expire unfilled at close —
    but the ledger had already been marked ``closed``, removed from
    positions.json, and the protective stop had been cancelled. The
    position stayed live at Tiger with NO monitoring and NO stop.

    Fix: transition to ``pending_close`` instead. The protective stop is
    NOT cancelled (still defends if the limit-sell expires). The position
    stays in positions.json (still visible to the operator + downstream
    audits). Reconciler completes the lifecycle: on confirmed fill ->
    closed; on expiry -> reverts to starter (stop is still in place).
    """
    doc.setdefault("meta", {})
    doc["meta"]["state"] = "pending_close"
    doc["meta"]["updated_by"] = "auto_paper/exits"
    doc["meta"]["updated_at"] = _now_iso()

    ps = doc.setdefault("position_state", {})
    if pending_sell_order_id is not None:
        ps["pending_sell_order_id"] = int(pending_sell_order_id)

    existing = doc.get("notes", "")
    new_note = (
        f"Pending close by auto_paper/exits on {_today_iso()} — "
        f"limit-sell @ ${sell_limit_price:.4f}, reason: {exit_reason}. "
        f"Awaiting reconcile fill confirmation."
    )
    doc["notes"] = f"{existing}\n{new_note}".strip() if existing else new_note


def _mark_positions_json_pending_close(ticker: str, *, pending_sell_order_id: int | None) -> None:
    """Update the positions.json entry's stage to pending_close.

    Keeps the entry in the index (the position is NOT yet closed at the
    broker) but flags the stage so monitors / dashboards can distinguish
    starter (open, no exit pending) from pending_close (exit limit-sell
    submitted, awaiting fill).
    """
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    import json as _json
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = _json.load(fh)
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["stage"] = "pending_close"
            if pending_sell_order_id is not None:
                entry["pending_sell_order_id"] = int(pending_sell_order_id)
            break
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        _json.dump(data, fh, indent=2)


def _close_ledger(
    doc: dict[str, Any],
    *,
    exit_price: float,
    exit_reason: str,
) -> None:
    """DEPRECATED: kept for backwards-compat with tests that monkeypatch it.

    The fill-confirmed close path now lives in
    ``tools.auto_paper.reconcile`` (see ``_close_ledger_from_pending``).
    The exits.py main flow no longer calls this — it transitions to
    pending_close and leaves the closed-state transition to reconcile.
    """
    doc.setdefault("meta", {})
    doc["meta"]["state"] = "closed"
    doc["meta"]["updated_by"] = "auto_paper/exits"
    doc["meta"]["updated_at"] = _now_iso()

    existing = doc.get("notes", "")
    new_note = (
        f"Closed by auto_paper/exits on {_today_iso()} at ${exit_price:.4f} — "
        f"reason: {exit_reason}"
    )
    doc["notes"] = f"{existing}\n{new_note}".strip() if existing else new_note


def _write_ledger(ticker: str, doc: dict[str, Any]) -> None:
    p = state.ledger_path(ticker)
    state._validate_against_schema(doc)  # noqa: SLF001 — internal helper, intentional
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _mark_positions_json_closed(ticker: str, *, exit_price: float, exit_reason: str) -> None:
    """Remove a closed ticker from the paper-auto positions index.

    positions.json is the OPEN-positions index — closed positions live
    in the ledger file (state: closed). Previously we stage-marked the
    entry as "closed" but left it in the array; that double-counted
    position-count and sector-concentration against `MAX_POSITIONS` /
    `MAX_PCT_PER_SECTOR` in `pipeline._check_track_limits`, blocking new
    entries silently. Closed-history reconstruction lives in
    `tools.auto_paper.performance` which reads ledgers directly.
    """
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    import json
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    before = len(data.get("positions", []))
    data["positions"] = [
        p for p in data.get("positions", [])
        if p.get("ticker") != ticker.upper()
    ]
    removed = before - len(data["positions"])
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    # The exit_price / exit_reason / exit_date are already persisted on
    # the ledger file by _close_ledger; keeping them argumented here so
    # callers don't need to change. The variables are intentionally
    # unused at this layer.
    del exit_price, exit_reason, removed


def _evaluate_one(
    *,
    pos: dict[str, Any],
    fetch_ohlcv_fn,
    pe_expansion_fn=None,
) -> tuple[str, dict[str, Any]]:
    """Return ``(action, evaluation_dict)`` for a single paper-auto position.

    Pure compute — no broker calls, no file writes. The caller decides
    whether to act on the action.

    Args:
        pe_expansion_fn: test seam — callable taking
            ``(ticker=, entry_price=, current_price=)`` returning a
            :class:`TraceEntry`. Default: :func:`pe_expansion_from_ticker`
            (live EDGAR). Pass ``lambda **kw: SimpleNamespace(output={})``
            in unit tests to skip the EDGAR roundtrip.
    """
    ticker = pos["ticker"]
    doc = _read_ledger(ticker)
    if doc is None:
        return "error", {"reason": f"ledger missing for {ticker}"}

    # Bug-class fix (Bug 3 of 2026-06-02 diagnostic): if an operator has
    # restored a stop or wants the monitor to keep its hands off a
    # specific position, position_state.operator_locked=true blocks the
    # transactional path. Detector composition still runs (so the
    # sell_eval_history reflects what the monitor SAW) but no broker
    # calls are made and no state transition happens. The operator
    # clears the flag to re-enable automation.
    ps = doc.get("position_state") or {}
    if bool(ps.get("operator_locked", False)):
        return "operator_locked", {"doc": doc, "reason": "operator_locked=true on ledger"}

    setup_grade = _setup_grade_from_ledger(doc)
    starter_price, fill_date = _starter_fill_info(doc)
    if starter_price is None or fill_date is None:
        return "error", {"reason": f"ledger missing starter fill info for {ticker}"}

    try:
        ohlcv = fetch_ohlcv_fn(ticker, period=DEFAULT_OHLCV_PERIOD, interval="1d")
    except Exception as exc:
        return "error", {"reason": f"fetch_ohlcv failed: {exc}"}

    df = ohlcv.df
    if df is None or df.empty:
        return "error", {"reason": "fetch_ohlcv returned empty"}

    # --- Detector 1: climax-top patterns -------------------------------
    climax_count = 0
    try:
        r = climax_compute(df)
        climax_count = int(r.output["patterns_firing"])
    except (ValueError, KeyError):
        climax_count = 0

    # --- Detector 2: violations ----------------------------------------
    violations_count = 0
    violation_5_alone = False
    try:
        r = violations_compute(df, entry_date=fill_date)
        violations_count = int(r.output["violations_firing"])
        violation_5_alone = bool(r.output["violation_5_alone_full_exit"])
    except (ValueError, KeyError):
        pass  # insufficient bars or entry_date out of range — treat as 0

    # --- Detector 3: base stage ----------------------------------------
    base_stage_val = 1
    new_high_today = False
    try:
        r = base_stage_compute(df)
        base_stage_val = int(r.output["base_stage"])
        new_high_today = bool(r.output["new_high_today"])
    except (ValueError, KeyError):
        pass  # insufficient bars

    # --- Detector 4: sell-into-strength --------------------------------
    sis_triggered = False
    sis_fraction = 0.0
    try:
        idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
        bars_since_fill = sum(1 for d in idx_dates if d >= fill_date)
        bars_since_fill = max(1, bars_since_fill)
        # Gain over the LAST 3 bars (per sell_into_strength 2-3 day window).
        if len(df) >= 4:
            recent_close = float(df["Close"].iloc[-1])
            base_close = float(df["Close"].iloc[-4])
            gain_pct = (recent_close / base_close) - 1.0 if base_close > 0 else 0.0
        else:
            gain_pct = 0.0
        r = sis_compute(
            gain_pct=gain_pct,
            days_in_move=min(3, bars_since_fill),
            setup_grade=setup_grade,
        )
        sis_triggered = bool(r.output["threshold_met"])
        sis_fraction = float(r.output["recommended_fraction"])
    except (ValueError, KeyError):
        pass

    # --- Detector 5: P/E expansion (EDGAR-backed) ----------------------
    # Non-fatal: any failure (unknown ticker, ADR, negative EPS, network)
    # falls back to False. Position evaluation continues.
    pe_fn = pe_expansion_fn if pe_expansion_fn is not None else pe_expansion_from_ticker
    pe_warning = False
    try:
        last_close = float(df["Close"].iloc[-1])
        pe_entry = pe_fn(
            ticker=ticker,
            entry_price=starter_price,
            current_price=last_close,
        )
        pe_warning = bool(pe_entry.output.get("pe_expanded", False))
    except Exception:  # noqa: BLE001 — adapter throws broadly; failure is non-fatal
        pe_warning = False

    # --- Compose -------------------------------------------------------
    try:
        decision = sell_decision_compute(
            climax_patterns_firing=climax_count,
            violations_firing=violations_count,
            violation_5_alone_full_exit=violation_5_alone,
            base_stage=base_stage_val,
            new_high_today=new_high_today,
            sell_into_strength_triggered=sis_triggered,
            sell_into_strength_fraction=sis_fraction,
            setup_grade=setup_grade,
            pe_expansion_warning=pe_warning,
        )
    except ValueError as exc:
        return "error", {"reason": f"sell_decision failed: {exc}"}

    action = decision.output["action"]
    return action, {
        "doc": doc,
        "df": df,
        "decision": decision.output,
        "climax_count": climax_count,
        "violations_count": violations_count,
        "base_stage": base_stage_val,
        "sis_triggered": sis_triggered,
        "pe_warning": pe_warning,
        "setup_grade": setup_grade,
    }


def evaluate_exits(
    *,
    client: TigerClient | None = None,
    dry_run: bool = False,
    fetch_ohlcv_fn=fetch_ohlcv,
    pe_expansion_fn=None,
) -> list[ExitResult]:
    """Evaluate per-bar sell-decision for every starter-state paper-auto position.

    Args:
        client: an existing :class:`TigerClient`. When None, constructs a
            paper-routed client. Refuses to construct if there are no
            starter positions (returns ``[]`` first).
        dry_run: when True, runs the detector composition and writes the
            ``sell_eval_history`` entry but does NOT place a sell, does
            NOT cancel the stop, and does NOT close the ledger.
        fetch_ohlcv_fn: injectable for tests. Default
            :func:`tools.data.fetch_ohlcv`.

    Returns:
        list[ExitResult] — one per starter-state paper-auto position.
        Empty list if nothing to evaluate.
    """
    starters = _starter_positions()
    if not starters:
        return []

    # Only construct the client if we have work to do — same pattern as
    # reconcile_today. Surface BrokerConfigError uniformly as per-position
    # errors so the caller sees the same shape.
    c: TigerClient | None = client
    if not dry_run and c is None:
        try:
            c = TigerClient()
        except BrokerConfigError as exc:
            return [
                ExitResult(
                    ticker=p["ticker"], action="error",
                    reason=f"broker config: {exc}",
                ) for p in starters
            ]

    # Bug-class fix (Bug 2 of 2026-06-02 diagnostic): pre-fetch the set of
    # tickers with OPEN SELL orders ONCE per evaluate_exits call. We use it
    # to skip placing duplicate sells when a multi-bar SELL signal fires
    # across consecutive monitor invocations. Cheap upfront fetch beats one
    # broker round-trip per ticker in the loop.
    open_sell_tickers: set[str] = set()
    if not dry_run and c is not None:
        open_sell_tickers = _open_sell_tickers(c)

    results: list[ExitResult] = []
    for pos in starters:
        ticker = pos["ticker"]
        action, ctx = _evaluate_one(
            pos=pos,
            fetch_ohlcv_fn=fetch_ohlcv_fn,
            pe_expansion_fn=pe_expansion_fn,
        )

        if action == "error":
            results.append(ExitResult(
                ticker=ticker, action="error",
                reason=ctx.get("reason"),
            ))
            continue

        if action == "operator_locked":
            # Detector composition was skipped (we returned early). Record
            # the lock + skip silently — the operator owns this position
            # until they clear the flag.
            results.append(ExitResult(
                ticker=ticker, action="operator_locked",
                placed=False,
                reason=ctx.get("reason"),
            ))
            continue

        doc: dict[str, Any] = ctx["doc"]
        df: pd.DataFrame = ctx["df"]
        decision_out: dict[str, Any] = ctx["decision"]
        climax_count = ctx["climax_count"]
        violations_count = ctx["violations_count"]
        base_stage_val = ctx["base_stage"]
        sis_triggered = ctx["sis_triggered"]
        pe_warning = ctx.get("pe_warning", False)
        confidence = decision_out.get("confidence", "MEDIUM")
        contributing = list(decision_out.get("contributing_triggers", []))

        if action not in SELL_ACTIONS:
            # Hold / tighten_stop / sell_1_3 — record but don't transact.
            # (sell_1_3 is intentionally not in SELL_ACTIONS for v1; we
            # treat it as a noisy hold to avoid 33% partial-fill plumbing.)
            _append_sell_eval(
                doc,
                climax_count=climax_count,
                violations_count=violations_count,
                base_stage=base_stage_val,
                sis_triggered=sis_triggered,
                action=action,
                confidence=confidence,
                new_stop=None,
                pe_warning=pe_warning,
            )
            if not dry_run:
                try:
                    _write_ledger(ticker, doc)
                except state.PaperAutoStateError as exc:
                    results.append(ExitResult(
                        ticker=ticker, action="error",
                        reason=f"ledger write (sell_eval append): {exc}",
                    ))
                    continue
            results.append(ExitResult(
                ticker=ticker, action=action,
                placed=False,
                climax_patterns_firing=climax_count,
                violations_firing=violations_count,
                base_stage=base_stage_val,
                sell_into_strength_triggered=sis_triggered,
                pe_doubled_late_stage=pe_warning,
                confidence=confidence,
                contributing_triggers=contributing,
                reason=("dry_run — sell_eval recorded, no transaction"
                        if dry_run else None),
            ))
            continue

        # --- Sell action ------------------------------------------------
        shares = int(pos.get("shares") or 0)
        if shares <= 0:
            results.append(ExitResult(
                ticker=ticker, action="error",
                reason=f"position has non-positive shares={shares}; refusing to sell",
            ))
            continue

        # Use the last bar's Close as a bid proxy (no live L1 here).
        last_close = float(df["Close"].iloc[-1])
        sell_limit = round(last_close * (1.0 - SELL_LIMIT_OFFSET_PCT), 2)

        # Bug-class fix (Bug 2 idempotency guard): if a prior monitor tick
        # already placed a SELL on this symbol (LMT or STP) and it is still
        # open at the broker, do NOT stack another sell. Just record the
        # composer eval + skip. The original 2026-05-28 COIN incident saw
        # four 213-sh SELL fills against a 213-sh long because each
        # 30-min monitor tick fired a fresh LMT.
        if ticker.upper() in open_sell_tickers:
            _append_sell_eval(
                doc,
                climax_count=climax_count,
                violations_count=violations_count,
                base_stage=base_stage_val,
                sis_triggered=sis_triggered,
                action=action,
                confidence=confidence,
                new_stop=None,
                pe_warning=pe_warning,
            )
            if not dry_run:
                try:
                    _write_ledger(ticker, doc)
                except state.PaperAutoStateError as exc:
                    results.append(ExitResult(
                        ticker=ticker, action="error",
                        reason=f"ledger write (sell_eval append, skip-duplicate): {exc}",
                    ))
                    continue
            results.append(ExitResult(
                ticker=ticker, action="sell_pending_duplicate",
                placed=False,
                sell_shares=shares,
                climax_patterns_firing=climax_count,
                violations_firing=violations_count,
                base_stage=base_stage_val,
                sell_into_strength_triggered=sis_triggered,
                pe_doubled_late_stage=pe_warning,
                confidence=confidence,
                contributing_triggers=contributing,
                reason=(
                    f"open SELL order already pending for {ticker} at broker; "
                    f"skipped duplicate placement (composer wanted {action})"
                ),
            ))
            continue

        if dry_run:
            _append_sell_eval(
                doc,
                climax_count=climax_count,
                violations_count=violations_count,
                base_stage=base_stage_val,
                sis_triggered=sis_triggered,
                action=action,
                confidence=confidence,
                new_stop=None,
                pe_warning=pe_warning,
            )
            results.append(ExitResult(
                ticker=ticker, action=action,
                placed=False,
                sell_limit_price=sell_limit,
                sell_shares=shares,
                cancelled_stop_order_id=None,
                climax_patterns_firing=climax_count,
                violations_firing=violations_count,
                base_stage=base_stage_val,
                sell_into_strength_triggered=sis_triggered,
                pe_doubled_late_stage=pe_warning,
                confidence=confidence,
                contributing_triggers=contributing,
                reason=(
                    f"dry_run — would place limit-sell {shares} {ticker} @ ${sell_limit:.2f}"
                    " (stop is LEFT IN PLACE until reconcile confirms fill)"
                ),
            ))
            continue

        # Place the limit-sell. The protective stop is NOT cancelled here —
        # the reconciler will cancel it only after confirming the limit-sell
        # filled. If the limit-sell expires DAY-unfilled, the stop is still
        # active and the position transitions back to ``starter`` on the
        # next reconcile pass.
        try:
            sell_entry = c.place_limit_sell(
                symbol=ticker.upper(),
                quantity=shares,
                limit_price=sell_limit,
            )
        except BrokerOrderError as exc:
            results.append(ExitResult(
                ticker=ticker, action="error",
                reason=f"place_limit_sell: {exc}",
            ))
            continue

        sell_order_id_raw = sell_entry.output.get("order_id")
        sell_order_id = int(sell_order_id_raw) if sell_order_id_raw is not None else None

        # Mark the local cache so a back-to-back ticker in this same loop
        # doesn't re-stack (defensive — same ticker shouldn't appear twice
        # in starters, but cheap to set).
        open_sell_tickers.add(ticker.upper())

        # Record evaluation + transition to pending_close (NOT closed).
        # Reconciler completes the lifecycle on fill confirmation.
        contributing_summary = ", ".join(contributing[:3]) if contributing else action
        exit_reason = f"sell_decision/{action} ({contributing_summary})"
        _append_sell_eval(
            doc,
            climax_count=climax_count,
            violations_count=violations_count,
            base_stage=base_stage_val,
            sis_triggered=sis_triggered,
            action=action,
            confidence=confidence,
            new_stop=None,
            pe_warning=pe_warning,
        )
        _mark_ledger_pending_close(
            doc,
            pending_sell_order_id=sell_order_id,
            sell_limit_price=sell_limit,
            exit_reason=exit_reason,
        )
        try:
            _write_ledger(ticker, doc)
        except state.PaperAutoStateError as exc:
            results.append(ExitResult(
                ticker=ticker, action="error",
                placed=True,
                sell_order_id=sell_order_id,
                sell_limit_price=sell_limit,
                sell_shares=shares,
                cancelled_stop_order_id=None,
                reason=(
                    f"order placed (id={sell_order_id}) but ledger pending_close-write failed: {exc}. "
                    "Manual reconciliation needed."
                ),
            ))
            continue

        _mark_positions_json_pending_close(
            ticker, pending_sell_order_id=sell_order_id,
        )

        results.append(ExitResult(
            ticker=ticker, action=action,
            placed=True,
            sell_order_id=sell_order_id,
            sell_limit_price=sell_limit,
            sell_shares=shares,
            cancelled_stop_order_id=None,  # stop NOT cancelled until reconciler confirms fill
            climax_patterns_firing=climax_count,
            violations_firing=violations_count,
            base_stage=base_stage_val,
            sell_into_strength_triggered=sis_triggered,
            pe_doubled_late_stage=pe_warning,
            confidence=confidence,
            contributing_triggers=contributing,
            reason="transitioned to pending_close; awaiting reconcile fill confirmation",
        ))

    return results
