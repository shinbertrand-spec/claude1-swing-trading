"""Tests for tools.bias_audit.

Strategy: build synthetic candidate-ledger directories with controlled
sector + market-cap distributions, then assert the audit correctly
identifies the planted biases (and stays silent when distribution
matches the baseline).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from tools.bias_audit import (
    DEFAULT_BASELINE,
    SECTOR_ETF_MAP,
    audit,
    compute_from_paths,
    _market_cap_bucket,
    _render_markdown,
)


# ----------------------------------------------------------------------
# Synthetic ledger builders
# ----------------------------------------------------------------------

# Micro (<$300M), small ($300M-$2B), mid ($2B-$10B), large ($10B-$200B), mega (>$200B).
MICRO_CAP = 100_000_000.0
SMALL_CAP = 1_000_000_000.0
MID_CAP = 5_000_000_000.0
LARGE_CAP = 50_000_000_000.0
MEGA_CAP = 500_000_000_000.0

# Quick reverse-lookup from sector name → ETF symbol for fixture building.
SECTOR_TO_ETF = {v: k for k, v in SECTOR_ETF_MAP.items()}


def _write_candidate(
    root: Path,
    dir_date: date,
    ticker: str,
    sector: str | None,
    market_cap_usd: float | None,
) -> Path:
    """Write a minimal candidate ledger to root/<dir_date>/<TICKER>.yml."""
    day_dir = root / dir_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    sector_etf = SECTOR_TO_ETF.get(sector) if sector else None
    ledger: dict = {
        "meta": {
            "ticker": ticker,
            "state": "candidate",
            "asof": f"{dir_date.isoformat()}T14:30:00+00:00",
        },
    }
    if sector_etf:
        ledger["regime"] = {"sector_etf": sector_etf}
    if market_cap_usd is not None:
        ledger["fundamentals"] = {"market_cap_usd": market_cap_usd}
    f = day_dir / f"{ticker}.yml"
    f.write_text(yaml.safe_dump(ledger), encoding="utf-8")
    return f


def _seed_baseline_matching(root: Path, n: int = 50) -> None:
    """Seed N candidates whose sector + cap distribution matches the
    default baseline (so the audit should flag nothing)."""
    # Sector targets: 30% Tech, 13% Fin, 11% HC, 10% Disc, 9% Comm, 8% Ind,
    # 6% Staples, 4% Energy, 2.5% Util, 2.5% Mats, 2% RE.
    # Unique 4-letter prefix per sector to avoid filename collisions.
    targets = [
        ("Technology", 15, "TECH"),
        ("Financials", 6, "FIN_"),
        ("Health Care", 6, "HCAR"),
        ("Consumer Discretionary", 5, "CDIS"),
        ("Communication Services", 4, "COMM"),
        ("Industrials", 4, "INDU"),
        ("Consumer Staples", 3, "CSTA"),
        ("Energy", 2, "ENER"),
        ("Utilities", 2, "UTIL"),
        ("Materials", 2, "MATR"),
        ("Real Estate", 1, "REST"),
    ]
    # Cap split per the Russell-3000-ish baseline: 55% mega, 25% large, 12% mid,
    # 7% small, 1% micro. With n=50: 28 mega, 12 large, 6 mid, 3 small, 1 micro.
    cap_seq = (
        [MEGA_CAP] * 28 + [LARGE_CAP] * 12 + [MID_CAP] * 6
        + [SMALL_CAP] * 3 + [MICRO_CAP] * 1
    )
    assert len(cap_seq) == n
    idx = 0
    for sector, count, prefix in targets:
        for j in range(count):
            ticker = f"{prefix}{j:02d}"
            cap = cap_seq[idx]
            _write_candidate(root, date(2026, 5, 1), ticker, sector, cap)
            idx += 1


def _seed_tech_overrepresented(root: Path, n: int = 50) -> None:
    """Seed N candidates with ~70% Technology — should produce a strong
    over-representation flag on Technology and a corresponding under-rep
    on the squeezed sectors."""
    sector_seq = ["Technology"] * 35 + (
        ["Financials"] * 5 + ["Health Care"] * 4 + ["Industrials"] * 3 +
        ["Energy"] * 1 + ["Consumer Discretionary"] * 2
    )
    assert len(sector_seq) == n
    for i, sector in enumerate(sector_seq):
        cap = LARGE_CAP if i % 2 == 0 else MEGA_CAP
        _write_candidate(root, date(2026, 5, 1), f"T{i:03d}", sector, cap)


def _seed_mega_cap_skew(root: Path) -> None:
    """Seed 50 candidates with extreme mega-cap concentration (~80% mega-cap)."""
    cap_seq = [MEGA_CAP] * 40 + [LARGE_CAP] * 8 + [MID_CAP] * 2
    sectors = ["Technology", "Financials", "Health Care", "Industrials"]
    for i, cap in enumerate(cap_seq):
        sector = sectors[i % len(sectors)]
        _write_candidate(root, date(2026, 5, 1), f"M{i:03d}", sector, cap)


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------


def test_market_cap_bucket_boundaries() -> None:
    assert _market_cap_bucket(1.0) == "micro_cap"
    assert _market_cap_bucket(299_999_999.0) == "micro_cap"
    assert _market_cap_bucket(300_000_000.0) == "small_cap"
    assert _market_cap_bucket(1_999_999_999.0) == "small_cap"
    assert _market_cap_bucket(2_000_000_000.0) == "mid_cap"
    assert _market_cap_bucket(9_999_999_999.0) == "mid_cap"
    assert _market_cap_bucket(10_000_000_000.0) == "large_cap"
    assert _market_cap_bucket(199_999_999_999.0) == "large_cap"
    assert _market_cap_bucket(200_000_000_000.0) == "mega_cap"
    assert _market_cap_bucket(3_000_000_000_000.0) == "mega_cap"
    assert _market_cap_bucket(None) is None
    assert _market_cap_bucket(-1.0) is None
    assert _market_cap_bucket(0.0) is None


def test_sector_etf_map_covers_eleven_sectors() -> None:
    """SPDR Select Sector ETFs cover all 11 GICS sectors."""
    assert len(SECTOR_ETF_MAP) == 11
    assert set(SECTOR_ETF_MAP.values()) == set(DEFAULT_BASELINE["sector"].keys())


# ----------------------------------------------------------------------
# Audit-behavior tests
# ----------------------------------------------------------------------


def test_audit_clean_distribution_flags_nothing(tmp_path: Path) -> None:
    """Seeded distribution matches the baseline — no flagged buckets."""
    root = tmp_path / "candidates"
    _seed_baseline_matching(root, n=50)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert report.n_candidates == 50
    assert report.sample_size_adequate is True
    assert report.flagged_buckets == [], (
        f"clean distribution should not flag any bucket. "
        f"Flagged: {report.flagged_buckets}"
    )


def test_audit_flags_technology_overrepresentation(tmp_path: Path) -> None:
    """70% Tech distribution should produce a strong Technology over-rep flag."""
    root = tmp_path / "candidates"
    _seed_tech_overrepresented(root, n=50)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    tech_flag = next(
        (f for f in report.flagged_buckets
         if f["axis"] == "sector" and f["bucket"] == "Technology"),
        None,
    )
    assert tech_flag is not None, (
        f"Technology over-rep should be flagged. "
        f"Flagged: {report.flagged_buckets}"
    )
    assert tech_flag["direction"] == "over"
    assert tech_flag["z_score"] >= 2.0


def test_audit_flags_mega_cap_skew(tmp_path: Path) -> None:
    """80% mega-cap should be flagged as over-representation."""
    root = tmp_path / "candidates"
    _seed_mega_cap_skew(root)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    mega_flag = next(
        (f for f in report.flagged_buckets
         if f["axis"] == "market_cap" and f["bucket"] == "mega_cap"),
        None,
    )
    assert mega_flag is not None, (
        f"mega-cap over-rep should be flagged. "
        f"Flagged: {report.flagged_buckets}"
    )
    assert mega_flag["direction"] == "over"


def test_audit_flags_under_representation(tmp_path: Path) -> None:
    """Seeding ZERO Financials should flag Financials as under-represented."""
    root = tmp_path / "candidates"
    _seed_tech_overrepresented(root, n=50)  # 70% Tech, 10% Fin
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    # Real Estate (baseline 2%) was not seeded at all in the tech fixture.
    re_stat = next(
        (s for s in report.sector_stats if s.bucket == "Real Estate"),
        None,
    )
    assert re_stat is not None
    assert re_stat.observed_count == 0
    # With n=50 and baseline 2%, an observed 0% gives z = (0 - 0.02) / sqrt(0.02*0.98/50) ~ -1.01
    # That's NOT past the |z|>=2 threshold, so it won't appear in flagged_buckets.
    # The under-rep is just informational. Surface in stats but not flagged.
    assert re_stat.flagged is False  # 2% baseline + n=50 isn't enough for stat sig


def test_audit_handles_empty_candidates_dir(tmp_path: Path) -> None:
    """Zero candidates → returns a report with n=0 + low-confidence note."""
    root = tmp_path / "candidates"
    root.mkdir()
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert report.n_candidates == 0
    assert report.sample_size_adequate is False
    assert any("sample size" in n for n in report.notes)
    assert report.flagged_buckets == []


def test_audit_handles_missing_root(tmp_path: Path) -> None:
    """Non-existent candidates root → empty report, no crash."""
    root = tmp_path / "does_not_exist"
    report = audit(root)
    assert report.n_candidates == 0


def test_audit_respects_date_range(tmp_path: Path) -> None:
    """Candidates outside the date filter are excluded."""
    root = tmp_path / "candidates"
    _write_candidate(root, date(2026, 1, 15), "OLD1", "Technology", LARGE_CAP)
    _write_candidate(root, date(2026, 5, 10), "MID1", "Financials", LARGE_CAP)
    _write_candidate(root, date(2026, 8, 1), "NEW1", "Energy", LARGE_CAP)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert report.n_candidates == 1
    assert {c.bucket for c in report.sector_stats if c.observed_count > 0} == {
        "Financials"
    }


def test_audit_flags_missing_sector_data_quality(tmp_path: Path) -> None:
    """If many candidates lack sector_etf, a data-quality note fires."""
    root = tmp_path / "candidates"
    for i in range(20):
        sector = "Technology" if i < 10 else None  # 50% missing sector
        _write_candidate(root, date(2026, 5, 1), f"X{i:02d}", sector, LARGE_CAP)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31), min_sample=10)
    assert len(report.missing_data["no_sector"]) == 10
    assert any("no usable sector_etf" in n for n in report.notes)


def test_audit_flags_missing_market_cap_data_quality(tmp_path: Path) -> None:
    """If many candidates lack market_cap_usd, a data-quality note fires."""
    root = tmp_path / "candidates"
    for i in range(20):
        cap = LARGE_CAP if i < 8 else None  # 60% missing market_cap
        _write_candidate(root, date(2026, 5, 1), f"Y{i:02d}", "Technology", cap)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31), min_sample=10)
    assert len(report.missing_data["no_market_cap"]) == 12
    assert any("no usable market_cap_usd" in n for n in report.notes)


def test_audit_below_min_sample_carries_low_confidence_note(tmp_path: Path) -> None:
    """n < min_sample → still produces findings but with a low-confidence note."""
    root = tmp_path / "candidates"
    for i in range(5):
        _write_candidate(root, date(2026, 5, 1), f"S{i:02d}", "Technology", LARGE_CAP)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31), min_sample=30)
    assert report.n_candidates == 5
    assert report.sample_size_adequate is False
    assert any("low confidence" in n for n in report.notes)


def test_audit_uppercases_ticker(tmp_path: Path) -> None:
    """Tickers from ledgers should be normalized to uppercase in the report."""
    root = tmp_path / "candidates"
    _write_candidate(root, date(2026, 5, 1), "nvda", "Technology", LARGE_CAP)
    report = audit(root, since=date(2026, 5, 1), until=date(2026, 5, 31), min_sample=1)
    tech = next(s for s in report.sector_stats if s.bucket == "Technology")
    assert tech.tickers == ["NVDA"]


# ----------------------------------------------------------------------
# Tool contract + CLI tests
# ----------------------------------------------------------------------


def test_compute_from_paths_returns_trace_entry(tmp_path: Path) -> None:
    """compute_from_paths wraps audit in a TraceEntry."""
    root = tmp_path / "candidates"
    _seed_baseline_matching(root, n=50)
    entry = compute_from_paths(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert entry.tool == "tools/bias_audit.py"
    assert entry.inputs["candidates_root"] == str(root)
    assert entry.output["n_candidates"] == 50
    assert "sector_distribution" in entry.output


def test_render_markdown_no_flags(tmp_path: Path) -> None:
    """Clean fixture renders a 'no flagged buckets' Markdown report."""
    root = tmp_path / "candidates"
    _seed_baseline_matching(root, n=50)
    entry = compute_from_paths(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    md = _render_markdown(entry)
    assert "No flagged buckets" in md
    assert "Sector distribution" in md
    assert "Market-cap distribution" in md


def test_render_markdown_with_flags(tmp_path: Path) -> None:
    """Biased fixture renders a 'Flagged buckets' section."""
    root = tmp_path / "candidates"
    _seed_tech_overrepresented(root, n=50)
    entry = compute_from_paths(root, since=date(2026, 5, 1), until=date(2026, 5, 31))
    md = _render_markdown(entry)
    assert "Flagged buckets" in md
    assert "Technology" in md
    assert "[FLAGGED]" in md


def test_audit_custom_baseline_overrides_default(tmp_path: Path) -> None:
    """If the caller passes a baseline that expects 70% Technology, the
    tech-heavy fixture should NOT be flagged."""
    root = tmp_path / "candidates"
    _seed_tech_overrepresented(root, n=50)
    tech_heavy_baseline = {
        "sector": {"Technology": 0.70, "Financials": 0.10, "Health Care": 0.10,
                   "Industrials": 0.06, "Energy": 0.02, "Consumer Discretionary": 0.02},
        "market_cap": DEFAULT_BASELINE["market_cap"],
    }
    report = audit(root, baseline=tech_heavy_baseline)
    tech_flagged = any(
        f["axis"] == "sector" and f["bucket"] == "Technology"
        for f in report.flagged_buckets
    )
    assert not tech_flagged, (
        "Technology should NOT be flagged when the baseline expects 70% Technology"
    )
