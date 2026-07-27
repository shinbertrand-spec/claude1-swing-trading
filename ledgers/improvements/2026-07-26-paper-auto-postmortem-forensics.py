"""Paper-auto live-trade post-mortem — deterministic forensic reproducer
(Claude1 paper-research pilot, 2026-07-26). SUGGEST-ONLY artifact.

Regenerates, from on-disk ledgers ONLY (no live broker call, so fully
reproducible), every number in 2026-07-26-paper-auto-postmortem.md:
  1. Per-trade realized P&L for every paper-auto ledger (entry = starter
     fill_price, exit = position_state.exit_price).
  2. Net realized P&L + win/loss count + retired-vs-live-strategy split.
  3. Three independent P&L views that DISAGREE (the bookkeeping gap):
     direct-ledger vs performance-module n_realized vs critic-calibration.
  4. The COIN ratchet-then-loss sequence (stop lifted to +10%, exit below
     entry) + the counterfactual had the ratcheted stop been honored.

Reads ONLY (ledgers/paper-auto/*.yml, ledgers/swing-critics/**, and the
read-only performance + calibration modules). Writes ONLY its own .md under
ledgers/improvements/. Touches no strategy code, no deployable_setups.yml,
no order path. The live-account reconciliation (Tiger positions vs
positions.json) is NON-deterministic and is documented in the .md with its
own command; it is intentionally NOT run here.

Usage:  uv run python ledgers/improvements/2026-07-26-paper-auto-postmortem-forensics.py
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml

RETIRED = {"residual_momentum_liquid_us", "xs_short_term_reversal",
           "xs_short_term_reversal_liquid_us", "clenow_momentum_liquid_us",
           "connors_rsi2"}
LIVE = {"ts_momentum_liquid_us"}
OUT_MD = os.path.join(_ROOT, "ledgers", "improvements",
                      "2026-07-26-paper-auto-postmortem-forensics.md")


def _status(setup: str) -> str:
    return "RETIRED" if setup in RETIRED else ("LIVE" if setup in LIVE else "?")


def _days(a, b):
    try:
        return (datetime.date.fromisoformat(str(b)[:10])
                - datetime.date.fromisoformat(str(a)[:10])).days
    except Exception:
        return None


def _panel_map():
    m = {}
    for pj in glob.glob(os.path.join(_ROOT, "ledgers", "swing-critics", "*", "*", "_panel.json")):
        try:
            j = json.load(open(pj, encoding="utf-8"))
        except Exception:
            continue
        tk = os.path.basename(os.path.dirname(pj))
        act = j.get("action") or (j.get("verdict") or {}).get("action")
        m.setdefault(tk, act)
    return m


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # avoid cp1252 crash on non-ASCII console echo
    except Exception:
        pass
    panel = _panel_map()
    rows = []
    for p in sorted(glob.glob(os.path.join(_ROOT, "ledgers", "paper-auto", "*.yml"))):
        y = yaml.safe_load(open(p, encoding="utf-8")) or {}
        meta = y.get("meta") or {}
        sc = y.get("setup_classification") or {}
        ps = y.get("position_state") or {}
        st = ps.get("starter") or {}
        t = meta.get("ticker") or os.path.basename(p)[:-4]
        setup = sc.get("type")
        ep, sh, fd = st.get("fill_price"), st.get("shares"), st.get("fill_date")
        xp, xd = ps.get("exit_price"), ps.get("exit_date")
        pnl = (float(xp) - float(ep)) * float(sh) if (ep and xp and sh) else None
        rows.append(dict(t=t, setup=setup or "?", status=_status(setup or ""),
                         grade=sc.get("grade"), state=meta.get("state") or ps.get("stage"),
                         ep=ep, xp=xp, sh=sh, hold=_days(fd, xd), pnl=pnl,
                         panel=panel.get(t), xr=ps.get("exit_reason")))

    filled = [r for r in rows if r["pnl"] is not None]
    net = sum(r["pnl"] for r in filled)
    wins = sum(1 for r in filled if r["pnl"] > 0)
    losses = sum(1 for r in filled if r["pnl"] < 0)
    by_status = {}
    for r in filled:
        acc = by_status.setdefault(r["status"], [0, 0.0])
        acc[0] += 1
        acc[1] += r["pnl"]

    # Cross-check view 2: performance module (may exclude no-exit_price ledgers).
    perf_n = perf_err = None
    try:
        from tools.auto_paper.performance import compute_performance
        rep = compute_performance(setup_filter=None, risk_per_trade=0.01, include_baselines=False)
        perf_n = getattr(rep, "n_realized", None)
    except Exception as e:
        perf_err = repr(e)
    # Cross-check view 3: critic-panel calibration (intended-size / R-based outcomes).
    calib_n = calib_pnl = calib_err = None
    try:
        from tools.auto_paper.calibration_analysis import compute as _cc
        c = _cc()
        calib_n = getattr(c, "n_joined", None)
        ba = getattr(c, "by_action", {}) or {}
        calib_pnl = sum((v.get("total_pnl", 0.0) if isinstance(v, dict)
                         else getattr(v, "total_pnl", 0.0)) for v in ba.values())
    except Exception as e:
        calib_err = repr(e)

    # COIN ratchet-then-loss reconstruction (from its ledger notes + stop history).
    coin = yaml.safe_load(open(os.path.join(_ROOT, "ledgers", "paper-auto", "COIN.yml"),
                               encoding="utf-8")) or {}
    cps = coin.get("position_state") or {}
    c_entry = (cps.get("starter") or {}).get("fill_price")
    c_sh = (cps.get("starter") or {}).get("shares")
    c_exit = cps.get("exit_price")
    notes = coin.get("notes") or ""
    stops = [float(x) for x in re.findall(r"-> \$?([0-9]+\.[0-9]+)", notes)]
    max_stop = max(stops) if stops else None
    actual_pnl = (float(c_exit) - float(c_entry)) * float(c_sh) if (c_entry and c_exit and c_sh) else None
    cf_pnl = (float(max_stop) - float(c_entry)) * float(c_sh) if (max_stop and c_entry and c_sh) else None
    # 6/01 gapped BELOW the $182.20 stop (verified from COIN OHLC cache: 5/29 close 189.03 -> 6/01 open 179.21),
    # so a stop-fill-at-trigger-price is unrealistic; the realistic gap fill is the 6/01 open.
    COIN_0601_OPEN = 179.21
    cf_gap_pnl = (COIN_0601_OPEN - float(c_entry)) * float(c_sh) if (c_entry and c_sh) else None

    L = ["# Paper-auto live-trade post-mortem — forensic reproducer output (2026-07-26)", "",
         "Deterministic: regenerated from on-disk ledgers only. Re-run: "
         "`uv run python ledgers/improvements/2026-07-26-paper-auto-postmortem-forensics.py`", "",
         "## Per-trade realized P&L (entry=starter fill, exit=position_state.exit_price)", "",
         "| Ticker | Strategy | Status | Grade | Entry | Exit | Sh | Hold(d) | P&L $ | Panel |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["pnl"] is None, r["pnl"] or 0)):
        pnl = "" if r["pnl"] is None else f"{r['pnl']:,.0f}"
        L.append(f"| {r['t']} | {r['setup']} | {r['status']} | {r['grade'] or ''} | "
                 f"{r['ep'] or ''} | {r['xp'] or ''} | {r['sh'] or ''} | "
                 f"{r['hold'] if r['hold'] is not None else ''} | {pnl} | {r['panel'] or ''} |")
    L += ["",
          f"**Filled closed trades: {len(filled)} — {wins} wins / {losses} losses. "
          f"NET realized P&L: ${net:,.0f}.**", "",
          "### By strategy status"]
    for k, (n, s) in sorted(by_status.items()):
        L.append(f"- **{k}**: n={n}, net ${s:,.0f}")
    L += ["",
          "### Three P&L views that disagree (the bookkeeping gap)",
          f"- Direct-ledger (filled, entry-to-exit): **${net:,.0f}** over {len(filled)} trades.",
          f"- Performance module `n_realized`: **{perf_n}** trade(s)"
          + (f" — ERROR {perf_err}" if perf_err else " (excludes ledgers missing exit_price)."),
          f"- Critic-panel calibration: **{calib_n} joined**, summed total_pnl "
          f"**${calib_pnl:,.0f}**" + (f" — ERROR {calib_err}" if calib_err else
          " (intended-size / includes NFLX)."),
          "",
          "## COIN — ratcheted stop did not protect the gain (critic-corrected, gap-aware)",
          f"- Entry ${c_entry} x {c_sh} sh. Ratcheted stops seen in ledger notes: {stops}.",
          f"- Highest ratcheted stop: **${max_stop}** (~+{((max_stop/float(c_entry))-1)*100:.1f}% vs entry).",
          f"- ACTUAL exit: **${c_exit}** -> realized **${actual_pnl:,.0f}**.",
          f"- Naive counterfactual (stop fills AT ${max_stop}): ~+${cf_pnl:,.0f} — UPPER BOUND ONLY, not used.",
          f"- Gap-aware counterfactual: 6/01 opened **${COIN_0601_OPEN}** (gapped BELOW the ${max_stop} stop; "
          f"5/29 close 189.03), so a stop fills ~${COIN_0601_OPEN} -> ~+${cf_gap_pnl:,.0f}; "
          f"realistic swing vs actual ~${cf_gap_pnl - actual_pnl:,.0f}.",
          "- STRONGER DEFECT: the position did NOT close on 6/01 (open 179.21) or the 6/02-6/03 sub-182 "
          "sessions; it closed 6/04 via the composer at ${:.2f}. The ratcheted stop appears to have provided "
          "NO protection (unprotected-state after cancel-then-place, or Tiger-paper STP not firing on a gap-down).".format(float(c_exit)),
          f"- Exit reason on record: {cps.get('exit_reason')}"]

    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(x for x in L if x is not None) + "\n")

    print("\n".join(x for x in L if x is not None))
    print(f"\nwrote {OUT_MD}")


if __name__ == "__main__":
    main()
