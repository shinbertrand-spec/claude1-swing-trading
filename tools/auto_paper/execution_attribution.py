"""Signal-vs-execution attribution (cherry-pick Batch B, B2).

Quantifies the documented execution gap: what the signal saw vs what the
broker actually filled, per trade and cumulative. For every entry leg with a
real fill, the total entry slippage vs the signal price decomposes as::

    total  = (fill - pivot)        / pivot            [bps]
    delay  = (fill_day_open - pivot) / pivot          [bps]  market drift
                                                      signal -> execution-day open
    resid  = (fill - fill_day_open) / pivot           [bps]  intraday: limit
                                                      placement + timing + impact
    total  = delay + resid   (identity, same denominator)

Positive bps = paid MORE than the signal price (drag). The cumulative
notional-weighted series is the sleeve's "execution drag"; the monthly
summary line carries a flag when drag exceeds ``DRAG_FLAG_BPS_PER_MONTH``.

Field sources (ledgers/paper-auto/<TICKER>.yml):

* signal price  -> ``setup_classification.pivot_price``
* decision time -> ``meta.created_at`` (+ ``signal_date`` from the
  quant_scanner reasoning-trace entry when present)
* fill          -> ``position_state.starter.fill_price`` / ``fill_date``
* placed limit  -> ``position_state.starter.limit_price_placed``

Honesty caveats, enforced in code:

* ``submitted`` ledgers seed fill_price = limit_price (state.py) — those rows
  are EXCLUDED (no real fill yet).
* ``closed_unfilled`` ledgers are excluded (nothing executed).
* Rows with |total| > ``SUSPECT_BPS`` are flagged ``suspect`` and reported
  separately, never averaged in — a corrupted ledger must surface, not
  dilute. (Motivating case: the 2026-07-24 NFLX ledger rewrite.)
* Fill dates are date-only (no clock time), so "delay cost" is measured to
  the execution day's OPEN — the finest boundary the data supports.

Series file: ``journal/paper-auto/execution_drag.jsonl`` — append-only with
(ticker, fill_date, broker_order_id) dedup keys, so re-running backfill is
idempotent.

Skip rows (fill-fidelity Task 0, 2026-08-06): an entry DAY order that expired
UNFILLED previously left no row at all — a skip could not explain itself, so
the R2 chase-cap mechanism was retroactively unverifiable. ``compute_skip_rows``
now emits one ``row_type: "skip"`` row per unfilled entry ledger, carrying the
placed limit, the attempt day's cached OHLC, a best-effort ~09:35 intraday
price (yfinance 5m, None when unavailable), and the deterministic explanation
test for a resting DAY buy limit: ``skip_explained = day_low > limit`` (the
whole session traded above the limit). Keys end in ``|skip`` so the same
append-only dedup applies. Scan-based: recomputable after the fact from the
EOD cache — only the 09:35 evidence decays (yfinance intraday ~60d window).
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

_ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = _ROOT / "ledgers" / "paper-auto"
SERIES_PATH = _ROOT / "journal" / "paper-auto" / "execution_drag.jsonl"
SERIES_PATH_ENV = "EXECUTION_DRAG_PATH"

SCHEMA_VERSION = 1

# Monthly notional-weighted drag beyond this flags to the operator. Basis:
# the cost model's mega/large-cap half-spreads are 1.5-5 bps — 25 bps/month
# of drag means systematically paying ~5-15x the expected half-spread per
# entry, i.e. the execution layer (not the spread) is leaking edge. Revisit
# against the backfill evidence in
# ledgers/improvements/2026-07-24-live-validation-batch-b.md.
DRAG_FLAG_BPS_PER_MONTH = 25.0

# Sanity bound: a legitimate marketable-limit entry cannot slip 5%+ against
# a same-week signal price. Anything past this is a data problem (corrupted
# ledger, wrong pivot), reported in its own "suspect" section.
SUSPECT_BPS = 500.0

_EXCLUDED_STATES = {"submitted", "closed_unfilled"}


@dataclass
class AttributionRow:
    ticker: str
    setup_type: Optional[str]
    state: Optional[str]
    signal_date: Optional[str]
    decision_at: Optional[str]
    fill_date: Optional[str]
    pivot_price: Optional[float]
    limit_price_placed: Optional[float]
    fill_price: Optional[float]
    shares: Optional[float]
    notional_usd: Optional[float]
    fill_day_open: Optional[float]
    total_slippage_bps: Optional[float]
    delay_cost_bps: Optional[float]
    execution_residual_bps: Optional[float]
    drag_usd: Optional[float]
    suspect: bool
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {"v": SCHEMA_VERSION, **asdict(self)}


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def _signal_date_from_trace(doc: dict[str, Any]) -> Optional[str]:
    for entry in doc.get("reasoning_trace") or []:
        sd = (entry.get("inputs") or {}).get("signal_date")
        if sd:
            return str(sd)[:10]
    return None


def _default_price_loader(ticker: str):
    from ..backtest import data_cache
    try:
        return data_cache.load(ticker)
    except Exception:
        return None


def _fill_day_open(
    ticker: str, fill_date: str, price_loader: Callable[[str], Any],
) -> Optional[float]:
    df = price_loader(ticker)
    if df is None or getattr(df, "empty", True) or "Open" not in df.columns:
        return None
    import pandas as pd
    dates = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    hits = df.loc[dates == fill_date]
    if hits.empty:
        return None
    return _safe_float(hits.iloc[0]["Open"])


def compute_row(
    doc: dict[str, Any], *, price_loader: Optional[Callable[[str], Any]] = None,
) -> Optional[AttributionRow]:
    """One ledger doc -> one attribution row, or None when nothing executed."""
    price_loader = price_loader or _default_price_loader
    meta = doc.get("meta") or {}
    state = (meta.get("state") or "").strip().lower()
    if state in _EXCLUDED_STATES:
        return None
    ticker = meta.get("ticker")
    ps = doc.get("position_state") or {}
    if ps.get("unfilled"):
        # Entry DAY order expired unfilled — the starter fill_price is the
        # SEEDED limit, not a real fill (2026-07-24 phantom-fill class).
        return None
    starter = ps.get("starter") or {}
    fill_price = _safe_float(starter.get("fill_price"))
    fill_date = starter.get("fill_date")
    if not ticker or fill_price is None or not fill_date:
        return None
    fill_date = str(fill_date)[:10]
    pivot = _safe_float((doc.get("setup_classification") or {}).get("pivot_price"))
    shares = _safe_float(starter.get("shares"))
    notional = fill_price * shares if shares else None

    total_bps = delay_bps = resid_bps = drag_usd = None
    day_open = None
    if pivot and pivot > 0:
        total_bps = (fill_price - pivot) / pivot * 1e4
        day_open = _fill_day_open(ticker, fill_date, price_loader)
        if day_open is not None:
            delay_bps = (day_open - pivot) / pivot * 1e4
            resid_bps = (fill_price - day_open) / pivot * 1e4
        if notional:
            drag_usd = total_bps / 1e4 * notional

    suspect = total_bps is not None and abs(total_bps) > SUSPECT_BPS
    return AttributionRow(
        ticker=ticker,
        setup_type=(doc.get("setup_classification") or {}).get("type"),
        state=state or None,
        signal_date=_signal_date_from_trace(doc),
        decision_at=meta.get("created_at"),
        fill_date=fill_date,
        pivot_price=pivot,
        limit_price_placed=_safe_float(starter.get("limit_price_placed")),
        fill_price=fill_price,
        shares=shares,
        notional_usd=round(notional, 2) if notional else None,
        fill_day_open=day_open,
        total_slippage_bps=round(total_bps, 2) if total_bps is not None else None,
        delay_cost_bps=round(delay_bps, 2) if delay_bps is not None else None,
        execution_residual_bps=round(resid_bps, 2) if resid_bps is not None else None,
        drag_usd=round(drag_usd, 2) if drag_usd is not None else None,
        suspect=suspect,
        key=f"{ticker}|{fill_date}|{starter.get('broker_order_id') or 'na'}",
    )


# ---------------------------------------------------------------------------
# Skip rows — unfilled entry orders must explain themselves (Task 0, 2026-08-06)
# ---------------------------------------------------------------------------

@dataclass
class SkipRow:
    ticker: str
    setup_type: Optional[str]
    state: Optional[str]
    decision_at: Optional[str]
    attempt_date: Optional[str]        # placement day (starter.fill_date seed)
    pivot_price: Optional[float]
    limit_price_placed: Optional[float]
    attempt_day_open: Optional[float]
    attempt_day_high: Optional[float]
    attempt_day_low: Optional[float]
    attempt_day_close: Optional[float]
    price_0935: Optional[float]        # best-effort 5m bar; None when unavailable
    skip_explained: Optional[bool]     # day_low > limit (None: no price data)
    explain_basis: str
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {"v": SCHEMA_VERSION, "row_type": "skip", **asdict(self)}


def _attempt_day_ohlc(
    ticker: str, day: str, price_loader: Callable[[str], Any],
) -> Optional[dict[str, Optional[float]]]:
    df = price_loader(ticker)
    if df is None or getattr(df, "empty", True):
        return None
    import pandas as pd
    dates = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    hits = df.loc[dates == day]
    if hits.empty:
        return None
    bar = hits.iloc[0]
    return {c.lower(): _safe_float(bar[c]) for c in ("Open", "High", "Low", "Close")
            if c in hits.columns}


def _intraday_0935(ticker: str, day: str) -> Optional[float]:
    """Best-effort ~09:35 ET price from yfinance 5m bars (last ~60 days only).
    Never raises — evidence when available, None otherwise."""
    try:
        import pandas as pd
        import yfinance as yf
        start = pd.Timestamp(day)
        df = yf.download(ticker, start=start, end=start + pd.Timedelta(days=1),
                         interval="5m", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        idx = df.index.tz_convert("US/Eastern") if df.index.tz is not None else df.index
        hits = df[(idx.hour == 9) & (idx.minute == 35)]
        if hits.empty:
            return None
        v = hits.iloc[0]["Open"]
        return _safe_float(v.iloc[0] if hasattr(v, "iloc") else v)
    except Exception:
        return None


def compute_skip_row(
    doc: dict[str, Any],
    *,
    price_loader: Optional[Callable[[str], Any]] = None,
    intraday_loader: Optional[Callable[[str, str], Optional[float]]] = None,
) -> Optional[SkipRow]:
    """One UNFILLED entry ledger -> one skip row, or None for filled/other."""
    price_loader = price_loader or _default_price_loader
    intraday_loader = intraday_loader or _intraday_0935
    meta = doc.get("meta") or {}
    ps = doc.get("position_state") or {}
    state = (meta.get("state") or "").strip().lower()
    unfilled = bool(ps.get("unfilled")) or state == "closed_unfilled"
    ticker = meta.get("ticker")
    if not unfilled or not ticker:
        return None
    starter = ps.get("starter") or {}
    attempt_date = starter.get("fill_date")
    attempt_date = str(attempt_date)[:10] if attempt_date else None
    limit = _safe_float(starter.get("limit_price_placed"))
    pivot = _safe_float((doc.get("setup_classification") or {}).get("pivot_price"))

    ohlc = (_attempt_day_ohlc(ticker, attempt_date, price_loader)
            if attempt_date else None)
    low = (ohlc or {}).get("low")
    if low is not None and limit is not None:
        explained = low > limit
        basis = ("day_low_above_limit" if explained
                 else "UNEXPLAINED_day_low_within_limit")
    else:
        explained = None
        basis = "no_price_data"
    p0935 = (intraday_loader(ticker, attempt_date)
             if attempt_date and limit is not None else None)
    return SkipRow(
        ticker=ticker,
        setup_type=(doc.get("setup_classification") or {}).get("type"),
        state=state or None,
        decision_at=meta.get("created_at"),
        attempt_date=attempt_date,
        pivot_price=pivot,
        limit_price_placed=limit,
        attempt_day_open=(ohlc or {}).get("open"),
        attempt_day_high=(ohlc or {}).get("high"),
        attempt_day_low=low,
        attempt_day_close=(ohlc or {}).get("close"),
        price_0935=p0935,
        skip_explained=explained,
        explain_basis=basis,
        key=f"{ticker}|{attempt_date or 'na'}|{starter.get('broker_order_id') or 'na'}|skip",
    )


def compute_skip_rows(
    ledger_dir: Optional[Path] = None,
    *,
    price_loader: Optional[Callable[[str], Any]] = None,
    intraday_loader: Optional[Callable[[str, str], Optional[float]]] = None,
) -> list[SkipRow]:
    d = Path(ledger_dir) if ledger_dir else LEDGER_DIR
    rows: list[SkipRow] = []
    for p in sorted(d.glob("*.yml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        row = compute_skip_row(doc, price_loader=price_loader,
                               intraday_loader=intraday_loader)
        if row is not None:
            rows.append(row)
    return rows


def compute_rows(
    ledger_dir: Optional[Path] = None,
    *,
    price_loader: Optional[Callable[[str], Any]] = None,
) -> list[AttributionRow]:
    d = Path(ledger_dir) if ledger_dir else LEDGER_DIR
    rows: list[AttributionRow] = []
    for p in sorted(d.glob("*.yml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        row = compute_row(doc, price_loader=price_loader)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Series persistence (append-only, dedup by key)
# ---------------------------------------------------------------------------

def _series_path(path: Optional[Path]) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(SERIES_PATH_ENV)
    if env:
        return Path(env)
    return SERIES_PATH


def append_series(
    rows: list[AttributionRow | SkipRow], *, path: Optional[Path] = None,
) -> tuple[Path, int]:
    """Append rows whose key is not already present. Returns (path, n_new)."""
    p = _series_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get("key"))
            except ValueError:
                continue
    new = [r for r in rows if r.key not in seen]
    with open(p, "a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r.to_dict()) + "\n")
    return p, len(new)


def load_series(path: Optional[Path] = None) -> list[dict[str, Any]]:
    p = _series_path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Monthly summary + report
# ---------------------------------------------------------------------------

def monthly_summary(rows: list[AttributionRow]) -> list[dict[str, Any]]:
    """Notional-weighted drag per fill month. Suspect rows excluded (they get
    their own report section — never averaged in)."""
    buckets: dict[str, list[AttributionRow]] = {}
    for r in rows:
        if r.suspect or r.total_slippage_bps is None or not r.fill_date:
            continue
        buckets.setdefault(r.fill_date[:7], []).append(r)
    out = []
    for month in sorted(buckets):
        rs = buckets[month]
        notional = sum(r.notional_usd or 0.0 for r in rs)
        def _w(attr: str) -> Optional[float]:
            pairs = [
                (getattr(r, attr), r.notional_usd)
                for r in rs
                if getattr(r, attr) is not None and r.notional_usd
            ]
            if not pairs:
                return None
            return round(sum(v * w for v, w in pairs) / sum(w for _, w in pairs), 2)
        w_total = _w("total_slippage_bps")
        out.append({
            "month": month,
            "n_trades": len(rs),
            "notional_usd": round(notional, 2),
            "weighted_total_bps": w_total,
            "weighted_delay_bps": _w("delay_cost_bps"),
            "weighted_execution_bps": _w("execution_residual_bps"),
            "drag_usd": round(sum(r.drag_usd or 0.0 for r in rs), 2),
            "flag": bool(w_total is not None and w_total > DRAG_FLAG_BPS_PER_MONTH),
        })
    return out


def render_markdown(rows: list[AttributionRow]) -> str:
    real = [r for r in rows if not r.suspect]
    suspects = [r for r in rows if r.suspect]
    lines = [
        "## Execution drag — signal-vs-fill attribution (B2)",
        "",
        f"- rows: {len(real)} attributed, {len(suspects)} suspect (excluded from averages)",
        f"- flag threshold: > {DRAG_FLAG_BPS_PER_MONTH:.0f} bps/month notional-weighted",
        "",
        "| ticker | setup | fill date | pivot | fill | total bps | delay bps | exec bps | drag $ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    def _f(v, fmt="{:.2f}"):
        return fmt.format(v) if v is not None else "—"
    for r in sorted(real, key=lambda r: (r.fill_date or "", r.ticker)):
        lines.append(
            f"| {r.ticker} | {r.setup_type or '—'} | {r.fill_date} | {_f(r.pivot_price)} "
            f"| {_f(r.fill_price)} | {_f(r.total_slippage_bps)} | {_f(r.delay_cost_bps)} "
            f"| {_f(r.execution_residual_bps)} | {_f(r.drag_usd)} |"
        )
    lines += ["", "### Monthly (notional-weighted)", "",
              "| month | n | notional $ | total bps | delay bps | exec bps | drag $ | flag |",
              "|---|---|---|---|---|---|---|---|"]
    for m in monthly_summary(rows):
        lines.append(
            f"| {m['month']} | {m['n_trades']} | {m['notional_usd']:,.0f} "
            f"| {_f(m['weighted_total_bps'])} | {_f(m['weighted_delay_bps'])} "
            f"| {_f(m['weighted_execution_bps'])} | {m['drag_usd']:,.2f} "
            f"| {'⚠ OVER THRESHOLD' if m['flag'] else 'ok'} |"
        )
    if suspects:
        lines += ["", "### Suspect rows (|total| > "
                  f"{SUSPECT_BPS:.0f} bps — data problems, investigate, do not average)", ""]
        for r in suspects:
            lines.append(
                f"- {r.ticker} {r.fill_date}: pivot {_f(r.pivot_price)} vs fill "
                f"{_f(r.fill_price)} -> {_f(r.total_slippage_bps)} bps (state={r.state})"
            )
    return "\n".join(lines) + "\n"


def render_skips_markdown(skips: list[SkipRow]) -> str:
    """Every unfilled entry order with its explanation status (Task 0)."""
    def _f(v, fmt="{:.2f}"):
        return fmt.format(v) if v is not None else "—"
    lines = ["### Skips — unfilled entry orders (each must explain itself)", ""]
    if not skips:
        return "\n".join(lines + ["- none recorded", ""])
    lines += [
        "| ticker | setup | attempt | pivot | limit | day low | ~09:35 | explained? | basis |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(skips, key=lambda r: (r.attempt_date or "", r.ticker)):
        mark = {True: "YES", False: "**NO — FAULT**", None: "no data"}[r.skip_explained]
        lines.append(
            f"| {r.ticker} | {r.setup_type or '—'} | {r.attempt_date or '—'} "
            f"| {_f(r.pivot_price)} | {_f(r.limit_price_placed)} "
            f"| {_f(r.attempt_day_low)} | {_f(r.price_0935)} | {mark} | {r.explain_basis} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Signal-vs-execution attribution (B2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bp = sub.add_parser("backfill", help="scan all ledgers, append new rows, print report")
    bp.add_argument("--ledger-dir", default=None)
    bp.add_argument("--no-append", action="store_true", help="report only, don't write series")
    up = sub.add_parser("update", help="nightly: scan + append new rows (quiet)")
    up.add_argument("--ledger-dir", default=None)
    sub.add_parser("report", help="render markdown from ledger scan (no writes)")
    args = ap.parse_args(argv)

    ledger_dir = Path(args.ledger_dir) if getattr(args, "ledger_dir", None) else None
    rows = compute_rows(ledger_dir)
    skips = compute_skip_rows(ledger_dir)
    if args.cmd == "backfill":
        if not args.no_append:
            path, n_new = append_series(rows + skips)
            print(f"series: {path} (+{n_new} new rows)")
        print(render_markdown(rows))
        print(render_skips_markdown(skips))
    elif args.cmd == "update":
        path, n_new = append_series(rows + skips)
        print(f"series: {path} (+{n_new} new rows)")
        for m in monthly_summary(rows):
            if m["flag"]:
                print(f"FLAG: {m['month']} drag {m['weighted_total_bps']} bps "
                      f"> {DRAG_FLAG_BPS_PER_MONTH} bps threshold")
        for s in skips:
            if s.skip_explained is False:
                print(f"FAULT: {s.ticker} {s.attempt_date} unfilled but day low "
                      f"{s.attempt_day_low} <= limit {s.limit_price_placed} — "
                      "unexplained non-fill (C2)")
    else:
        print(render_markdown(rows))
        print(render_skips_markdown(skips))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
