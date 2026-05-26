"""One-shot /auto-paper --dry-run preview.

Mirrors what the cron-fired /auto-paper would do at 21:50 SGT (9:50 AM ET),
but: no broker calls, no ledger writes, no positions.json mutation.

Steps:
  1. TigerClient() → account_summary (net_liq, cash)
  2. classify_broad(SPY) → regime stage + multiplier
  3. scan_today() over all deployable KIND_REGISTRY setups
  4. For each candidate from each ScannerReport, call place_candidate(dry_run=True)
  5. Print a per-candidate outcome table + post-run track-state summary

No morning-scan candidates file exists yet (cron hasn't fired) — so the
SETUP_REPLAY_REGISTRY family is empty by definition. Everything here comes
from the quant scanner.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.broker.tiger import TigerClient  # noqa: E402
from tools.auto_paper.pipeline import place_candidate  # noqa: E402
from tools.auto_paper.quant_scanner import scan_today  # noqa: E402
from tools.regime_check import classify_broad  # noqa: E402
from tools.trend_template import compute_from_ticker as tt_from_ticker  # noqa: E402


def main() -> None:
    print("# /auto-paper --dry-run preview\n")
    print(f"# Asof: {datetime.now().isoformat()}")
    print()

    # 1. Tiger account state
    print("[1/4] Pulling Tiger paper account summary...", flush=True)
    c = TigerClient()
    summary = c.account_summary().output
    net_liq = float(summary.get("net_liquidation") or 0.0)
    cash = float(summary.get("cash") or 0.0)
    avail = summary.get("available_funds", "n/a")
    print(f"  net_liquidation: ${net_liq:,.2f}")
    print(f"  cash:            ${cash:,.2f}")
    print(f"  available_funds: {avail}")
    print()

    # 2. Regime
    print("[2/4] Computing SPY broad-market regime...", flush=True)
    passes_7 = tt_from_ticker("SPY", include_rs=False).output["trend_template_passes"]
    regime_class, regime_mult = classify_broad(passes_7)
    print(f"  trend_template_passes: {passes_7}/7")
    print(f"  stage_class: {regime_class}")
    print(f"  size multiplier: {regime_mult}×")
    if regime_mult <= 0.0:
        print(f"\n  ⚠️ regime_mult is 0 — pipeline would HALT all entries.")
    print()

    # 3. Quant scanner over all deployable rows
    print("[3/4] Running quant scanner over deployable KIND_REGISTRY setups...", flush=True)
    cash_for_sizer = cash if cash > 0 else None
    reports = scan_today(
        account_net_liq=net_liq,
        regime_class=regime_class,
        cash_available=cash_for_sizer,
    )
    print(f"  {len(reports)} scanner reports returned")
    for r in reports:
        print(f"    {r.setup}: {len(r.eligible_tickers)} eligible · "
              f"{len(r.candidates)} sized candidates · signal_date={r.signal_date}")
        if r.note:
            print(f"      note: {r.note}")
    print()

    # 4. Dry-run each candidate through the pipeline
    print("[4/4] Dry-run placement for each candidate...", flush=True)
    print()
    print("| # | Setup | Ticker | Shares | Limit | Stop | Cost | Status | Detail |")
    print("|---|---|---|---|---|---|---|---|---|")
    n = 0
    total_cost = 0.0
    placements_by_setup: dict[str, int] = {}
    rejections: list[tuple[str, str, str]] = []
    for r in reports:
        for cand in r.candidates:
            n += 1
            result = place_candidate(cand, client=c, dry_run=True)
            short_setup = r.setup.replace("_liquid_us", "_li").replace("_short_term_", "_st_")
            status_emoji = {"dry_run": "✅", "rejected": "❌", "error": "💥"}.get(result.status, "?")
            cost = (cand.shares * cand.limit_price) if result.status == "dry_run" else 0.0
            detail = ""
            if result.status == "dry_run":
                total_cost += cost
                placements_by_setup[r.setup] = placements_by_setup.get(r.setup, 0) + 1
                detail = f"would place"
            else:
                detail = (result.reason or "")[:60]
                rejections.append((r.setup, cand.ticker, result.reason or ""))
            print(f"| {n} | {short_setup} | {cand.ticker} | {cand.shares} | "
                  f"${cand.limit_price:.2f} | ${cand.stop_price:.2f} | ${cost:,.0f} | "
                  f"{status_emoji} {result.status} | {detail} |")

    print()
    print("## Summary")
    print(f"- Total candidates: **{n}**")
    n_placed = sum(placements_by_setup.values())
    print(f"- Would-be-placed: **{n_placed}**")
    print(f"- Total cost basis (dry-run): **${total_cost:,.2f}** "
          f"({(total_cost/net_liq*100) if net_liq>0 else 0:.1f}% of net liq)")
    print(f"- Cash after: **${cash - total_cost:,.2f}** "
          f"({((cash - total_cost)/net_liq*100) if net_liq>0 else 0:.1f}% of net liq)")
    print()
    print("### Placements per setup")
    for s, k in sorted(placements_by_setup.items(), key=lambda kv: -kv[1]):
        print(f"- {s}: {k}")

    if rejections:
        print()
        print(f"### Rejections ({len(rejections)})")
        # Aggregate by reason root
        by_reason: dict[str, int] = {}
        sample: dict[str, tuple[str, str]] = {}
        for setup, ticker, reason in rejections:
            key = reason.split(" — ")[0].split(" (")[0][:60] if reason else "(no reason)"
            by_reason[key] = by_reason.get(key, 0) + 1
            sample.setdefault(key, (setup, ticker))
        for r_reason, k in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            setup, ticker = sample[r_reason]
            print(f"- **{k}×** `{r_reason}` (e.g. {setup}/{ticker})")

    print()
    print("## Track state after this dry-run")
    print(f"- Paper-auto positions: {n_placed} / 8 (cap)")
    cb_pct = ((cash - total_cost) / net_liq * 100) if net_liq > 0 else 0
    print(f"- Cash buffer: {cb_pct:.1f}% (min 15%)")
    print(f"- Track ledgers existed before run: 0")
    print()
    print("(Nothing written to disk; no broker calls. Run /auto-paper "
          "without --dry-run to actually place.)")


if __name__ == "__main__":
    main()
