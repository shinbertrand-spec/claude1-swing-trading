#!/usr/bin/env python
"""
screen_tickers.py — fast framework-applicability screen for a list of tickers.

Given a list of tickers (CLI args or one-per-line on stdin), pulls 1y OHLCV and
flags which ones are *applicable* as discretionary swing entries under the
CLAUDE.md gates that actually bind in practice:

  APPLICABLE  ==  in an uptrend (last > SMA50 and > SMA200)
              AND stoppable (2 x ATR%  <  stop cap, default 8%)
              AND liquid (20d avg volume > ADV floor, default 1M shares)

This is the same screen used by the `denoise` subagent to cut a finfluencer
ticker firehose down to the handful worth a real deep-dive. It is a TRIAGE
filter, not a verdict — anything that passes still goes through
trade-researcher -> trade-skeptic -> risk-and-compliance before any trade.

Names already in journal/positions.json or journal/watchlist.json are tagged
[held]/[watch] so the screen surfaces genuinely new ideas.

Usage:
  uv run python scripts/screen_tickers.py CSCO TSM AMD INTC NBIS ...
  echo "CSCO TSM AMD" | uv run python scripts/screen_tickers.py
  uv run python scripts/screen_tickers.py --cap 12 --adv 0.5 AMD INTC   # try a wider stop cap
  uv run python scripts/screen_tickers.py --json CSCO TSM               # machine-readable

Flags:
  --cap N    stop-cap percent the 2xATR stop must fit under (default 8.0)
  --adv N    ADV floor in millions of shares (default 1.0)
  --json     emit JSON instead of the text tables
"""
from __future__ import annotations
import sys
import json
import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]


def wilder_atr(df: pd.DataFrame, n: int = 14) -> float:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + up / dn)).iloc[-1])


def _known_names() -> dict[str, str]:
    """Map ticker -> tag ([held]/[watch]) from the live journal files."""
    tags: dict[str, str] = {}
    pj = ROOT / "journal" / "positions.json"
    wl = ROOT / "journal" / "watchlist.json"
    try:
        for p in json.loads(pj.read_text()).get("positions", []):
            if t := p.get("ticker"):
                tags[t.upper()] = "held"
    except Exception:
        pass
    try:
        for e in json.loads(wl.read_text()).get("watchlist", []):
            if t := e.get("ticker"):
                tags.setdefault(t.upper(), "watch")
    except Exception:
        pass
    return tags


def screen_one(ticker: str, cap_pct: float, adv_floor: float) -> dict | None:
    df = yf.Ticker(ticker).history(period="1y", interval="1d").dropna()
    if len(df) < 60:
        return None
    c = df["Close"]
    last = float(c.iloc[-1])
    s20 = float(c.rolling(20).mean().iloc[-1])
    s50 = float(c.rolling(50).mean().iloc[-1])
    s200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else float("nan")
    atr = wilder_atr(df)
    atrp = atr / last * 100
    adv = float(df["Volume"].iloc[-20:].mean())
    yh = float(c.max())
    has200 = s200 == s200  # not NaN
    uptrend = last > s50 and (not has200 or last > s200)
    stoppable = (2 * atrp) < cap_pct
    liquid = adv > adv_floor * 1e6
    return {
        "ticker": ticker.upper(),
        "last": round(last, 2),
        "v50d_pct": round((last - s50) / s50 * 100, 1),
        "atr_pct": round(atrp, 1),
        "atr2x_pct": round(2 * atrp, 1),
        "rsi": round(rsi(c)),
        "from_hi_pct": round((1 - last / yh) * 100, 1),
        "dist20d_pct": round((last - s20) / s20 * 100, 1),
        "adv_m": round(adv / 1e6, 1),
        "uptrend": uptrend,
        "stoppable": stoppable,
        "liquid": liquid,
        "applicable": uptrend and stoppable and liquid,
    }


def reasons(r: dict, cap_pct: float) -> str:
    why = []
    if not r["uptrend"]:
        why.append("downtrend(<50/200d)")
    if not r["stoppable"]:
        why.append(f"2xATR={r['atr2x_pct']:.0f}%>{cap_pct:.0f}cap")
    if not r["liquid"]:
        why.append("thin")
    return ", ".join(why)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--cap", type=float, default=8.0, help="stop-cap %% the 2xATR stop must fit under")
    ap.add_argument("--adv", type=float, default=1.0, help="ADV floor in millions of shares")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tickers = [t.strip().upper().lstrip("$") for t in args.tickers]
    if not tickers and not sys.stdin.isatty():
        tickers = [w.strip().upper().lstrip("$") for line in sys.stdin for w in line.split()]
    tickers = [t for t in dict.fromkeys(tickers) if t]  # dedupe, keep order
    if not tickers:
        print("no tickers given", file=sys.stderr)
        return 2

    tags = _known_names()
    results, errors = [], []
    for t in tickers:
        try:
            r = screen_one(t, args.cap, args.adv)
            if r is None:
                errors.append((t, "insufficient history"))
            else:
                r["tag"] = tags.get(t, "")
                results.append(r)
        except Exception as e:  # noqa: BLE001
            errors.append((t, str(e)[:50]))

    if args.json:
        print(json.dumps({"cap_pct": args.cap, "adv_floor_m": args.adv,
                          "results": results, "errors": errors}, indent=2))
        return 0

    ap_rows = sorted([r for r in results if r["applicable"]], key=lambda r: abs(r["dist20d_pct"]))
    no_rows = [r for r in results if not r["applicable"]]

    print(f"=== APPLICABLE (uptrend + 2xATR<{args.cap:.0f}% + ADV>{args.adv}M) — sorted by proximity to 20d ===")
    if not ap_rows:
        print("  (none)")
    for r in ap_rows:
        tag = f" [{r['tag']}]" if r["tag"] else ""
        print(f"  {r['ticker']:6s}{tag:8s} ${r['last']:8.2f} | v50d={r['v50d_pct']:+5.0f}% "
              f"ATR={r['atr_pct']:4.1f}%(2x={r['atr2x_pct']:.0f}) RSI={r['rsi']:3.0f} "
              f"frmHi=-{r['from_hi_pct']:.0f}% dist20d={r['dist20d_pct']:+.0f}% ADV={r['adv_m']:.0f}M")

    print(f"\n=== NOT APPLICABLE ({len(no_rows)}) ===")
    for r in no_rows:
        tag = f" [{r['tag']}]" if r["tag"] else ""
        print(f"  {r['ticker']:6s}{tag:8s} ${r['last']:8.2f} v50d={r['v50d_pct']:+5.0f}% "
              f"ATR={r['atr_pct']:.1f}% -> {reasons(r, args.cap)}")

    if errors:
        print(f"\n=== NO DATA ({len(errors)}) ===")
        for t, e in errors:
            print(f"  {t:6s} -- {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
