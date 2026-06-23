"""Entry-trigger watcher orchestrator + CLI.

Loads structured `trigger` blocks from journal/watchlist.json, evaluates each
against live OHLCV + the SPY regime, and pushes an edge-triggered Telegram alert
when a name escalates toward a buyable entry. Advisory-only — never trades.

Cadence: hourly during US market hours + a near-close run (Windows Task
Scheduler — see scripts/install-watcher-task.ps1). Self-gates: an entry with no
structured `trigger` block is skipped (legacy free-text entries are ignored).

CLI::

    uv run python -m tools.watcher.run            # live (push transitions)
    uv run python -m tools.watcher.run --dry-run  # evaluate + print, no push
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .evaluate import (
    RANK,
    STATUS_APPROACHING,
    STATUS_BLOCKED,
    STATUS_FIRED,
    STATUS_IN_ZONE,
    WatchSignal,
    evaluate_entry,
)
from .state import STATE_DEFAULT, decide_pushes, load_state, save_state

_REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_DEFAULT = _REPO_ROOT / "journal" / "watchlist.json"

_EMOJI = {
    STATUS_FIRED: "🟢", STATUS_IN_ZONE: "🟡",
    STATUS_APPROACHING: "🔵", STATUS_BLOCKED: "⏸️",
}


def _et_today_key() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()


def load_triggers(path: Path = WATCHLIST_DEFAULT) -> list[dict[str, Any]]:
    """Return watchlist entries that carry a structured `trigger` block."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [
        e for e in doc.get("watchlist", [])
        if isinstance(e.get("trigger"), dict)
        or (isinstance(e.get("trigger"), list) and e["trigger"])
    ]


def _fetch(ticker: str):
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(period="1y", interval="1d").dropna()
        return df if len(df) >= 25 else None
    except Exception:  # noqa: BLE001
        return None


def _regime_ok() -> tuple[bool, str]:
    """Return (regime_ok, stage_class). Fail-safe: on error, treat as blocked."""
    try:
        from ..regime_check import classify_broad
        from ..trend_template import compute_from_ticker as tt_from_ticker

        passes = tt_from_ticker("SPY", include_rs=False).output["trend_template_passes"]
        stage_class, _mult = classify_broad(passes)
        return stage_class in ("stage_2_confirmed", "stage_2_weakening"), stage_class
    except Exception as exc:  # noqa: BLE001
        return False, f"regime_check_failed:{exc!r}"


def format_signal(s: WatchSignal, stage_class: str) -> str:
    emoji = _EMOJI.get(s.status, "•")
    head = {
        STATUS_FIRED: "ENTRY TRIGGER",
        STATUS_IN_ZONE: "IN ZONE",
        STATUS_APPROACHING: "APPROACHING",
        STATUS_BLOCKED: "BLOCKED (regime)",
    }.get(s.status, s.status.upper())
    p = s.payload
    lines = [f"{emoji} *{s.ticker}* — {head}", f"${s.price:.2f} — {s.reason}"]
    if p.get("entry") is not None:
        plan = f"Entry ${p['entry']} · Stop ${p.get('stop')} · Target ${p.get('target')}"
        if p.get("rr"):
            plan += f" · R:R {p['rr']}"
        lines.append(plan)
    if p.get("note"):
        lines.append(p["note"])
    lines.append(f"SPY regime: {stage_class}. Advisory only — confirm before entry.")
    return "\n".join(lines)


def run(*, dry_run: bool = False, watchlist: Path = WATCHLIST_DEFAULT,
        state_path: Path = STATE_DEFAULT, send=None) -> dict[str, Any]:
    triggers = load_triggers(watchlist)
    regime_ok, stage_class = _regime_ok()
    signals: list[WatchSignal] = []
    skipped: list[str] = []
    for e in triggers:
        tkr = str(e.get("ticker", "")).upper()
        df = _fetch(tkr)
        if df is None:
            skipped.append(tkr)
            continue
        trig = e["trigger"]
        trig_list = trig if isinstance(trig, list) else [trig]
        per = [evaluate_entry(tkr, t, df, regime_ok=regime_ok) for t in trig_list]
        # One signal per ticker: surface the nearest-to-firing trigger.
        signals.append(max(per, key=lambda s: RANK.get(s.status, 0)))

    state = load_state(state_path)
    to_push, new_state = decide_pushes(signals, state, today_key=_et_today_key())

    pushed: list[str] = []
    errors: list[str] = []
    if not dry_run and to_push:
        if send is None:
            from ..thematic_portfolio.kill_switch.telegram_alert import send_alert as send
        for s in to_push:
            try:
                res = send(format_signal(s, stage_class))
                if getattr(res, "ok", False):
                    pushed.append(s.ticker)
                else:
                    errors.append(f"{s.ticker}: {getattr(res, 'error', 'send_failed')}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{s.ticker}: {exc!r}")
        save_state(new_state, state_path)
    elif not dry_run:
        save_state(new_state, state_path)

    return {
        "stage_class": stage_class, "regime_ok": regime_ok,
        "evaluated": [s.to_dict() for s in signals],
        "to_push": [s.ticker for s in to_push], "pushed": pushed,
        "errors": errors, "skipped": skipped, "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="tools.watcher.run",
                                 description="Entry-trigger watcher — advisory Telegram alerts.")
    ap.add_argument("--dry-run", action="store_true", help="evaluate + print, do not push or persist")
    ap.add_argument("--watchlist", default=str(WATCHLIST_DEFAULT))
    args = ap.parse_args()

    out = run(dry_run=args.dry_run, watchlist=Path(args.watchlist))
    print(f"=== entry watcher === SPY {out['stage_class']} (regime_ok={out['regime_ok']})")
    if not out["evaluated"]:
        print("  no watchlist entries with a structured trigger block — nothing to watch.")
    # ASCII-only console markers (the emoji in _EMOJI are for Telegram, which is
    # UTF-8; the Windows console is cp1252 and would raise on them).
    _MARK = {STATUS_FIRED: ">>", STATUS_IN_ZONE: " *",
             STATUS_APPROACHING: " ~", STATUS_BLOCKED: " x"}
    for s in out["evaluated"]:
        mk = _MARK.get(s["status"], "  ")
        print(f"  {mk} {s['ticker']:6} {s['status']:11} ${s['price']:>8.2f}  {s['reason']}")
    if out["to_push"]:
        print(f"  -> {'WOULD PUSH' if out['dry_run'] else 'PUSHED'}: {', '.join(out['to_push'])}")
    if out["skipped"]:
        print(f"  (no data: {', '.join(out['skipped'])})")
    if out["errors"]:
        print(f"  ERRORS: {out['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
