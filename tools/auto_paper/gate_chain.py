"""Deterministic pre-order gate chain (cherry-pick Batch B, B1).

Conviction never reaches the broker directly. The caller supplies a
:class:`GateCandidate` ({ticker, direction, stated_probability, proposed
size}) plus a :class:`BookSnapshot` of the current sleeve, and a chain of
five deterministic gates decides the final size or vetoes the order. Every
gate's result — pass, reduce, or fail — is appended to an append-only JSONL
log so each decision is individually auditable after the fact.

Gates, in order (sizing first so later gates see the adjusted size):

  1. ``sizing_bounds``   — fractional Kelly (<= ``kelly_fraction_cap`` x full
     Kelly) on the stated probability, clamped to the operator's size band.
     Stated-vs-realized calibration inputs are logged per candidate so the
     multiplier can be tuned from evidence, not vibes.
  2. ``liquidity``       — position cost <= ``max_adv_pct`` of 20-day dollar
     ADV (median Close x Volume, point-in-time). Reduces to the cap rather
     than vetoing, unless the cap leaves < 1 share.
  3. ``correlation``     — max pairwise return-correlation vs the open book
     <= threshold, and the candidate must not duplicate an already-open
     theme cluster.
  4. ``concentration``   — position-count / per-position / sector / cross-
     track theme-cluster caps (defaults mirror the pipeline's track limits).
  5. ``circuit_breaker`` — sleeve-level kill switch: drawdown from the
     sleeve high-water mark beyond ``breaker_dd_pct`` trips the breaker to
     no-new-entries until an operator reset (a logged manual action).

All five gates are pure functions of their inputs (the chain runner threads
the size adjustments through); disk/network reads live only in the
``build_snapshot`` adapter and the breaker-state persistence helpers, so the
gates are hermetically testable.

Every gate is evaluated even when an earlier one fails — the DoD is a full
per-candidate gate log, not a short-circuit — and the chain verdict is the
AND of all five.

Wiring note (2026-07-24): this chain is built to slot into
``pipeline.place_candidate`` alongside ``_check_track_limits``, but the
call-site edit is DEFERRED until the NFLX short-guard remediation (Step 5 of
the 2026-07-24 incident) completes and the cron gate clears. See
``ledgers/improvements/2026-07-24-live-validation-batch-b.md``.

Design port (pattern only, no code) from OctagonAI/kalshi-trading-bot-cli
(MIT); thresholds translated from prediction markets to equities per the
2026-07-24 cherry-pick spec.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[2]

# Append-only decision log, date-partitioned JSONL — mirrors the
# tools.cluster_calibration sink convention (injectable dir + env override).
GATE_LOG_DIR = _ROOT / "ledgers" / "paper-auto" / "_gates"
GATE_LOG_DIR_ENV = "GATE_CHAIN_LOG_DIR"

# Sleeve circuit-breaker state (high-water mark + tripped flag). Lives next
# to positions.json because it is paper-auto-track state, not analytics.
BREAKER_STATE_PATH = _ROOT / "journal" / "paper-auto" / "sleeve_breaker.json"
BREAKER_STATE_ENV = "GATE_CHAIN_BREAKER_PATH"

SCHEMA_VERSION = 1

GATE_NAMES = (
    "sizing_bounds",
    "liquidity",
    "correlation",
    "concentration",
    "circuit_breaker",
)


@dataclass
class GateConfig:
    """Chain thresholds. Defaults mirror the pipeline's paper-auto track
    limits (pipeline.py constants) — passed as config rather than imported to
    keep this module a leaf (no pipeline import edge, no broker import on
    test collection). The future pipeline wiring passes its own constants in.
    """
    # Gate 1 — sizing
    kelly_fraction_cap: float = 0.5      # <= 0.5x full Kelly per the spec
    assumed_payoff_ratio: float = 2.0    # b when no target present (2R default)
    band_min_pct: float = 0.0            # operator size band, fraction of net liq
    band_max_pct: float = 0.05           # paper-auto per-position cap
    # Gate 2 — liquidity
    max_adv_pct: float = 0.05            # spec start point: 5% of 20-day ADV
    adv_window: int = 20
    # Gate 3 — correlation / theme overlap
    corr_threshold: float = 0.75
    min_overlap_bars: int = 40
    reject_duplicate_theme: bool = True
    # Gate 4 — concentration (mirrors pipeline constants)
    max_positions: int = 8
    max_pct_per_position: float = 0.05
    max_pct_per_sector: float = 0.20
    max_pct_per_cluster: float = 0.30
    # Gate 5 — circuit breaker
    breaker_dd_pct: float = 0.20         # trip at -20% from sleeve high-water


@dataclass
class GateCandidate:
    """One candidate order as emitted by the conviction layer."""
    ticker: str
    direction: str                        # paper-auto track is long-only
    stated_probability: float             # P(win) as stated by the signal layer
    proposed_shares: int
    price: float                          # limit / decision price
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    sector_etf: Optional[str] = None
    theme: Optional[str] = None           # cluster name; None = untagged


@dataclass
class BookSnapshot:
    """Point-in-time inputs the gates need. Built by :func:`build_snapshot`
    for live use; constructed directly (synthetic) in tests."""
    account_net_liq: float
    cash: float
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    discretionary_positions: list[dict[str, Any]] = field(default_factory=list)
    regime_class: Optional[str] = None
    dollar_adv_20d: Optional[float] = None            # candidate's 20-day $ADV
    candidate_returns: Optional[Any] = None           # pd.Series of daily returns
    book_returns: dict[str, Any] = field(default_factory=dict)  # ticker -> pd.Series
    open_themes: dict[str, str] = field(default_factory=dict)   # ticker -> theme
    sleeve_equity: Optional[float] = None             # defaults to account_net_liq


@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str
    adjusted_shares: Optional[int] = None   # set when the gate changed the size
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChainResult:
    ticker: str
    passed: bool
    proposed_shares: int
    final_shares: int                       # 0 when vetoed
    results: list[GateResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "passed": self.passed,
            "proposed_shares": self.proposed_shares,
            "final_shares": self.final_shares,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Gate 1 — sizing bounds (fractional Kelly, clamped to the operator band)
# ---------------------------------------------------------------------------

def gate_sizing_bounds(
    cand: GateCandidate, snap: BookSnapshot, cfg: GateConfig
) -> GateResult:
    """Fractional Kelly on the stated probability.

    f* = p - (1-p)/b with b = (target-entry)/(entry-stop) (payoff ratio in R);
    applied fraction = ``kelly_fraction_cap`` x f*, clamped to the operator
    band [band_min_pct, band_max_pct] of net liq. The chain never sizes UP —
    the result is min(proposed, kelly-derived). Stated-vs-realized calibration
    inputs (stated_p, b, f*, applied fraction) ride in ``detail`` so the log
    accumulates the evidence needed to tune ``kelly_fraction_cap`` later.
    """
    if cand.direction.strip().lower() != "long":
        # 2026-07-24 lesson: nothing on this track may size a non-long. Ever.
        return GateResult(
            gate="sizing_bounds", passed=False,
            reason=f"direction {cand.direction!r} not supported — paper-auto track is long-only",
        )
    p = cand.stated_probability
    if not (0.0 < p < 1.0):
        return GateResult(
            gate="sizing_bounds", passed=False,
            reason=f"stated_probability {p} outside (0, 1)",
        )
    if cand.price <= 0:
        return GateResult(gate="sizing_bounds", passed=False, reason="price <= 0")
    if cand.stop_price is not None and cand.target_price is not None and cand.stop_price < cand.price:
        risk = cand.price - cand.stop_price
        reward = cand.target_price - cand.price
        b = (reward / risk) if risk > 0 and reward > 0 else cfg.assumed_payoff_ratio
    else:
        b = cfg.assumed_payoff_ratio
    kelly_f = p - (1.0 - p) / b
    detail = {
        "stated_probability": p,
        "payoff_ratio_b": round(b, 4),
        "kelly_f": round(kelly_f, 4),
        "kelly_fraction_cap": cfg.kelly_fraction_cap,
    }
    if kelly_f <= 0:
        return GateResult(
            gate="sizing_bounds", passed=False,
            reason=f"no edge at stated probability (kelly_f={kelly_f:.3f} <= 0)",
            detail=detail,
        )
    applied_f = min(max(cfg.kelly_fraction_cap * kelly_f, cfg.band_min_pct), cfg.band_max_pct)
    detail["applied_fraction"] = round(applied_f, 4)
    kelly_shares = math.floor(applied_f * snap.account_net_liq / cand.price)
    final = min(cand.proposed_shares, kelly_shares)
    detail["kelly_shares"] = kelly_shares
    if final < 1:
        return GateResult(
            gate="sizing_bounds", passed=False,
            reason=f"kelly-derived size {kelly_shares} shares < 1 at applied fraction {applied_f:.3%}",
            detail=detail,
        )
    if final < cand.proposed_shares:
        return GateResult(
            gate="sizing_bounds", passed=True,
            reason=f"reduced {cand.proposed_shares} -> {final} shares (0.5-Kelly clamp)",
            adjusted_shares=final, detail=detail,
        )
    return GateResult(
        gate="sizing_bounds", passed=True,
        reason=f"proposed {cand.proposed_shares} shares within {applied_f:.3%} Kelly bound",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Gate 2 — liquidity (position <= max_adv_pct of 20-day dollar ADV)
# ---------------------------------------------------------------------------

def gate_liquidity(
    cand: GateCandidate, snap: BookSnapshot, cfg: GateConfig, shares: int
) -> GateResult:
    adv = snap.dollar_adv_20d
    if adv is None or adv <= 0:
        # Unknown liquidity is penalised, never waved through (mirrors
        # security_master.liquidity_tier's None -> micro convention).
        return GateResult(
            gate="liquidity", passed=False,
            reason=f"20-day dollar ADV unknown for {cand.ticker} — cannot bound impact",
        )
    cap_usd = cfg.max_adv_pct * adv
    cost = shares * cand.price
    detail = {"dollar_adv_20d": round(adv, 2), "cap_usd": round(cap_usd, 2),
              "cost_usd": round(cost, 2), "max_adv_pct": cfg.max_adv_pct}
    if cost <= cap_usd:
        return GateResult(
            gate="liquidity", passed=True,
            reason=f"cost ${cost:,.0f} within {cfg.max_adv_pct:.0%} of ADV (${cap_usd:,.0f})",
            detail=detail,
        )
    reduced = math.floor(cap_usd / cand.price)
    detail["reduced_shares"] = reduced
    if reduced < 1:
        return GateResult(
            gate="liquidity", passed=False,
            reason=f"ADV cap ${cap_usd:,.0f} leaves < 1 share at ${cand.price:,.2f}",
            detail=detail,
        )
    return GateResult(
        gate="liquidity", passed=True,
        reason=f"reduced {shares} -> {reduced} shares to fit {cfg.max_adv_pct:.0%} of ADV",
        adjusted_shares=reduced, detail=detail,
    )


# ---------------------------------------------------------------------------
# Gate 3 — correlation / theme overlap vs the open book
# ---------------------------------------------------------------------------

def gate_correlation(
    cand: GateCandidate, snap: BookSnapshot, cfg: GateConfig
) -> GateResult:
    # Theme duplication first — cheaper, and independent of return data.
    if cfg.reject_duplicate_theme and cand.theme:
        dupes = sorted(
            t for t, theme in snap.open_themes.items() if theme == cand.theme
        )
        if dupes:
            return GateResult(
                gate="correlation", passed=False,
                reason=f"duplicates open theme {cand.theme!r} (held: {', '.join(dupes)})",
                detail={"theme": cand.theme, "duplicate_holdings": dupes},
            )
    if not snap.open_positions:
        return GateResult(
            gate="correlation", passed=True, reason="book is empty — nothing to correlate",
        )
    if snap.candidate_returns is None or len(snap.candidate_returns) < cfg.min_overlap_bars:
        return GateResult(
            gate="correlation", passed=False,
            reason=(
                f"candidate return history missing/short (<{cfg.min_overlap_bars} bars) — "
                "cannot bound correlation to the open book"
            ),
        )
    correlations: dict[str, float] = {}
    skipped: list[str] = []
    for pos in snap.open_positions:
        t = pos.get("ticker")
        if not t:
            continue
        other = snap.book_returns.get(t)
        if other is None:
            skipped.append(t)
            continue
        joined = snap.candidate_returns.align(other, join="inner")
        if len(joined[0]) < cfg.min_overlap_bars:
            skipped.append(t)
            continue
        correlations[t] = float(joined[0].corr(joined[1]))
    detail: dict[str, Any] = {
        "correlations": {k: round(v, 3) for k, v in correlations.items()},
        "skipped_no_data": skipped,
        "threshold": cfg.corr_threshold,
    }
    if correlations:
        worst_ticker = max(correlations, key=lambda k: correlations[k])
        worst = correlations[worst_ticker]
        if worst > cfg.corr_threshold:
            return GateResult(
                gate="correlation", passed=False,
                reason=f"corr({cand.ticker}, {worst_ticker}) = {worst:.2f} > {cfg.corr_threshold}",
                detail=detail,
            )
    return GateResult(
        gate="correlation", passed=True,
        reason=(
            f"max pairwise corr {max(correlations.values()):.2f} <= {cfg.corr_threshold}"
            if correlations else "no overlapping return history to correlate (pairs skipped)"
        ),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Gate 4 — concentration (count / per-position / sector / cluster)
# ---------------------------------------------------------------------------

def gate_concentration(
    cand: GateCandidate, snap: BookSnapshot, cfg: GateConfig, shares: int
) -> GateResult:
    from .. import cluster_concentration  # leaf-safe: no broker import

    cost = shares * cand.price
    detail: dict[str, Any] = {"cost_usd": round(cost, 2)}
    if len(snap.open_positions) >= cfg.max_positions:
        return GateResult(
            gate="concentration", passed=False,
            reason=f"position count limit hit ({len(snap.open_positions)} >= {cfg.max_positions})",
            detail=detail,
        )
    pct_position = cost / snap.account_net_liq
    detail["pct_position"] = round(pct_position, 4)
    if pct_position > cfg.max_pct_per_position:
        return GateResult(
            gate="concentration", passed=False,
            reason=f"position {pct_position:.2%} exceeds {cfg.max_pct_per_position:.0%} cap",
            detail=detail,
        )
    if cand.sector_etf:
        same_sector = sum(
            p.get("shares", 0) * p.get("entry_price", 0)
            for p in snap.open_positions
            if p.get("sector") == cand.sector_etf
        )
        sector_pct = (same_sector + cost) / snap.account_net_liq
        detail["sector_pct"] = round(sector_pct, 4)
        if sector_pct > cfg.max_pct_per_sector:
            return GateResult(
                gate="concentration", passed=False,
                reason=f"sector {cand.sector_etf} {sector_pct:.2%} exceeds {cfg.max_pct_per_sector:.0%} cap",
                detail=detail,
            )
    cluster = cluster_concentration.compute_from_books(
        ticker=cand.ticker,
        proposed_cost_usd=cost,
        account_value_usd=snap.account_net_liq,
        books=[snap.open_positions, snap.discretionary_positions],
        cap_pct=cfg.max_pct_per_cluster,
        regime_class=snap.regime_class,
        track="paper-auto",
    )
    detail["cluster"] = {
        k: cluster.output.get(k)
        for k in ("theme", "cluster_pct", "effective_cap_pct", "breach", "ceiling_breach")
    }
    if cluster.output["breach"]:
        return GateResult(
            gate="concentration", passed=False,
            reason=(
                f"theme cluster {cluster.output['theme']} would exceed "
                f"{cluster.output['effective_cap_pct']:.0%} cap "
                f"({cluster.output['cluster_pct']:.2%} cross-track)"
            ),
            detail=detail,
        )
    return GateResult(
        gate="concentration", passed=True,
        reason="within position/sector/cluster caps", detail=detail,
    )


# ---------------------------------------------------------------------------
# Gate 5 — sleeve drawdown circuit breaker (trip -> manual reset only)
# ---------------------------------------------------------------------------

def gate_circuit_breaker(
    snap: BookSnapshot, cfg: GateConfig, state: dict[str, Any]
) -> tuple[GateResult, dict[str, Any]]:
    """Pure given (snapshot, state): returns (result, new_state). The chain
    runner persists new_state; tests inspect it directly. A tripped breaker
    fails EVERY candidate until an operator reset (a logged manual action via
    the CLI) — the trip does not auto-clear on recovery, by design."""
    equity = snap.sleeve_equity if snap.sleeve_equity is not None else snap.account_net_liq
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_state = dict(state)
    if state.get("tripped"):
        return (
            GateResult(
                gate="circuit_breaker", passed=False,
                reason=(
                    f"breaker TRIPPED since {state.get('tripped_at')} "
                    f"(dd {state.get('tripped_dd_pct', 0):.1%}) — operator reset required"
                ),
                detail={"state": dict(state)},
            ),
            new_state,
        )
    hwm = float(state.get("high_water") or 0.0)
    if equity > hwm:
        new_state["high_water"] = equity
        new_state["high_water_at"] = now
        hwm = equity
    dd = 0.0 if hwm <= 0 else (hwm - equity) / hwm
    detail = {"sleeve_equity": equity, "high_water": hwm, "drawdown_pct": round(dd, 4),
              "breaker_dd_pct": cfg.breaker_dd_pct}
    if dd >= cfg.breaker_dd_pct:
        new_state.update(
            tripped=True, tripped_at=now, tripped_dd_pct=dd, tripped_equity=equity,
        )
        return (
            GateResult(
                gate="circuit_breaker", passed=False,
                reason=f"sleeve drawdown {dd:.1%} >= {cfg.breaker_dd_pct:.0%} — breaker TRIPPED",
                detail=detail,
            ),
            new_state,
        )
    return (
        GateResult(
            gate="circuit_breaker", passed=True,
            reason=f"drawdown {dd:.1%} within {cfg.breaker_dd_pct:.0%} tolerance",
            detail=detail,
        ),
        new_state,
    )


# ---------------------------------------------------------------------------
# Chain runner + decision log
# ---------------------------------------------------------------------------

def _resolve_log_dir(log_dir: Optional[Path]) -> Path:
    if log_dir is not None:
        return Path(log_dir)
    env = os.environ.get(GATE_LOG_DIR_ENV)
    if env:
        return Path(env)
    return GATE_LOG_DIR


def append_gate_log(
    chain: ChainResult,
    *,
    source: str,
    dry_run: bool = False,
    ledger_date: Optional[date] = None,
    log_dir: Optional[Path] = None,
) -> Path:
    """Append one line per gate result + one chain-summary line to
    ``ledgers/paper-auto/_gates/YYYY-MM-DD.jsonl``. Append-only."""
    if ledger_date is None:
        ledger_date = date.today()
    d = _resolve_log_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ledger_date.isoformat()}.jsonl"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as fh:
        for r in chain.results:
            fh.write(json.dumps({
                "v": SCHEMA_VERSION, "ts": ts, "kind": "gate", "source": source,
                "dry_run": dry_run, "ticker": chain.ticker, **r.to_dict(),
            }) + "\n")
        fh.write(json.dumps({
            "v": SCHEMA_VERSION, "ts": ts, "kind": "chain_summary", "source": source,
            "dry_run": dry_run, **chain.to_dict(),
        }) + "\n")
    return path


def run_chain(
    cand: GateCandidate,
    snap: BookSnapshot,
    cfg: Optional[GateConfig] = None,
    breaker_state: Optional[dict[str, Any]] = None,
) -> tuple[ChainResult, dict[str, Any]]:
    """Run all five gates (no short-circuit — the log carries every verdict),
    threading size adjustments through. Returns (ChainResult, new breaker
    state). final_shares is 0 unless every gate passed."""
    cfg = cfg or GateConfig()
    breaker_state = breaker_state if breaker_state is not None else {}
    results: list[GateResult] = []

    r1 = gate_sizing_bounds(cand, snap, cfg)
    results.append(r1)
    shares = r1.adjusted_shares if r1.adjusted_shares is not None else cand.proposed_shares

    r2 = gate_liquidity(cand, snap, cfg, shares)
    results.append(r2)
    if r2.adjusted_shares is not None:
        shares = r2.adjusted_shares

    results.append(gate_correlation(cand, snap, cfg))
    results.append(gate_concentration(cand, snap, cfg, shares))

    r5, new_state = gate_circuit_breaker(snap, cfg, breaker_state)
    results.append(r5)

    passed = all(r.passed for r in results)
    chain = ChainResult(
        ticker=cand.ticker,
        passed=passed,
        proposed_shares=cand.proposed_shares,
        final_shares=shares if passed else 0,
        results=results,
    )
    return chain, new_state


# ---------------------------------------------------------------------------
# Breaker state persistence + live snapshot adapter
# ---------------------------------------------------------------------------

def _breaker_path(path: Optional[Path]) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(BREAKER_STATE_ENV)
    if env:
        return Path(env)
    return BREAKER_STATE_PATH


def load_breaker_state(path: Optional[Path] = None) -> dict[str, Any]:
    p = _breaker_path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_breaker_state(state: dict[str, Any], path: Optional[Path] = None) -> Path:
    p = _breaker_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return p


def reset_breaker(
    *, operator: str, note: str, path: Optional[Path] = None,
    log_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Operator-only breaker reset. Refuses if not tripped. The reset itself
    is a logged manual action (one JSONL line, kind=breaker_reset)."""
    state = load_breaker_state(path)
    if not state.get("tripped"):
        raise RuntimeError("breaker is not tripped — nothing to reset")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_state = dict(state)
    new_state.update(
        tripped=False, reset_at=now, reset_by=operator, reset_note=note,
        # HWM re-bases to the tripped equity so a post-reset recovery does not
        # instantly re-trip against the stale pre-drawdown high.
        high_water=float(state.get("tripped_equity") or 0.0),
    )
    save_breaker_state(new_state, path)
    d = _resolve_log_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{date.today().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "v": SCHEMA_VERSION, "ts": now, "kind": "breaker_reset",
            "operator": operator, "note": note,
            "previous_tripped_at": state.get("tripped_at"),
        }) + "\n")
    return new_state


def build_snapshot(
    cand: GateCandidate,
    *,
    account_net_liq: float,
    cash: float,
    open_positions: list[dict[str, Any]],
    discretionary_positions: Optional[list[dict[str, Any]]] = None,
    regime_class: Optional[str] = None,
    asof: Optional[date] = None,
    returns_window: int = 60,
) -> BookSnapshot:
    """Live adapter: does the disk reads (data cache ADV + return histories,
    theme map) the pure gates must not do. Degrades field-by-field: a missing
    cache entry leaves that field None/absent and the corresponding gate
    fails conservative, loudly, in its own log row."""
    import pandas as pd  # noqa: F401 — align() path needs pandas present

    from .. import cluster_concentration
    from ..backtest import data_cache, security_master

    asof = asof or date.today()

    def _returns(ticker: str):
        try:
            df = data_cache.load(ticker)
        except Exception:
            return None
        if df is None or df.empty or "Close" not in df.columns:
            return None
        closes = df["Close"].astype(float)
        closes = closes[pd.to_datetime(closes.index).date <= asof]
        return closes.pct_change().dropna().tail(returns_window)

    theme_map = cluster_concentration.load_theme_map()
    themes: dict[str, str] = {}
    for p in open_positions:
        t = p.get("ticker")
        if not t:
            continue
        theme = cluster_concentration.theme_of(t, theme_map)
        if theme:
            themes[t] = theme
    if cand.theme is None:
        cand.theme = cluster_concentration.theme_of(cand.ticker, theme_map)

    return BookSnapshot(
        account_net_liq=account_net_liq,
        cash=cash,
        open_positions=open_positions,
        discretionary_positions=discretionary_positions or [],
        regime_class=regime_class,
        dollar_adv_20d=security_master.dollar_adv_from_cache(
            cand.ticker, asof, window=20,
        ),
        candidate_returns=_returns(cand.ticker),
        book_returns={
            t: r for t in themes.keys() | {p.get("ticker") for p in open_positions if p.get("ticker")}
            if (r := _returns(t)) is not None
        },
        open_themes=themes,
        sleeve_equity=account_net_liq,
    )


# ---------------------------------------------------------------------------
# CLI — status / reset / demo
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    state = load_breaker_state()
    print(json.dumps(state or {"tripped": False, "high_water": None}, indent=2))
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    try:
        state = reset_breaker(operator=args.operator, note=args.note)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(json.dumps(state, indent=2))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """Synthetic candidate through the full chain — the B1 DoD demo. Uses a
    synthetic book + returns so it runs hermetically (no network, no cache)."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2026-04-01", periods=80)
    base = pd.Series(rng.normal(0.0005, 0.02, size=80), index=idx)
    cand = GateCandidate(
        ticker="DEMO", direction="long", stated_probability=0.55,
        proposed_shares=400, price=50.0, stop_price=46.0, target_price=62.0,
        sector_etf="XLK", theme=None,
    )
    snap = BookSnapshot(
        account_net_liq=100_000.0, cash=60_000.0,
        open_positions=[
            {"ticker": "HELD1", "shares": 40, "entry_price": 100.0,
             "sector": "XLV", "stage": "starter"},
        ],
        dollar_adv_20d=25_000_000.0,
        candidate_returns=base,
        book_returns={"HELD1": base * 0.1 + pd.Series(rng.normal(0, 0.02, 80), index=idx)},
        open_themes={},
        sleeve_equity=100_000.0,
    )
    chain, new_state = run_chain(cand, snap, GateConfig(), breaker_state={})
    log_dir = Path(args.log_dir) if args.log_dir else None
    path = append_gate_log(chain, source="demo-cli", dry_run=True, log_dir=log_dir)
    print(f"log: {path}")
    print(json.dumps(chain.to_dict(), indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Pre-order gate chain (B1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print breaker state")
    rp = sub.add_parser("reset", help="operator breaker reset (logged manual action)")
    rp.add_argument("--operator", required=True)
    rp.add_argument("--note", required=True)
    dp = sub.add_parser("demo", help="synthetic candidate through the chain")
    dp.add_argument("--log-dir", default=None)
    args = ap.parse_args(argv)
    return {"status": _cmd_status, "reset": _cmd_reset, "demo": _cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
