"""Integrated Value + Momentum — ONE portfolio on a COMBINED score.

The #1 cost-surviving, momentum-diversifying setup of the factor roster. Value
is ~negatively correlated to momentum, so an INTEGRATED score (rank every name
by 0.5·z_value + 0.5·z_momentum) — NOT a blend of a value book and a momentum
book — lets opposing signals net out: cheap-but-falling and expensive-but-rising
names score middling and never trade, which is what lowers turnover and lifts net
IR (AQR 2015). That difference IS the edge.

Construction (cost-survival levers, all load-bearing):
  * COMBINED score, single ranking — not two sleeves averaged.
  * BANDING / buy-hold spread: a name enters only in the top ``entry_band_pct``
    and is HELD until it falls past ``exit_band_pct`` (looser exit than entry).
    The band is what keeps realized turnover under the cost-survival threshold;
    it is expressed to the immutable portfolio simulator as a per-entry
    ``max_hold_days`` equal to the banded holding span.
  * Monthly signal refresh; sector-neutral z-scores; winsorized.
  * 8-position cap (CLAUDE.md hard rule). A top-decile basket can't be held at
    8 names, so the deployable form is the banded top-8 by combined score. The
    KIND itself enforces the 8-cap selection (the simulator would otherwise drop
    overflow in arbitrary ticker order — the ts_momentum tie-break failure).
  * NON-CHASE entry: routed to REVERSION_KINDS (limit at pivot), because this is
    a low-turnover rebalance, not a breakout.

Signals (cross-sectional, PIT):
  * Value   = z(B/M) + z(E/P) + z(FCF yield), each winsorized + sector-neutral,
    using fundamentals KNOWABLE at the rebalance date (tools.fundamentals.
    pit_fundamentals — as-filed, filed<=asof).
  * Momentum = 12-1 cross-sectional (return from t-(lookback+skip) to t-skip),
    sector-neutral. Skipping the last month avoids the 1-month reversal.

Baselines fall out of ``value_weight``: 1.0 = value-only, 0.0 = momentum-only,
0.5 = integrated. The gate runner compares all three.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import date
from typing import NamedTuple, Optional

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal
from ...fundamentals import pit_fundamentals as pf
from .. import factor_utils as fu
from .. import sector_map as sm

KIND = "value_momentum_integrated"

DEFAULT_TOP_K = 8
DEFAULT_REBALANCE = 21          # ~monthly
DEFAULT_MOM_LOOKBACK = 252      # 12 months
DEFAULT_MOM_SKIP = 21           # skip last month (12-1)
DEFAULT_ENTRY_BAND = 0.20       # enter top 20%
DEFAULT_EXIT_BAND = 0.45        # hold until past 45% (looser exit)


class IntegratedState(NamedTuple):
    spans_by_ticker: dict[str, list[tuple[date, Optional[date], float]]]
    rebalance_dates: list[pd.Timestamp]


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _rebalance_calendar(universe_dfs, benchmark):
    if benchmark and benchmark in universe_dfs:
        return list(universe_dfs[benchmark].index)
    best: list = []
    for df in universe_dfs.values():
        if len(df.index) > len(best):
            best = list(df.index)
    return best


def _momentum_12_1(df: pd.DataFrame, d: pd.Timestamp, lookback: int, skip: int) -> Optional[float]:
    """Return from t-(lookback+skip) to t-skip (skips the most recent month)."""
    if d not in df.index:
        return None
    pos = df.index.get_loc(d)
    if not isinstance(pos, int) or pos < lookback + skip:
        return None
    closes = df["Close"]
    c_recent = float(closes.iloc[pos - skip])
    c_old = float(closes.iloc[pos - skip - lookback])
    if c_old <= 0 or pd.isna(c_old) or pd.isna(c_recent):
        return None
    return (c_recent / c_old) - 1.0


def _close_at(df: pd.DataFrame, d: pd.Timestamp) -> Optional[float]:
    if d not in df.index:
        return None
    c = float(df.loc[d]["Close"])
    return c if c > 0 and not pd.isna(c) else None


def _load_facts(tickers, params):
    """{ticker -> raw company-facts dict}. Injectable via params['_facts_by_ticker']."""
    if params.get("_facts_by_ticker") is not None:
        return params["_facts_by_ticker"]
    cik_map = pf.load_ticker_cik_map()
    out = {}
    for t in tickers:
        cik = cik_map.get(t)
        if not cik:
            continue
        try:
            out[t] = pf.fetch_company_facts(cik)
        except Exception:        # noqa: BLE001 — missing/odd CIKs skipped
            continue
    return out


def _extract_points(facts_by_ticker: dict) -> dict:
    """Pre-extract the value concepts ONCE per ticker (avoid re-parsing the facts
    dict on every rebalance date). Only the 5 concepts the value factor needs."""
    out = {}
    for t, facts in facts_by_ticker.items():
        out[t] = {
            "book": pf.extract_points(facts, pf.BOOK_EQUITY),
            "shares": pf.extract_points(facts, pf.SHARES),
            "ni": pf.extract_points(facts, pf.NET_INCOME),
            "ocf": pf.extract_points(facts, pf.OCF),
            "capex": pf.extract_points(facts, pf.CAPEX),
        }
    return out


def _value_zscores(tickers, d, universe_dfs, points_by_ticker, sectors, winsor):
    """z(B/M)+z(E/P)+z(FCF yield), winsorized + sector-neutral, re-standardized.

    Uses pre-extracted PIT point-lists; all values are as-filed (filed<=asof).
    """
    bm, ep, fcfy = {}, {}, {}
    asof = d.date() if hasattr(d, "date") else d
    for t in tickers:
        pts = points_by_ticker.get(t)
        price = _close_at(universe_dfs[t], d)
        if not pts or price is None:
            continue
        sh = pf.latest_stock_as_of(pts["shares"], asof)
        if sh is None or sh.val <= 0:
            continue
        mcap = price * sh.val
        if mcap <= 0:
            continue
        bk = pf.latest_stock_as_of(pts["book"], asof)
        if bk is not None and bk.val > 0:
            bm[t] = bk.val / mcap
        ni = pf.ttm_flow_as_of(pts["ni"], asof)
        if ni is not None:
            ep[t] = ni.val / mcap
        ocf = pf.ttm_flow_as_of(pts["ocf"], asof)
        capex = pf.ttm_flow_as_of(pts["capex"], asof)
        if ocf is not None and capex is not None:
            fcfy[t] = (ocf.val - capex.val) / mcap
    z_bm = fu.standardize_factor(bm, sectors, winsor_limit=winsor)
    z_ep = fu.standardize_factor(ep, sectors, winsor_limit=winsor)
    z_fcfy = fu.standardize_factor(fcfy, sectors, winsor_limit=winsor)
    common = set(z_bm) & set(z_ep) & set(z_fcfy)   # require all 3 components
    composite = {t: z_bm[t] + z_ep[t] + z_fcfy[t] for t in common}
    return fu.zscore(composite)


def _momentum_zscores(tickers, d, universe_dfs, sectors, lookback, skip, winsor):
    raw = {}
    for t in tickers:
        m = _momentum_12_1(universe_dfs[t], d, lookback, skip)
        if m is not None:
            raw[t] = m
    return fu.standardize_factor(raw, sectors, winsor_limit=winsor)


def _banded_membership(scores_by_date, rebalance_dates, top_k, entry_band, exit_band):
    """Banded top-K held-set per rebalance date (buy-hold spread)."""
    held_by_date: dict = {}
    prev: set = set()
    for d in rebalance_dates:
        scores = scores_by_date.get(d, {})
        if not scores:
            held_by_date[d] = set(prev)   # no fresh scores → carry holdings
            continue
        # deterministic tie-break on ticker (set/dict order is hash-seed dependent)
        ranked = sorted(scores, key=lambda t: (-scores[t], t))
        n = len(ranked)
        pct = {t: (i / (n - 1) if n > 1 else 0.0) for i, t in enumerate(ranked)}
        keep = sorted((t for t in prev if t in scores and pct[t] <= exit_band),
                      key=lambda t: (-scores[t], t))
        held = list(keep)
        if len(held) < top_k:
            for t in ranked:
                if len(held) >= top_k:
                    break
                if t not in held and pct[t] <= entry_band:
                    held.append(t)
        held_by_date[d] = set(held)
        prev = set(held)
    return held_by_date


def _membership_to_spans(held_by_date, scores_by_date, rebalance_dates):
    open_entry: dict = {}     # ticker -> (entry_date, score)
    spans: dict = defaultdict(list)
    for d in rebalance_dates:
        held = held_by_date.get(d, set())
        for t in sorted(open_entry):
            if t not in held:
                ed, sc = open_entry.pop(t)
                spans[t].append((ed.date(), d.date(), sc))
        for t in sorted(held):
            if t not in open_entry:
                open_entry[t] = (d, scores_by_date.get(d, {}).get(t, 0.0))
    for t, (ed, sc) in open_entry.items():
        spans[t].append((ed.date(), None, sc))
    return dict(spans)


# --------------------------------------------------------------------------- #
# KIND contract                                                              #
# --------------------------------------------------------------------------- #
def precompute(universe_dfs: dict[str, pd.DataFrame], params: dict) -> IntegratedState:
    benchmark = params.get("benchmark")
    value_w = float(params.get("value_weight", 0.5))
    mom_w = float(params.get("momentum_weight", 1.0 - value_w))
    top_k = int(params.get("top_k", DEFAULT_TOP_K))
    rebalance = int(params.get("rebalance_period_days", DEFAULT_REBALANCE))
    lookback = int(params.get("mom_lookback_days", DEFAULT_MOM_LOOKBACK))
    skip = int(params.get("mom_skip_days", DEFAULT_MOM_SKIP))
    entry_band = float(params.get("entry_band_pct", DEFAULT_ENTRY_BAND))
    exit_band = float(params.get("exit_band_pct", DEFAULT_EXIT_BAND))
    winsor = float(params.get("winsor_limit", 0.02))
    sector_neutral = bool(params.get("sector_neutral", True))

    tickers = [t for t in universe_dfs if t != benchmark]
    cal = _rebalance_calendar(universe_dfs, benchmark)
    warmup = max(lookback + skip, 2)
    if len(cal) < warmup + 2:
        return IntegratedState({}, [])
    rebalance_dates = [cal[i] for i in range(warmup, len(cal), rebalance)]

    sectors = sm.build_sector_map(tickers) if sector_neutral else {}
    points_by_ticker = (_extract_points(_load_facts(tickers, params))
                        if value_w != 0 else {})

    scores_by_date: dict = {}
    for d in rebalance_dates:
        factors: dict = {}
        if value_w != 0:
            zv = _value_zscores(tickers, d, universe_dfs, points_by_ticker, sectors, winsor)
            if zv:
                factors["value"] = zv
        if mom_w != 0:
            zm = _momentum_zscores(tickers, d, universe_dfs, sectors, lookback, skip, winsor)
            if zm:
                factors["mom"] = zm
        combined = fu.combine(factors, {"value": value_w, "mom": mom_w})
        if combined:
            scores_by_date[d] = combined

    held = _banded_membership(scores_by_date, rebalance_dates, top_k, entry_band, exit_band)
    spans = _membership_to_spans(held, scores_by_date, rebalance_dates)
    return IntegratedState(spans, rebalance_dates)


def replay(df, ticker, params, state):
    if ticker == params.get("benchmark") or state is None:
        return []
    spans = state.spans_by_ticker.get(ticker)
    if not spans:
        return []
    atr_period = int(params.get("atr_period", 20))
    atr_mult = float(params.get("atr_stop_multiple", 3.0))
    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close")

    df_index = list(df.index)
    # Match by DATE (the df index may be tz-aware while spans carry plain dates).
    dates_sorted = [ts.date() for ts in df_index]
    date_to_pos = {d: k for k, d in enumerate(dates_sorted)}
    signals: list[TradeSignal] = []
    for entry_d, exit_d, score in spans:
        entry_d = entry_d.date() if hasattr(entry_d, "date") else entry_d
        i = date_to_pos.get(entry_d)
        if i is None or i + 1 >= len(df_index):
            continue
        entry_price = float(df.iloc[i + 1]["Open"])
        if entry_price <= 0 or pd.isna(entry_price):
            continue
        atr = _compute_atr(df.iloc[: i + 1], atr_period)
        if atr is None or atr <= 0:
            continue
        stop_price = entry_price - atr_mult * atr
        if stop_price >= entry_price:
            continue
        # banding hold length → max_hold (bars from fill bar to exit-rebalance bar)
        fill_i = i + 1
        if exit_d is None:
            max_hold = len(df_index) - fill_i      # hold to end of data
        else:
            exit_d = exit_d.date() if hasattr(exit_d, "date") else exit_d
            ex_pos = bisect.bisect_right(dates_sorted, exit_d) - 1   # bar on/before exit
            max_hold = int(ex_pos - fill_i)
        if max_hold < 1:
            continue
        signals.append(TradeSignal(
            ticker=ticker, setup_type=KIND, setup_grade="B",
            entry_date=entry_d, fill_date=pd.Timestamp(df_index[i + 1]).date(),
            entry_price=entry_price, stop_price=stop_price, target_price=None,
            max_hold_days=max_hold, atr_at_signal=atr,
            notes={"combined_score": float(score),
                   "entry_rebalance": str(entry_d),
                   "exit_rebalance": str(exit_d) if exit_d else None},
        ))
    return signals


def _compute_atr(df: pd.DataFrame, period: int) -> Optional[float]:
    if len(df) < period + 1:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    tr[0] = high[0] - low[0]
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])
