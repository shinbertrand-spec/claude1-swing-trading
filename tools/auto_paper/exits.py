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


def _close_ledger(
    doc: dict[str, Any],
    *,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Mutate the in-memory ledger doc to a closed state, recording the exit."""
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
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    import json
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["stage"] = "closed"
            entry["exit_price"] = float(exit_price)
            entry["exit_reason"] = exit_reason
            entry["exit_date"] = _today_iso()
            break
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


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

        # Existing stop_order_id, if any, for cancellation post-sell.
        stop_order_id = (doc.get("position_state") or {}).get("stop_order_id")
        stop_order_id = int(stop_order_id) if isinstance(stop_order_id, (int, float)) else None

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
                    + (f"; would cancel stop #{stop_order_id}" if stop_order_id else "")
                ),
            ))
            continue

        # Place the limit-sell.
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

        sell_order_id = sell_entry.output.get("order_id")

        # Cancel the resting broker-side stop so it doesn't fire later.
        cancelled_id: int | None = None
        cancel_err: str | None = None
        if stop_order_id is not None:
            try:
                cancel_entry = c.cancel(order_id=stop_order_id)
                if cancel_entry.output.get("accepted"):
                    cancelled_id = stop_order_id
                else:
                    cancel_err = f"broker did not accept cancel of stop #{stop_order_id}"
            except BrokerOrderError as exc:
                cancel_err = f"cancel(stop #{stop_order_id}): {exc}"

        # Clear stop_order_id on the ledger regardless of whether the
        # cancel succeeded — the stop_order_id no longer represents an
        # active protective stop (the position is closing). If the cancel
        # failed, the stop is surfaced via the result's cancel_err.
        ps = doc.setdefault("position_state", {})
        if "stop_order_id" in ps:
            del ps["stop_order_id"]

        # Record evaluation + close the ledger.
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
        _close_ledger(doc, exit_price=sell_limit, exit_reason=exit_reason)
        try:
            _write_ledger(ticker, doc)
        except state.PaperAutoStateError as exc:
            results.append(ExitResult(
                ticker=ticker, action="error",
                placed=True,
                sell_order_id=int(sell_order_id) if sell_order_id is not None else None,
                sell_limit_price=sell_limit,
                sell_shares=shares,
                cancelled_stop_order_id=cancelled_id,
                reason=(
                    f"order placed but ledger close-write failed: {exc}. "
                    "Manual reconciliation needed."
                ),
            ))
            continue

        _mark_positions_json_closed(
            ticker, exit_price=sell_limit, exit_reason=exit_reason,
        )

        results.append(ExitResult(
            ticker=ticker, action=action,
            placed=True,
            sell_order_id=int(sell_order_id) if sell_order_id is not None else None,
            sell_limit_price=sell_limit,
            sell_shares=shares,
            cancelled_stop_order_id=cancelled_id,
            climax_patterns_firing=climax_count,
            violations_firing=violations_count,
            base_stage=base_stage_val,
            sell_into_strength_triggered=sis_triggered,
            pe_doubled_late_stage=pe_warning,
            confidence=confidence,
            contributing_triggers=contributing,
            reason=cancel_err,   # non-fatal cancel warning, if any
        ))

    return results
