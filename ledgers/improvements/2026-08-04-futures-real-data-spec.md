# Spec — real-futures data pull + long/short trend backtest

**Pilot artifact, 2026-08-04. Suggest-only.** This spec + the companion runner
(`2026-08-04-futures-real-data-backtest.py`) make the real-futures test turnkey: the
operator runs one Databento pull (I never touch the API key), then runs the backtest. It
extends `ts_momentum` — a *proven* edge — onto real futures, and tests the 2022-style
crisis-alpha diversification the free ETF proxy only hinted at (L/S +0.64 vs SPY −0.78 in
2022, but standalone Sharpe 0.15 and +0.25 corr to SPY).

## 1. Objective + promote/kill criteria

Answer, on the best available data, the open question from `project_crossasset_trend`:
does a **long/short** trend book on real futures (a) clear the net-of-cost gate, and (b)
diversify our equity-long edge?

- **Promote** → blind judge + critic: net-of-cost aggregate **Sharpe > 1.0 AND |MDD| < 25%
  AND ≥50% of 6 OOS years (2020-25) clear Sharpe > 0.5 AND DSR > 0.95**, AND diversifying
  (|corr| to ES < ~0.3, positive in 2022/crisis years).
- **Kill** (stop, edge confirmed absent): real-data L/S no better than the ETF proxy
  (Sharpe < ~0.5). The proxy already said the shorts detract; real data with roll yield is
  the fair retest, not a second bite.

## 2. Contract universe (28 roots, 6 sectors — CME/CBOT/NYMEX/COMEX, one dataset)

| Sector | Roots |
|---|---|
| Equity | ES (S&P 500), NQ (Nasdaq-100), RTY (Russell 2000), YM (Dow) |
| Rates | ZT (2y), ZF (5y), ZN (10y), ZB (30y), UB (Ultra Bond) |
| FX | 6E (EUR), 6J (JPY), 6B (GBP), 6A (AUD), 6C (CAD), 6S (CHF) |
| Energy | CL (WTI crude), NG (nat gas), HO (heating oil), RB (RBOB gasoline) |
| Metals | GC (gold), SI (silver), HG (copper) |
| Grains/meats | ZC (corn), ZW (wheat), ZS (soybeans), ZL (soy oil), ZM (soy meal), LE (live cattle) |

All on **GLBX.MDP3** (CME Globex), so a single Databento dataset covers everything.
*Optional extension:* ICE softs (SB sugar, KC coffee, CT cotton, CC cocoa) live on
**IFUS** — a second dataset/pull if you want ags breadth. Not required for v1.

## 3. The Databento pull (operator runs — needs your own API key)

- **Dataset:** `GLBX.MDP3` · **Schema:** `ohlcv-1d` · **Symbology:** continuous, volume-roll
  front month (`<ROOT>.v.0`) · **Range:** `2010-06-01 .. 2026-05-25` (GLBX.MDP3 history
  starts ~2010-06; this misses the 2008 GFC — noted caveat, still includes 2018/2020/2022).
- **Cost:** daily OHLCV for 28 continuous contracts × ~16y is a *tiny* data volume (~100k
  records, a few MB). This is well within Databento's free signup credit or a few dollars
  pay-as-you-go — **effectively $0–30 for daily bars.** (Intraday/tick would be far more —
  not needed here.)
- **Credentials:** set `DATABENTO_API_KEY` in your env. **Do not paste the key into any
  file, journal, or Telegram** (CLAUDE.md § Sensitive Information). The pilot wall means I
  don't run this — you do.

```python
# operator runs locally; writes back-adjusted CSVs the backtest reads.
# pip install databento pandas   ·   set DATABENTO_API_KEY in env first.
import os, databento as db, pandas as pd, numpy as np
OUT = "ledgers/improvements/_futures_data"; os.makedirs(OUT, exist_ok=True)
ROOTS = ["ES","NQ","RTY","YM","ZT","ZF","ZN","ZB","UB","6E","6J","6B","6A","6C","6S",
         "CL","NG","HO","RB","GC","SI","HG","ZC","ZW","ZS","ZL","ZM","LE"]
client = db.Historical()  # reads DATABENTO_API_KEY from env

def back_adjust(df):
    # ratio back-adjustment across rolls: when instrument_id changes, scale the older
    # segment so the series is continuous (no artificial roll gap).
    df = df.sort_values("ts_event").reset_index(drop=True)
    ids = df["instrument_id"].values; close = df["close"].astype(float).values.copy()
    roll_idx = [i for i in range(1, len(df)) if ids[i] != ids[i-1]]
    for i in reversed(roll_idx):
        ratio = close[i] / close[i-1] if close[i-1] else 1.0
        close[:i] *= ratio
    df["close_adj"] = close
    return df

for root in ROOTS:
    data = client.timeseries.get_range(
        dataset="GLBX.MDP3", schema="ohlcv-1d", stype_in="continuous",
        symbols=[f"{root}.v.0"], start="2010-06-01", end="2026-05-25")
    df = data.to_df().reset_index()                      # columns incl ts_event, instrument_id, close
    df = back_adjust(df)
    out = pd.DataFrame({"date": pd.to_datetime(df["ts_event"]).dt.date, "close": df["close_adj"]})
    out.to_csv(f"{OUT}/{root}.csv", index=False)
    print(f"{root}: {len(out)} rows -> {OUT}/{root}.csv")
```

> **Verify against current Databento docs before running** — API surfaces drift. Confirm
> the continuous symbology (`.v.0`), that `ohlcv-1d` returns `instrument_id` + `close`, and
> the roll convention. If Databento exposes a pre-adjusted continuous product, prefer it
> and drop `back_adjust`. (Same "re-verify, never guess" discipline as `model_cutoffs`.)

## 4. Data contract the backtest expects

One `<ROOT>.csv` per contract in `ledgers/improvements/_futures_data/`, columns
`date,close` where `close` is the **back-adjusted continuous settle**. The runner
`load_prices()` is tolerant (accepts `close` or `settle`, any column case). Absent
directory → the runner prints these instructions and exits 0 (safe to run anytime).

## 5. Method (in the runner — a proper CTA, upgrades over the proxy)

- **Signal:** 12-1 TSMOM sign (base) + a 3-6-12-month blend variant (smoother, less
  whipsaw — the proxy's weakness in 2020/2023).
- **Sizing:** inverse-realized-vol risk weighting, **35% gross cap per sector** (so one
  sector can't dominate), portfolio **vol-targeted to 12%** annualized (CTA-typical; makes
  MDD comparable across variants — Sharpe is unchanged by scaling).
- **Cost:** 2 bps per unit turnover (futures are cheap), charged on monthly rebalance.
- **Modes:** L/S vs LONG-only vs corr-to-ES, each gated + per-year + DSR.

## 6. Caveats (honest limits)

- **2010 start misses the 2008 GFC** — the single best managed-futures year. Real edge
  could look better with pre-2010 data (needs a different vendor; ICE/older CME history).
- **Vol-target uses trailing realized vol** (shifted, no lookahead) — a live book would
  size similarly; fine.
- **Blind-critic wrinkle:** if this promotes, the pilot's blind critic MUST re-run the
  backtest, so the critic needs the same `_futures_data/` locally. The pull is done once
  and shared.
- Skew, contract liquidity at the tails, and fills at settle (not intraday) are
  approximations — acceptable for a daily-rebalanced trend book, not for anything faster.

## 7. Run order

1. Operator: set `DATABENTO_API_KEY`, run the §3 snippet → populates `_futures_data/`.
2. `uv run python ledgers/improvements/2026-08-04-futures-real-data-backtest.py`
3. Read the emitted `.md`. If a variant clears §1's promote bar → hand to blind judge +
   critic (the pilot process). Else → kill, edge confirmed absent on best-available data.

Suggest-only; nothing applied.
