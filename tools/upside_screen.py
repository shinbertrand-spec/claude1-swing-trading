"""Upside screen — rank a themed universe by the "winner DNA".

A repeatable idea-generation screen for the HUMAN-DISCRETIONARY track. It does
NOT find guaranteed 3x names (nobody can; see the strategy-search conclusion in
project memory). It finds the *profile* the realised winners (NBIS / MRVL /
QCOM) shared at the time you'd have bought them: a strong, smooth, established
uptrend in a liquid name — and ranks the universe by it so you consistently see
the strongest momentum candidates instead of eyeballing charts.

The momentum rank is the **Clenow Stocks-on-the-Move regression score**
(annualised exponential-regression log-slope x R^2) — reused verbatim from the
project's own backtested `clenow_momentum` kind, so "what the screen rewards" is
the same math the deployment gate validated. High score = strong AND smooth
(choppy moves are penalised by the R^2 term).

This is TRIAGE, not a verdict. Anything it surfaces still goes through
trade-researcher -> trade-skeptic -> risk-and-compliance before a trade. It
places nothing.

CLI::

    uv run python -m tools.upside_screen                 # default: broad AI universe
    uv run python -m tools.upside_screen --pure          # tight 41-name AI universe
    uv run python -m tools.upside_screen --tickers NVDA MRVL NBIS
    uv run python -m tools.upside_screen --top 15 --lookback 90 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml

# Reuse the EXACT validated Clenow regression score from the backtested kind.
from tools.quant_strategies._kinds.clenow_momentum import _annualised_log_slope_r2

ROOT = Path(__file__).resolve().parents[1]
_UNIVERSE_DIR = ROOT / "tools" / "quant_strategies" / "_universes"
BROAD_UNIVERSE = _UNIVERSE_DIR / "ai_thematic_broad_2026q2.yml"
PURE_UNIVERSE = _UNIVERSE_DIR / "ai_thematic_pure_2026q2.yml"

DEFAULT_LOOKBACK = 90          # Clenow momentum_lookback_days
DEFAULT_TREND_SMA = 100        # Clenow regime/trend filter period
DEFAULT_ADV_FLOOR_M = 1.0      # millions of shares
GAP_FLAG_PCT = 15.0            # Clenow disqualifier: a >15% single-day move in window


def load_universe(path: str | Path) -> list[str]:
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return [str(t).upper() for t in (doc.get("tickers") or [])]


def _wilder_atr(df: pd.DataFrame, n: int = 14) -> float:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def _rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + up / dn)).iloc[-1])


def score_ticker(
    ticker: str,
    df: pd.DataFrame,
    *,
    lookback: int = DEFAULT_LOOKBACK,
    trend_sma: int = DEFAULT_TREND_SMA,
    adv_floor_m: float = DEFAULT_ADV_FLOOR_M,
) -> Optional[dict[str, Any]]:
    """Pure scoring of one OHLCV frame. Returns a row dict, or None if too short.

    ``qualifies`` is True iff the name is in an established uptrend (price above
    its ``trend_sma``) AND liquid (ADV above the floor). The Clenow score is
    computed regardless so even non-qualifying names can be inspected.
    """
    df = df.dropna()
    if len(df) < max(lookback, trend_sma) + 5:
        return None
    c = df["Close"]
    last = float(c.iloc[-1])
    score = _annualised_log_slope_r2(c, lookback)
    if not np.isfinite(score):
        return None

    # Decompose the score for display: annualised slope and R^2 separately.
    window = np.log(c.iloc[-lookback:].to_numpy())
    x = np.arange(len(window), dtype=float)
    slope, intercept = np.polyfit(x, window, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((window - fitted) ** 2))
    ss_tot = float(np.sum((window - window.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    ann_slope = float(np.exp(slope * 252) - 1.0)

    sma_t = float(c.rolling(trend_sma).mean().iloc[-1])
    sma50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else float("nan")
    sma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else float("nan")
    adv = float(df["Volume"].iloc[-20:].mean())
    yhigh = float(c.max())
    ret_3m = float(last / c.iloc[-63] - 1.0) if len(c) >= 63 else float("nan")
    ret_6m = float(last / c.iloc[-126] - 1.0) if len(c) >= 126 else float("nan")
    daily = c.pct_change().iloc[-lookback:]
    max_gap = float(daily.abs().max() * 100) if len(daily) else 0.0

    uptrend = last > sma_t
    liquid = adv > adv_floor_m * 1e6

    return {
        "ticker": ticker.upper(),
        "score": round(score, 4),
        "ann_slope_pct": round(ann_slope * 100, 1),
        "r2": round(r2, 3),
        "last": round(last, 2),
        "ret_3m_pct": round(ret_3m * 100, 1) if ret_3m == ret_3m else None,
        "ret_6m_pct": round(ret_6m * 100, 1) if ret_6m == ret_6m else None,
        "from_hi_pct": round((1 - last / yhigh) * 100, 1),
        "above_50": bool(last > sma50) if sma50 == sma50 else None,
        "above_trend_sma": bool(uptrend),
        "above_200": bool(last > sma200) if sma200 == sma200 else None,
        "rsi": round(_rsi(c)),
        "atr_pct": round(_wilder_atr(df) / last * 100, 1),
        "adv_m": round(adv / 1e6, 1),
        "max_gap_pct": round(max_gap, 1),
        "gap_flag": max_gap > GAP_FLAG_PCT,
        "uptrend": uptrend,
        "liquid": liquid,
        "qualifies": uptrend and liquid,
    }


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Qualifying names sorted by Clenow score (desc). Non-qualifiers dropped."""
    return sorted(
        [r for r in rows if r.get("qualifies")],
        key=lambda r: r["score"],
        reverse=True,
    )


def _known_names() -> dict[str, str]:
    tags: dict[str, str] = {}
    for fn, tag in (("positions.json", "held"), ("watchlist.json", "watch")):
        try:
            doc = json.loads((ROOT / "journal" / fn).read_text())
            key = "positions" if "positions" in doc else "watchlist"
            for e in doc.get(key, []):
                if t := e.get("ticker"):
                    tags.setdefault(t.upper(), tag)
        except Exception:
            pass
    return tags


def _fetch(ticker: str) -> Optional[pd.DataFrame]:
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(period="1y", interval="1d")
        return df if len(df) else None
    except Exception:
        return None


def screen(
    tickers: list[str],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    trend_sma: int = DEFAULT_TREND_SMA,
    adv_floor_m: float = DEFAULT_ADV_FLOOR_M,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch + score every ticker. Returns (rows, errors)."""
    rows, errors = [], []
    for t in tickers:
        df = _fetch(t)
        if df is None:
            errors.append(t)
            continue
        r = score_ticker(t, df, lookback=lookback, trend_sma=trend_sma, adv_floor_m=adv_floor_m)
        if r is None:
            errors.append(t)
        else:
            rows.append(r)
    return rows, errors


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="tools.upside_screen",
        description="Rank a themed universe by the Clenow momentum 'winner DNA'. Discretionary triage only.",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--broad", action="store_true", help="broad AI universe (~132, default)")
    g.add_argument("--pure", action="store_true", help="tight pure-play AI universe (~41)")
    g.add_argument("--universe", help="path to a universe YAML with a tickers: list")
    g.add_argument("--tickers", nargs="+", help="explicit ticker list")
    ap.add_argument("--top", type=int, default=20, help="show top N ranked names")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    ap.add_argument("--trend-sma", type=int, default=DEFAULT_TREND_SMA)
    ap.add_argument("--adv", type=float, default=DEFAULT_ADV_FLOOR_M, help="ADV floor (M shares)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.tickers:
        tickers, src = [t.upper().lstrip("$") for t in args.tickers], "explicit"
    elif args.universe:
        tickers, src = load_universe(args.universe), Path(args.universe).stem
    elif args.pure:
        tickers, src = load_universe(PURE_UNIVERSE), "ai_thematic_pure"
    else:
        tickers, src = load_universe(BROAD_UNIVERSE), "ai_thematic_broad"

    rows, errors = screen(
        tickers, lookback=args.lookback, trend_sma=args.trend_sma, adv_floor_m=args.adv
    )
    ranked = rank(rows)
    tags = _known_names()
    for r in ranked:
        r["tag"] = tags.get(r["ticker"], "")

    if args.json:
        print(json.dumps({"universe": src, "n_scored": len(rows), "n_qualifying": len(ranked),
                          "ranked": ranked, "errors": errors}, indent=2))
        return 0

    print(f"=== UPSIDE SCREEN — {src} universe — ranked by Clenow momentum score ===")
    print(f"    {len(ranked)} in confirmed uptrend (> {args.trend_sma}d SMA) + liquid, of {len(rows)} scored")
    print(f"    score = annualised regression slope x R^2 (strong AND smooth). TRIAGE ONLY — not a buy.\n")
    print(f"    {'#':>2} {'TICKER':8} {'SCORE':>7} {'SLOPE%':>7} {'R2':>5} "
          f"{'3mo%':>6} {'6mo%':>6} {'frHi%':>6} {'RSI':>4} {'ATR%':>5} {'ADV(M)':>7}  FLAGS")
    for i, r in enumerate(ranked[: args.top], 1):
        tag = f"[{r['tag']}]" if r["tag"] else ""
        flags = []
        if r["gap_flag"]:
            flags.append(f"gap{r['max_gap_pct']:.0f}%")
        if r["above_200"] is False:
            flags.append("<200d")
        if r["rsi"] >= 80:
            flags.append("RSI>80")
        print(f"    {i:>2} {r['ticker']:8} {r['score']:>7.3f} {r['ann_slope_pct']:>6.0f}% "
              f"{r['r2']:>5.2f} {_p(r['ret_3m_pct']):>6} {_p(r['ret_6m_pct']):>6} "
              f"-{r['from_hi_pct']:>4.0f}% {r['rsi']:>4.0f} {r['atr_pct']:>4.1f}% {r['adv_m']:>6.0f}  "
              f"{' '.join(flags)} {tag}")
    if errors:
        print(f"\n    no data / too short ({len(errors)}): {', '.join(errors[:20])}"
              + (" ..." if len(errors) > 20 else ""))
    print("\n    Next: send a name to the full pipeline — trade-researcher -> trade-skeptic -> risk-and-compliance.")
    return 0


def _p(v: Any) -> str:
    return f"{v:+.0f}" if isinstance(v, (int, float)) else "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
