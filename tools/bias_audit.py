"""Phase 6 — Universe-side bias audit (the unaddressed Type 4 doctrine requirement).

Per ``swing-risk-compliance-doctrine`` and the [[llm-financial-hallucination]]
Type 4 finding: bias is *structural and persists*, not localised. The 5-gate
per-trade sequence catches arithmetic / reasoning / staleness failures but
**cannot see systematic skew across many trades** — that requires a separate
periodic audit.

This tool scans candidate ledgers over a date range and flags systematic
skew in sector + market-cap selection vs the swing-eligible universe
baseline. It does not audit setup classification, grade calibration, or
temporal drift — those are deferred axes.

CLI::

    uv run python -m tools.bias_audit --since 2026-01-01 --until 2026-05-31
    uv run python -m tools.bias_audit --days 30
    uv run python -m tools.bias_audit --baseline tools/data/universe_baseline.yml

Library::

    from tools.bias_audit import audit, compute_from_paths
    report = audit(candidates_root="ledgers/candidates", since=date(2026,1,1))
"""
from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/bias_audit.py"

# SPDR Select Sector ETF → GICS sector mapping. Used to derive a candidate's
# sector from ``regime.sector_etf`` (the only sector signal currently in the
# ledger schema).
SECTOR_ETF_MAP: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLB": "Materials",
}

# Market-cap buckets (USD). Cap floor was relaxed to liquidity-only
# (ADV > 500K shares) on 2026-05-26 — small + micro-cap names are now
# eligible in the universe, so the audit needs buckets for them.
MARKET_CAP_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("micro_cap", 0.0, 300_000_000.0),
    ("small_cap", 300_000_000.0, 2_000_000_000.0),
    ("mid_cap", 2_000_000_000.0, 10_000_000_000.0),
    ("large_cap", 10_000_000_000.0, 200_000_000_000.0),
    ("mega_cap", 200_000_000_000.0, None),
)

# Default baseline proportions if the caller doesn't supply a file. Rough
# Russell 3000 weights as of 2026-Q2 (the framework's eligible universe is
# now liquid-US wide, not S&P-only). Refresh quarterly when the baseline
# file is rebuilt.
DEFAULT_BASELINE: dict[str, dict[str, float]] = {
    "sector": {
        "Technology": 0.30,
        "Financials": 0.13,
        "Health Care": 0.11,
        "Consumer Discretionary": 0.10,
        "Communication Services": 0.09,
        "Industrials": 0.08,
        "Consumer Staples": 0.06,
        "Energy": 0.04,
        "Real Estate": 0.02,
        "Utilities": 0.025,
        "Materials": 0.025,
    },
    "market_cap": {
        "micro_cap": 0.01,
        "small_cap": 0.07,
        "mid_cap": 0.12,
        "large_cap": 0.25,
        "mega_cap": 0.55,
    },
}

DEFAULT_Z_THRESHOLD = 2.0
DEFAULT_MIN_SAMPLE = 30
DEFAULT_UNKNOWN_FLAG_PCT = 0.10


@dataclass
class CandidateBucket:
    """One candidate's bucket assignment."""

    ticker: str
    ledger_path: str
    fetched_date: date | None
    sector: str | None       # None = sector_etf missing or unmapped
    market_cap_bucket: str | None  # None = market_cap_usd missing
    market_cap_usd: float | None


@dataclass
class BucketStat:
    """Per-bucket distribution + z-score result."""

    bucket: str
    observed_count: int
    observed_pct: float
    baseline_pct: float
    z_score: float
    flagged: bool
    tickers: list[str] = field(default_factory=list)


@dataclass
class BiasAuditReport:
    """Aggregate audit result."""

    n_candidates: int
    date_range: tuple[str, str]
    sample_size_adequate: bool
    min_sample: int
    z_threshold: float
    sector_stats: list[BucketStat]
    market_cap_stats: list[BucketStat]
    flagged_buckets: list[dict]
    missing_data: dict[str, list[str]]
    notes: list[str]


def _parse_date(s: str | date | datetime) -> date:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    return date.fromisoformat(str(s))


def _load_yaml(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, yaml.YAMLError):
        return None


def _market_cap_bucket(usd: float | None) -> str | None:
    if usd is None or usd <= 0:
        return None
    for name, lo, hi in MARKET_CAP_BUCKETS:
        if usd >= lo and (hi is None or usd < hi):
            return name
    return None


def _extract_bucket(ledger: dict, ledger_path: str) -> CandidateBucket | None:
    """Extract sector + market-cap bucket from a ledger dict. Returns None
    if there's no usable identity (missing ticker)."""
    meta = ledger.get("meta") or {}
    ticker = meta.get("ticker")
    if not ticker:
        return None
    asof = meta.get("asof") or meta.get("created_at")
    fetched_date: date | None = None
    if asof:
        try:
            s = str(asof)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            fetched_date = datetime.fromisoformat(s).date()
        except (ValueError, TypeError):
            fetched_date = None
    regime = ledger.get("regime") or {}
    sector_etf = regime.get("sector_etf")
    sector = SECTOR_ETF_MAP.get(sector_etf) if sector_etf else None
    fundamentals = ledger.get("fundamentals") or {}
    market_cap_usd = fundamentals.get("market_cap_usd")
    try:
        market_cap_usd = float(market_cap_usd) if market_cap_usd is not None else None
    except (TypeError, ValueError):
        market_cap_usd = None
    return CandidateBucket(
        ticker=str(ticker).upper(),
        ledger_path=ledger_path,
        fetched_date=fetched_date,
        sector=sector,
        market_cap_bucket=_market_cap_bucket(market_cap_usd),
        market_cap_usd=market_cap_usd,
    )


def _walk_candidate_ledgers(
    root: Path, since: date | None = None, until: date | None = None
) -> list[CandidateBucket]:
    """Walk ``root/<YYYY-MM-DD>/<TICKER>.yml``; return bucketed candidates
    whose ledger asof date falls in [since, until]."""
    out: list[CandidateBucket] = []
    if not root.exists():
        return out
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        # Date dirs are YYYY-MM-DD; quick filter before reading the files.
        try:
            dir_date = date.fromisoformat(date_dir.name)
        except ValueError:
            continue
        if since is not None and dir_date < since:
            continue
        if until is not None and dir_date > until:
            continue
        for f in sorted(date_dir.glob("*.yml")):
            ledger = _load_yaml(f)
            if ledger is None:
                continue
            bucket = _extract_bucket(ledger, str(f))
            if bucket is None:
                continue
            # If the ledger's own asof is outside the range but the dir-name
            # suggested otherwise, trust the dir-name (the audit window is
            # research-dir-based).
            out.append(bucket)
    return out


def _z_score(observed_pct: float, baseline_pct: float, n: int) -> float:
    """Two-sided z-score for observed_pct vs baseline_pct under a normal
    approximation of the binomial. Returns 0 if n<1 or baseline_pct in {0,1}."""
    if n < 1:
        return 0.0
    p0 = baseline_pct
    if p0 <= 0.0 or p0 >= 1.0:
        return 0.0
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se == 0.0:
        return 0.0
    return (observed_pct - p0) / se


def _bucket_stats(
    bucket_counts: dict[str, int],
    bucket_tickers: dict[str, list[str]],
    baseline: dict[str, float],
    n: int,
    z_threshold: float,
) -> list[BucketStat]:
    """Build per-bucket stats including buckets that appear in baseline but
    not observed (so under-representation surfaces)."""
    stats: list[BucketStat] = []
    seen = set(bucket_counts) | set(baseline)
    for bucket in sorted(seen):
        observed = bucket_counts.get(bucket, 0)
        pct = observed / n if n else 0.0
        b_pct = baseline.get(bucket, 0.0)
        z = _z_score(pct, b_pct, n)
        stats.append(
            BucketStat(
                bucket=bucket,
                observed_count=observed,
                observed_pct=round(pct, 4),
                baseline_pct=round(b_pct, 4),
                z_score=round(z, 3),
                flagged=abs(z) >= z_threshold,
                tickers=sorted(bucket_tickers.get(bucket, [])),
            )
        )
    return stats


def audit(
    candidates_root: str | Path,
    since: date | None = None,
    until: date | None = None,
    baseline: dict[str, dict[str, float]] | None = None,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    unknown_flag_pct: float = DEFAULT_UNKNOWN_FLAG_PCT,
) -> BiasAuditReport:
    """Run the bias audit over candidates in [since, until].

    Args:
        candidates_root: directory holding ``YYYY-MM-DD/<TICKER>.yml`` dirs.
        since / until: date filters (inclusive). If None, no bound on that side.
        baseline: per-axis expected proportions. Defaults to S&P 500
            weights from :data:`DEFAULT_BASELINE`.
        z_threshold: |z| at or above which a bucket is flagged.
        min_sample: minimum N below which findings carry a low-confidence note.
        unknown_flag_pct: portion of candidates missing sector / market-cap
            data above which a separate data-quality flag fires.
    """
    baseline = baseline or DEFAULT_BASELINE
    root = Path(candidates_root)
    candidates = _walk_candidate_ledgers(root, since=since, until=until)
    n = len(candidates)
    notes: list[str] = []

    # Date range — actual span (may be tighter than the requested filter).
    if candidates:
        dates_seen = [c.fetched_date for c in candidates if c.fetched_date]
        if dates_seen:
            actual_since = min(dates_seen).isoformat()
            actual_until = max(dates_seen).isoformat()
        else:
            actual_since = since.isoformat() if since else "unknown"
            actual_until = until.isoformat() if until else "unknown"
    else:
        actual_since = since.isoformat() if since else "unknown"
        actual_until = until.isoformat() if until else "unknown"

    sample_ok = n >= min_sample
    if not sample_ok:
        notes.append(
            f"sample size n={n} is below min_sample={min_sample}; bias "
            "findings carry low confidence and should not drive workflow "
            "changes on their own"
        )

    # Sector axis — only count candidates with known sector.
    sector_counts: dict[str, int] = {}
    sector_tickers: dict[str, list[str]] = {}
    no_sector: list[str] = []
    for c in candidates:
        if c.sector is None:
            no_sector.append(c.ticker)
            continue
        sector_counts[c.sector] = sector_counts.get(c.sector, 0) + 1
        sector_tickers.setdefault(c.sector, []).append(c.ticker)
    sector_n = sum(sector_counts.values())
    sector_stats = _bucket_stats(
        sector_counts, sector_tickers, baseline.get("sector", {}),
        sector_n, z_threshold,
    )

    # Market-cap axis — only candidates with usable market_cap.
    cap_counts: dict[str, int] = {}
    cap_tickers: dict[str, list[str]] = {}
    no_cap: list[str] = []
    for c in candidates:
        if c.market_cap_bucket is None:
            no_cap.append(c.ticker)
            continue
        cap_counts[c.market_cap_bucket] = cap_counts.get(c.market_cap_bucket, 0) + 1
        cap_tickers.setdefault(c.market_cap_bucket, []).append(c.ticker)
    cap_n = sum(cap_counts.values())
    cap_stats = _bucket_stats(
        cap_counts, cap_tickers, baseline.get("market_cap", {}),
        cap_n, z_threshold,
    )

    # Data-quality flags.
    if n > 0:
        if len(no_sector) / n > unknown_flag_pct:
            notes.append(
                f"{len(no_sector)}/{n} candidates have no usable sector_etf "
                "in regime — fix trade-researcher or expand SECTOR_ETF_MAP"
            )
        if len(no_cap) / n > unknown_flag_pct:
            notes.append(
                f"{len(no_cap)}/{n} candidates have no usable market_cap_usd "
                "in fundamentals — fix trade-researcher"
            )

    # Flagged buckets across both axes.
    flagged: list[dict] = []
    for s in sector_stats:
        if s.flagged:
            flagged.append({
                "axis": "sector",
                "bucket": s.bucket,
                "z_score": s.z_score,
                "observed_pct": s.observed_pct,
                "baseline_pct": s.baseline_pct,
                "direction": "over" if s.z_score > 0 else "under",
                "tickers": s.tickers,
            })
    for s in cap_stats:
        if s.flagged:
            flagged.append({
                "axis": "market_cap",
                "bucket": s.bucket,
                "z_score": s.z_score,
                "observed_pct": s.observed_pct,
                "baseline_pct": s.baseline_pct,
                "direction": "over" if s.z_score > 0 else "under",
                "tickers": s.tickers,
            })

    return BiasAuditReport(
        n_candidates=n,
        date_range=(actual_since, actual_until),
        sample_size_adequate=sample_ok,
        min_sample=min_sample,
        z_threshold=z_threshold,
        sector_stats=sector_stats,
        market_cap_stats=cap_stats,
        flagged_buckets=flagged,
        missing_data={"no_sector": no_sector, "no_market_cap": no_cap},
        notes=notes,
    )


def _report_to_dict(report: BiasAuditReport) -> dict[str, Any]:
    return {
        "n_candidates": report.n_candidates,
        "date_range": list(report.date_range),
        "sample_size_adequate": report.sample_size_adequate,
        "min_sample": report.min_sample,
        "z_threshold": report.z_threshold,
        "sector_distribution": [asdict(s) for s in report.sector_stats],
        "market_cap_distribution": [asdict(s) for s in report.market_cap_stats],
        "flagged_buckets": report.flagged_buckets,
        "missing_data": report.missing_data,
        "notes": report.notes,
    }


def compute_from_paths(
    candidates_root: str | Path,
    since: date | None = None,
    until: date | None = None,
    baseline_path: str | Path | None = None,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> TraceEntry:
    baseline = DEFAULT_BASELINE
    if baseline_path:
        loaded = _load_yaml(Path(baseline_path))
        if loaded:
            baseline = loaded
    report = audit(
        candidates_root,
        since=since,
        until=until,
        baseline=baseline,
        z_threshold=z_threshold,
        min_sample=min_sample,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={
            "candidates_root": str(candidates_root),
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "baseline_path": str(baseline_path) if baseline_path else "<default>",
            "z_threshold": z_threshold,
            "min_sample": min_sample,
        },
        output=_report_to_dict(report),
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.bias_audit",
        description=(
            "Audit candidate ledgers for universe-side discovery bias "
            "(sector + market-cap skew vs S&P 500 baseline)."
        ),
    )
    p.add_argument(
        "--candidates-root",
        default="ledgers/candidates",
        help="Directory holding YYYY-MM-DD/<TICKER>.yml dirs.",
    )
    p.add_argument("--since", default=None, help="ISO date lower bound (inclusive).")
    p.add_argument("--until", default=None, help="ISO date upper bound (inclusive).")
    p.add_argument(
        "--days",
        type=int,
        default=None,
        help="Convenience: audit the past N days. Overrides --since/--until.",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Path to baseline YAML/JSON. Default = built-in S&P 500 weights.",
    )
    p.add_argument(
        "--z-threshold",
        type=float,
        default=DEFAULT_Z_THRESHOLD,
        help="|z|-score at or above which a bucket is flagged.",
    )
    p.add_argument(
        "--min-sample",
        type=int,
        default=DEFAULT_MIN_SAMPLE,
        help="Minimum N below which findings carry a low-confidence note.",
    )
    p.add_argument(
        "--format",
        choices=("trace", "markdown"),
        default="trace",
        help="Output format. 'trace' = JSON TraceEntry; 'markdown' = report.",
    )
    args = p.parse_args()

    since: date | None = None
    until: date | None = None
    if args.days is not None:
        until = date.today()
        since = until - timedelta(days=args.days)
    else:
        if args.since:
            since = _parse_date(args.since)
        if args.until:
            until = _parse_date(args.until)

    entry = compute_from_paths(
        args.candidates_root,
        since=since,
        until=until,
        baseline_path=args.baseline,
        z_threshold=args.z_threshold,
        min_sample=args.min_sample,
    )
    if args.format == "trace":
        emit(entry)
    else:
        print(_render_markdown(entry))


def _render_markdown(entry: TraceEntry) -> str:
    """Render a TraceEntry to a Markdown bias-audit report. Used by the
    /bias-audit slash command output and journal entries."""
    out = entry.output
    lines: list[str] = []
    lines.append("# Bias audit report")
    lines.append("")
    lines.append(f"- Date range: **{out['date_range'][0]} -> {out['date_range'][1]}**")
    lines.append(f"- Candidates audited: **{out['n_candidates']}**")
    lines.append(f"- Sample size adequate (>={out['min_sample']}): **{out['sample_size_adequate']}**")
    lines.append(f"- z-score threshold for flagging: |z| >= {out['z_threshold']}")
    lines.append("")
    if out["notes"]:
        lines.append("## Notes")
        for n in out["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    if out["flagged_buckets"]:
        lines.append("## Flagged buckets")
        lines.append("")
        lines.append("| Axis | Bucket | Direction | Observed | Baseline | z | Tickers |")
        lines.append("|---|---|---|---|---|---|---|")
        for f in out["flagged_buckets"]:
            tickers = ", ".join(f["tickers"][:8]) + (f" (+{len(f['tickers']) - 8})" if len(f["tickers"]) > 8 else "")
            lines.append(
                f"| {f['axis']} | {f['bucket']} | {f['direction']} | "
                f"{f['observed_pct']:.1%} | {f['baseline_pct']:.1%} | "
                f"{f['z_score']:+.2f} | {tickers} |"
            )
        lines.append("")
    else:
        lines.append("## No flagged buckets")
        lines.append("")
        lines.append("All sector and market-cap distributions are within "
                     f"|z| < {out['z_threshold']} of the baseline.")
        lines.append("")
        lines.append("")

    lines.append("## Sector distribution")
    lines.append("")
    lines.append("| Sector | Observed | Baseline | z | Tickers |")
    lines.append("|---|---|---|---|---|")
    for s in out["sector_distribution"]:
        tickers = ", ".join(s["tickers"][:6]) + (f" (+{len(s['tickers']) - 6})" if len(s["tickers"]) > 6 else "")
        flag = " **[FLAGGED]**" if s["flagged"] else ""
        lines.append(
            f"| {s['bucket']}{flag} | {s['observed_pct']:.1%} ({s['observed_count']}) "
            f"| {s['baseline_pct']:.1%} | {s['z_score']:+.2f} | {tickers} |"
        )
    lines.append("")

    lines.append("## Market-cap distribution")
    lines.append("")
    lines.append("| Bucket | Observed | Baseline | z | Tickers |")
    lines.append("|---|---|---|---|---|")
    for s in out["market_cap_distribution"]:
        tickers = ", ".join(s["tickers"][:6]) + (f" (+{len(s['tickers']) - 6})" if len(s["tickers"]) > 6 else "")
        flag = " **[FLAGGED]**" if s["flagged"] else ""
        lines.append(
            f"| {s['bucket']}{flag} | {s['observed_pct']:.1%} ({s['observed_count']}) "
            f"| {s['baseline_pct']:.1%} | {s['z_score']:+.2f} | {tickers} |"
        )
    lines.append("")

    md = out.get("missing_data") or {}
    if md.get("no_sector") or md.get("no_market_cap"):
        lines.append("## Data-quality issues")
        if md.get("no_sector"):
            lines.append(f"- **Missing sector** ({len(md['no_sector'])}): "
                         + ", ".join(md["no_sector"]))
        if md.get("no_market_cap"):
            lines.append(f"- **Missing market_cap** ({len(md['no_market_cap'])}): "
                         + ", ".join(md["no_market_cap"]))
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
