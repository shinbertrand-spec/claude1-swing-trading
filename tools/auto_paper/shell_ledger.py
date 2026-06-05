"""Shell-ledger builder for quant-scanner candidates (Phase 2 prerequisite).

The trade-skeptic subagent reads a candidate ledger at
``ledgers/candidates/YYYY-MM-DD/<TICKER>.yml`` and constructs the
invalidation thesis. For quant-scanner picks, no such ledger exists —
the quant track explicitly bypasses the trade-researcher deep-dive.
This module builds a **schema-valid minimal candidate ledger** from the
:class:`tools.auto_paper.pipeline.CandidateInput` + screener output +
fresh deterministic-tool calls (regime_check, atr_compute), so the
skeptic can run normally.

Why this is necessary (rather than adapting trade-skeptic to a different
input shape):
* trade-skeptic's prompt is canonical — it appends bear trace_refs to the
  same ledger object the bull side wrote. Forking the input interface
  doubles the maintenance burden.
* The schema is the contract. A shell ledger that validates against
  ``ledgers/_schema/ledger.schema.json`` is indistinguishable from a
  trade-researcher-produced ledger for downstream consumers — Gate 6
  debate_synthesis, ledger_freshness_audit, trace_audit, etc.

The shell ledger is **honest about its origin** — `meta.created_by` reads
``auto_paper/shell_ledger`` so any downstream that wants to special-case
quant picks can detect them.

CLI::

    uv run python -m tools.auto_paper.shell_ledger \\
        --ticker VRT --setup xs_short_term_reversal --grade B \\
        --pivot 328.10 --stop 289.66 --sector XLK
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ..contract import TraceEntry

TOOL = "tools/auto_paper/shell_ledger.py"

_ROOT = Path(__file__).resolve().parents[2]
_CANDIDATES_DIR = _ROOT / "ledgers" / "candidates"


# ---------------------------------------------------------------------------
# Inputs (a minimal CandidateInput-equivalent, decoupled from pipeline so this
# module can be tested + invoked standalone)
# ---------------------------------------------------------------------------


@dataclass
class ShellLedgerInput:
    ticker: str
    setup_type: str
    setup_grade: Optional[str]
    pivot_price: float
    stop_price: float
    sector_etf: Optional[str]
    # Optional screener output. When provided, populates fundamentals +
    # any sector correction. When None, the shell ledger has empty
    # fundamentals (skeptic re-fetches anyway).
    screener_output: Optional[dict[str, Any]] = None
    # Strategy-discovery track for bias-audit slicing (Alfred Delta 6).
    # "ai_thematic" when the source deployable_setups.yml row carries
    # track: ai_thematic; otherwise None (omit field, treated as "generic"
    # by tools.bias_audit). Orthogonal to account_track (always paper-auto
    # for shell-ledger writes).
    track: Optional[str] = None
    # Optional pre-computed trace entries the caller has already produced.
    # Threaded into reasoning_trace as ids 1..N. Tools we run internally
    # extend the list.
    seed_trace: list[TraceEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _isoformat_date_str(d: date | str | None) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, str):
        return d
    return d.isoformat()


def _next_id(traces: list[TraceEntry]) -> int:
    """Next sequential id for the reasoning_trace. Ids start at 1."""
    return (max((t.id or 0) for t in traces) + 1) if traces else 1


def _run_regime(ticker: str, sector_etf: Optional[str]) -> tuple[Optional[TraceEntry], Optional[str]]:
    """Run regime_check.compute. Returns (entry, error_str)."""
    try:
        from ..regime_check import compute as regime_compute
        return regime_compute(candidate_ticker=ticker, sector_etf=sector_etf), None
    except Exception as exc:
        return None, str(exc)


def _run_atr(ticker: str) -> tuple[Optional[TraceEntry], Optional[str]]:
    """Run atr_compute.compute_from_ticker. Returns (entry, error_str)."""
    try:
        from ..atr_compute import compute_from_ticker as atr_from_ticker
        return atr_from_ticker(ticker), None
    except Exception as exc:
        return None, str(exc)


def _extract_screener_evidence(screener_output: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Pull fundamentals.next_earnings_date + companion fields from the
    screener result. Returns empty dict when screener_output is None or
    the earnings check failed.
    """
    if not screener_output:
        return {}
    checks = screener_output.get("checks", [])
    earnings = next(
        (c for c in checks if c.get("check") == "earnings_blackout"),
        None,
    )
    if not earnings:
        return {}
    ev = earnings.get("evidence", {}) or {}
    return {
        "next_earnings_date": ev.get("next_earnings_date"),
        "trading_days_to_earnings": ev.get("trading_days_to_earnings"),
    }


def build_quant_shell_ledger(
    inp: ShellLedgerInput,
    *,
    today: date | None = None,
    run_tools: bool = True,
) -> tuple[dict[str, Any], list[TraceEntry]]:
    """Compose a schema-valid candidate-ledger dict + reasoning_trace.

    Args:
        inp: ticker / setup / pivot / stop facts.
        today: optional date override (test seam).
        run_tools: when False, skip the live regime_check + atr_compute
            calls and produce a ledger with empty technical/regime sections.
            Used by tests that don't want network.

    Returns:
        (ledger_dict, trace_entries). The caller may write ledger_dict to
        YAML via :func:`write_shell_ledger`. trace_entries are already
        embedded in ledger_dict["reasoning_trace"]; they're returned
        separately for callers that want to keep working with them as
        Python objects.
    """
    if today is None:
        today = date.today()
    now = _utc_now()

    # Seed trace entries from the caller, renumber to 1..N.
    traces: list[TraceEntry] = []
    for seed in inp.seed_trace:
        seed.id = _next_id(traces)
        traces.append(seed)

    # Tool runs — regime + ATR
    regime_entry: Optional[TraceEntry] = None
    atr_entry: Optional[TraceEntry] = None
    tool_errors: list[str] = []
    if run_tools:
        regime_entry, err = _run_regime(inp.ticker, inp.sector_etf)
        if regime_entry is not None:
            regime_entry.id = _next_id(traces)
            traces.append(regime_entry)
        elif err:
            tool_errors.append(f"regime_check: {err}")

        atr_entry, err = _run_atr(inp.ticker)
        if atr_entry is not None:
            atr_entry.id = _next_id(traces)
            traces.append(atr_entry)
        elif err:
            tool_errors.append(f"atr_compute: {err}")

    # Compose ledger sections
    meta = {
        "schema_version": "1.0",
        "ticker": inp.ticker,
        "asof": now,
        "state": "candidate",
        "account_track": "paper-auto",
        "ledger_path": f"ledgers/candidates/{today.isoformat()}/{inp.ticker}.yml",
        "created_by": "auto_paper/shell_ledger",
        "created_at": now,
    }
    if inp.track:
        meta["track"] = inp.track

    # setup_classification — required fields per schema
    confluence: list[dict[str, Any]] = [
        {
            "criterion": (
                f"Quant signal: {inp.setup_type} rank top-K. "
                f"Backtest cleared deployment gate (Sharpe > 1.0, |MDD| < 25%, n >= 30)."
            ),
            "status": "PASS",
            "evidence": (
                f"Strategy={inp.setup_type}; grade={inp.setup_grade}; "
                "see tools/deployable_setups.yml"
            ),
            "trace_refs": [],
        }
    ]
    # If we have screener evidence (litigation/dilution clean), add a passing
    # confluence row so the skeptic sees screener cleared the strategy-blind
    # disqualifiers.
    if inp.screener_output and not inp.screener_output.get("blocked"):
        confluence.append({
            "criterion": "Strategy-blind disqualifiers cleared (litigation/dilution/earnings)",
            "status": "PASS",
            "evidence": "tools.auto_paper.screener.screen returned blocked=False",
            "trace_refs": [],
        })

    stop_dist = (inp.pivot_price - inp.stop_price) / inp.pivot_price if inp.pivot_price > 0 else None
    setup_classification = {
        "type": inp.setup_type,
        "grade": inp.setup_grade,
        "confluence_checklist": confluence,
        "pivot_price": inp.pivot_price,
        "stop_price": inp.stop_price,
        "stop_distance_pct": round(stop_dist, 6) if stop_dist is not None else None,
        "trace_refs": [t.id for t in traces if t.id is not None],
        "notes": (
            "Shell ledger built by tools.auto_paper.shell_ledger. The quant track "
            "bypasses the trade-researcher 14-Q deep-dive; this ledger exists so the "
            "trade-skeptic subagent has a schema-valid handle to read. Bull case = "
            "the strategy's backtest evidence (see tools/deployable_setups.yml). "
            "Skeptic appends bear trace_refs normally."
        ),
    }
    # Schema forbids extra keys; drop stop_distance_pct if None.
    if setup_classification["stop_distance_pct"] is None:
        del setup_classification["stop_distance_pct"]
    if setup_classification["grade"] is None:
        del setup_classification["grade"]

    ledger: dict[str, Any] = {
        "meta": meta,
        "setup_classification": setup_classification,
        "reasoning_trace": [t.to_dict() for t in traces],
    }

    # Optional: regime block (if tool ran)
    if regime_entry is not None:
        out = regime_entry.output
        broad = out.get("broad_market", {})
        sector = out.get("sector") or {}
        regime_block: dict[str, Any] = {
            "broad_market_ticker": broad.get("ticker"),
            "broad_market_trend_template_passes": broad.get("trend_template_passes"),
            "broad_market_stage_class": broad.get("stage_class"),
            "regime_multiplier": out.get("regime_multiplier"),
            "computed_at": now,
        }
        if sector:
            regime_block["sector_etf"] = sector.get("ticker")
            regime_block["sector_trend_template_passes"] = sector.get("trend_template_passes")
            regime_block["sector_qualifies_for_long"] = sector.get("qualifies_for_long")
        ledger["regime"] = regime_block

    # Optional: technical block (ATR + candidate trend template)
    if regime_entry is not None or atr_entry is not None:
        tech_block: dict[str, Any] = {"computed_at": now}
        if regime_entry is not None:
            cand = regime_entry.output.get("candidate", {})
            if cand.get("trend_template_passes") is not None:
                tech_block["trend_template_passes"] = cand["trend_template_passes"]
            if cand.get("stage") is not None:
                tech_block["stage"] = cand["stage"]
        if atr_entry is not None:
            atr_val = atr_entry.output.get("atr_14") or atr_entry.output.get("atr")
            if atr_val is not None:
                tech_block["atr_14"] = float(atr_val)
        if len(tech_block) > 1:  # more than just computed_at
            ledger["technical"] = tech_block

    # Optional: fundamentals (from screener earnings check)
    fund_evidence = _extract_screener_evidence(inp.screener_output)
    if fund_evidence.get("next_earnings_date"):
        ledger["fundamentals"] = {
            "next_earnings_date": fund_evidence["next_earnings_date"],
            "next_earnings_source": "tool:earnings_calendar.py (via screener)",
            "source": "tool:auto_paper/screener.py",
            "fetched_at": now,
        }

    # If we collected any tool errors, log them in meta as updated_by (so the
    # caller knows the shell ledger is partial). Schema allows updated_by.
    if tool_errors:
        ledger["meta"]["updated_by"] = (
            "auto_paper/shell_ledger (partial; errors: " + "; ".join(tool_errors) + ")"
        )
        ledger["meta"]["updated_at"] = now

    return ledger, traces


def write_shell_ledger(
    ledger_dict: dict[str, Any],
    *,
    ticker: str,
    ledger_date: date | None = None,
    candidates_dir: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Write the shell ledger to ``ledgers/candidates/YYYY-MM-DD/<TICKER>.yml``.

    Args:
        ledger_dict: output of :func:`build_quant_shell_ledger`.
        ticker: symbol.
        ledger_date: defaults to today.
        candidates_dir: override for tests. Defaults to project-root
            ``ledgers/candidates``.
        overwrite: when True, replace any existing file. Default False
            refuses to clobber — preserves a discretionary deep-dive if
            one was written for the same ticker earlier.

    Returns:
        Absolute path written.

    Raises:
        FileExistsError if a ledger already exists and overwrite=False.
    """
    if ledger_date is None:
        ledger_date = date.today()
    if candidates_dir is None:
        candidates_dir = _CANDIDATES_DIR
    day_dir = candidates_dir / ledger_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{ticker}.yml"
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"candidate ledger already exists at {path} — refusing to overwrite"
        )
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(ledger_dict, fh, sort_keys=False)
    return path


def write_bull_stub_report(
    *,
    inp: ShellLedgerInput,
    ledger_path: Path,
    ledger_date: date | None = None,
    candidates_dir: Path | None = None,
) -> Path:
    """Write a minimal bull-side Markdown report that the trade-skeptic
    can read alongside the shell ledger. The "bull case" for a quant pick
    is the strategy's backtest evidence — not a discretionary narrative.

    Returns the path written. Refuses to overwrite if exists.
    """
    if ledger_date is None:
        ledger_date = date.today()
    if candidates_dir is None:
        candidates_dir = _CANDIDATES_DIR
    day_dir = candidates_dir / ledger_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{inp.ticker}.md"
    if path.exists():
        # Don't clobber a real bull report; return the path so caller knows.
        return path
    stop_dist_pct = (
        (inp.pivot_price - inp.stop_price) / inp.pivot_price * 100
        if inp.pivot_price > 0 else 0.0
    )
    body = f"""**Ledger:** {ledger_path}
**Setup / Grade:** {inp.setup_type} / {inp.setup_grade or "(none)"}
**Bull case:** quantitative strategy signal — see backtest evidence in
`tools/deployable_setups.yml`.

---

### Bull thesis (quant track — no discretionary narrative)

This candidate was selected by the `{inp.setup_type}` strategy from the
`tools.auto_paper.quant_scanner` ranking output. The discretionary 14-Q
framework was intentionally bypassed per `/auto-paper` Step 3b ("the
strategy IS the thesis — the deployment gate already did the equivalent
of a 5-gate validation, mechanically, over thousands of historical
signals"). The bull case is statistical, not narrative.

### Strategy evidence (from rolling walk-forward backtest)

- Strategy: {inp.setup_type}
- Backtest deployment gate: Sharpe > 1.0, |MDD| < 25%, n >= 30, >= 50% rolling-window pass-rate
- See `tools/deployable_setups.yml` for the recorded metrics.

### Trade parameters

- Pivot price: ${inp.pivot_price:.2f}
- Stop price: ${inp.stop_price:.2f}
- Stop distance: {stop_dist_pct:.2f}%
- Sector (claimed): {inp.sector_etf or "(unknown)"}

### What the bear should look for

Per `wiki/notes/swing-cherrypick-h1-design-spec.md` § Bear scope:

- Strategy-blind disqualifiers the screener missed (litigation announced
  after-hours, undisclosed customer concentration, off-cycle dilution
  via SEC filing not yet on finviz).
- Setup-quality concerns the quant signal is blind to (above-avg volume on
  pullback = distribution character; gap-driven move that the ATR-based stop
  cannot survive; no support level below entry).
- Catalyst-vs-thesis-horizon mismatch (e.g. earnings 11-12 days out
  squeaks past the 10-day blackout but lands well inside the swing
  2-6 week hold window).
- Correlation with other paper-auto positions or human-track positions
  that could compound a loss.

### Sources

Strategy YAML: `tools/quant_strategies/{inp.setup_type}.yml`
Backtest gate: `tools/deployable_setups.yml`
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.auto_paper.shell_ledger",
        description="Build a shell candidate ledger for a quant-scanner pick.",
    )
    p.add_argument("--ticker", required=True)
    p.add_argument("--setup", required=True, dest="setup_type")
    p.add_argument("--grade", default=None)
    p.add_argument("--pivot", type=float, required=True, dest="pivot_price")
    p.add_argument("--stop", type=float, required=True, dest="stop_price")
    p.add_argument("--sector", default=None, dest="sector_etf")
    p.add_argument(
        "--track", default=None, choices=["generic", "ai_thematic"],
        help="Strategy-discovery track for bias-audit slicing (Alfred Delta 6). "
             "Omit for generic; set to ai_thematic for the AI-thematic track.",
    )
    p.add_argument(
        "--write", action="store_true",
        help="Write the ledger to ledgers/candidates/YYYY-MM-DD/<TICKER>.yml. "
             "Without this flag, just prints the JSON to stdout.",
    )
    p.add_argument(
        "--no-tools", action="store_true",
        help="Skip the regime_check + atr_compute live tool calls.",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="Force-overwrite an existing ledger at the target path.",
    )
    args = p.parse_args()

    inp = ShellLedgerInput(
        ticker=args.ticker,
        setup_type=args.setup_type,
        setup_grade=args.grade,
        pivot_price=args.pivot_price,
        stop_price=args.stop_price,
        sector_etf=args.sector_etf,
        track=args.track,
    )
    ledger, _ = build_quant_shell_ledger(inp, run_tools=not args.no_tools)
    if args.write:
        path = write_shell_ledger(
            ledger, ticker=args.ticker, overwrite=args.overwrite,
        )
        bull_path = write_bull_stub_report(inp=inp, ledger_path=path)
        print(f"Wrote ledger: {path}")
        print(f"Wrote bull stub: {bull_path}")
    else:
        print(json.dumps(ledger, indent=2, default=str))


if __name__ == "__main__":
    main()
