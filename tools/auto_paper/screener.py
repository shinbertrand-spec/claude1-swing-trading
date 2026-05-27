"""Pre-placement screener — strategy-blind disqualifier checks.

Catches the things the walk-forward backtest's sanitized universe could not
see: active securities class actions, recent dilutive offerings, forward
earnings inside the 10-trading-day blackout. Also corrects the quant
scanner's XLK-everywhere sector heuristic via a real GICS-sector lookup.

Phase 1 design intent (per `wiki/notes/auto-paper-screener.md`-equivalent
in-session decision):

* **Hard blocks** = the things CLAUDE.md disqualifiers cover that an OHLCV
  signal cannot detect. False positives are acceptable here — we'd rather
  skip a tradeable name than enter a named defendant.
* **Strategy-relative judgments** (Stage 2 vs Stage 4, fundamental momentum
  thresholds, setup-pattern detection) are NOT screened. Those are baked
  into the quant strategy's signal definition and screening them out would
  veto every xs_short_term_reversal pick by construction.

Composes with :func:`tools.auto_paper.pipeline.place_candidate` — runs
after the deployable-setup filter and before the broker client is even
constructed, so we never pay a broker round-trip on a screener-blocked
candidate.

CLI::

    uv run python -m tools.auto_paper.screener GO
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..contract import TraceEntry
from ..earnings_calendar import compute_from_ticker as earnings_from_ticker

TOOL = "tools/auto_paper/screener.py"

# yfinance GICS sector → SPDR sector ETF.
# Communication Services has no GICS-clean mapping in yfinance — yfinance
# returns "Communication Services" for FB/GOOGL/NFLX and "Technology" for
# AAPL/MSFT, so we mirror that taxonomy. The list is non-exhaustive on
# purpose: unknown sectors fall through to the caller-supplied default
# (typically XLK from the quant scanner heuristic) and the screener
# surfaces a WARN entry rather than a hard block.
_YF_SECTOR_TO_ETF: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}

# Litigation / SEC-investigation keyword pattern. False positives are
# acceptable here — we'd rather skip a tradeable name than enter a named
# defendant. Tuned against the GO case (~3 distinct "GO Lawsuit Alleges..."
# headlines pulled by the finviz news panel as of 2026-05-27).
_LITIGATION_PATTERN = re.compile(
    r"class action"
    r"|securities (lawsuit|fraud)"
    r"|sec investigat"
    r"|filed (a )?lawsuit"
    r"|shareholder (lawsuit|investigation)"
    r"|investor (alert|investigation|notification)"
    r"|lawsuit alleges"
    r"|notice of (pendency|class action)",
    re.IGNORECASE,
)

# Dilutive-raise keyword pattern. Negative lookbehind for "no" handles the
# ambiguous "no offering announced" phrasing some PR wires use.
_DILUTION_PATTERN = re.compile(
    r"(?<!no )("
    r"secondary offering"
    r"|shelf offering"
    r"|common stock offering"
    r"|atm offering"
    r"|at-the-market offering"
    r"|registered direct"
    r"|pipe offering"
    r"|public offering of (common )?stock"
    r"|prices (its )?(public )?offering"
    r"|announces (the )?pricing of"
    r")",
    re.IGNORECASE,
)

# Finviz scrape — rate-limit friendly defaults.
_FINVIZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
_FINVIZ_NEWS_REGEX = re.compile(
    r'<a[^>]*class="tab-link-news"[^>]*>([^<]+)</a>'
)
# Conservative caller-throttle. Finviz rate-limits aggressively when
# requests come in under ~1.5s apart. Caller in pipeline runs sequentially
# so this is the per-candidate delay before each fetch.
_FINVIZ_THROTTLE_SECS = 2.0
_LAST_FINVIZ_FETCH: list[float] = [0.0]  # mutable singleton for global throttle


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """One screener check's outcome."""
    check: str         # e.g. "litigation", "dilution", "earnings_blackout", "sector_lookup"
    passed: bool       # True = no block. False = hard-block, OR (sector-only) advisory correction.
    reason: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScreenerResult:
    """Aggregate of all checks. ``blocked`` iff any hard-block check failed."""
    ticker: str
    blocked: bool
    blocking_checks: list[str]  # names of checks that failed AND are hard-blocks
    corrected_sector_etf: Optional[str]  # set iff sector_lookup detected a mismatch
    checks: list[CheckResult]
    computed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "blocked": self.blocked,
            "blocking_checks": self.blocking_checks,
            "corrected_sector_etf": self.corrected_sector_etf,
            "checks": [c.to_dict() for c in self.checks],
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# Finviz news fetch (with global throttle + one retry on 429)
# ---------------------------------------------------------------------------


def _fetch_finviz_news_headlines(ticker: str) -> tuple[list[str], Optional[str]]:
    """Return (headlines, error_reason). On 429 retries once after a back-off.

    Headlines are stripped of HTML noise but otherwise raw. Caller scans
    against keyword patterns. Empty list + error_reason means "could not
    fetch" — caller decides whether to fail-open or fail-closed.
    """
    # Global throttle — sleep just enough to space requests by 2s. Pipeline
    # runs sequentially so this is per-candidate, not per-strategy.
    elapsed = time.monotonic() - _LAST_FINVIZ_FETCH[0]
    if elapsed < _FINVIZ_THROTTLE_SECS:
        time.sleep(_FINVIZ_THROTTLE_SECS - elapsed)

    url = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
    req = urllib.request.Request(url, headers=_FINVIZ_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # Back off and retry once. If the second attempt also fails we
            # surface the error rather than hammering.
            time.sleep(5.0)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
            except Exception as e2:
                _LAST_FINVIZ_FETCH[0] = time.monotonic()
                return [], f"finviz HTTPError after retry: {e2}"
        else:
            _LAST_FINVIZ_FETCH[0] = time.monotonic()
            return [], f"finviz HTTPError: {e.code}"
    except Exception as e:
        _LAST_FINVIZ_FETCH[0] = time.monotonic()
        return [], f"finviz fetch failed: {e}"

    _LAST_FINVIZ_FETCH[0] = time.monotonic()
    raw = _FINVIZ_NEWS_REGEX.findall(html)
    cleaned = [re.sub(r"\s+", " ", h).strip() for h in raw]
    return cleaned, None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_litigation(ticker: str, headlines: list[str], fetch_error: Optional[str]) -> CheckResult:
    """Scan finviz news headlines for litigation / SEC investigation keywords.

    Failure mode handling: if the finviz fetch failed entirely (network /
    rate-limit-after-retry), we fail-OPEN here (passed=True) rather than
    block every candidate when finviz is down. The fetch error is recorded
    in evidence so the operator can see when this happens. False negatives
    from a fetch outage are acceptable; a 100% block on every candidate
    when finviz reformats HTML is not.
    """
    if fetch_error and not headlines:
        return CheckResult(
            check="litigation",
            passed=True,
            reason=None,
            evidence={"fetch_error": fetch_error, "fail_mode": "fail_open"},
        )
    hits = [h for h in headlines if _LITIGATION_PATTERN.search(h)]
    if hits:
        return CheckResult(
            check="litigation",
            passed=False,
            reason=(
                f"Active litigation / SEC concern detected — "
                f"{len(hits)} matching headline(s)"
            ),
            evidence={
                "matching_headlines": hits[:5],
                "total_matches": len(hits),
                "headlines_scanned": len(headlines),
            },
        )
    return CheckResult(
        check="litigation",
        passed=True,
        evidence={"headlines_scanned": len(headlines)},
    )


def _check_dilution(ticker: str, headlines: list[str], fetch_error: Optional[str]) -> CheckResult:
    """Scan finviz news headlines for dilutive-offering keywords.

    Same fail-open behavior as litigation on a fetch outage.
    """
    if fetch_error and not headlines:
        return CheckResult(
            check="dilution",
            passed=True,
            reason=None,
            evidence={"fetch_error": fetch_error, "fail_mode": "fail_open"},
        )
    hits = [h for h in headlines if _DILUTION_PATTERN.search(h)]
    if hits:
        return CheckResult(
            check="dilution",
            passed=False,
            reason=(
                f"Recent dilutive-raise announcement detected — "
                f"{len(hits)} matching headline(s)"
            ),
            evidence={
                "matching_headlines": hits[:5],
                "total_matches": len(hits),
                "headlines_scanned": len(headlines),
            },
        )
    return CheckResult(
        check="dilution",
        passed=True,
        evidence={"headlines_scanned": len(headlines)},
    )


def _check_earnings_blackout(ticker: str) -> CheckResult:
    """Hit the existing :func:`tools.earnings_calendar` for forward 10d check.

    Fail-OPEN on tool error (yfinance hiccup): we'd rather take the trade
    than block on a flaky API. Evidence records the error so the operator
    can audit.
    """
    try:
        entry = earnings_from_ticker(ticker)
    except Exception as e:
        return CheckResult(
            check="earnings_blackout",
            passed=True,
            evidence={"fetch_error": str(e), "fail_mode": "fail_open"},
        )
    out = entry.output
    within = bool(out.get("within_blackout_window"))
    if within:
        return CheckResult(
            check="earnings_blackout",
            passed=False,
            reason=(
                f"Earnings within {out.get('blackout_threshold_days', 10)}-trading-day window "
                f"({out.get('trading_days_to_earnings')} days to {out.get('next_earnings_date')})"
            ),
            evidence={
                "next_earnings_date": out.get("next_earnings_date"),
                "trading_days_to_earnings": out.get("trading_days_to_earnings"),
                "source": out.get("source_field"),
            },
        )
    return CheckResult(
        check="earnings_blackout",
        passed=True,
        evidence={
            "next_earnings_date": out.get("next_earnings_date"),
            "trading_days_to_earnings": out.get("trading_days_to_earnings"),
        },
    )


def _lookup_sector(ticker: str, candidate_sector_etf: Optional[str]) -> CheckResult:
    """Look up real GICS sector via yfinance, map to SPDR sector ETF, and
    compare to the candidate's claimed sector. Mismatch is NOT a hard block —
    only a correction. ``passed=True`` always; ``corrected_sector_etf`` field
    in the parent ScreenerResult carries the correction.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
    except Exception as e:
        return CheckResult(
            check="sector_lookup",
            passed=True,
            evidence={"fetch_error": str(e), "fail_mode": "fail_open"},
        )
    yf_sector = info.get("sector") if isinstance(info, dict) else None
    yf_industry = info.get("industry") if isinstance(info, dict) else None
    correct_etf = _YF_SECTOR_TO_ETF.get(yf_sector) if yf_sector else None

    mismatch = (
        correct_etf is not None
        and candidate_sector_etf is not None
        and correct_etf != candidate_sector_etf
    )
    return CheckResult(
        check="sector_lookup",
        passed=True,  # never blocks
        reason=(
            f"sector mismatch: claimed={candidate_sector_etf}, actual={correct_etf}"
            if mismatch else None
        ),
        evidence={
            "yfinance_sector": yf_sector,
            "yfinance_industry": yf_industry,
            "claimed_sector_etf": candidate_sector_etf,
            "actual_sector_etf": correct_etf,
            "mismatch": mismatch,
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def screen(ticker: str, *, claimed_sector_etf: Optional[str] = None) -> ScreenerResult:
    """Run all four checks against ``ticker`` and return a ScreenerResult.

    Args:
        ticker: stock ticker symbol.
        claimed_sector_etf: the sector ETF the candidate is currently
            tagged with (typically XLK from the quant scanner heuristic).
            Used only by the sector_lookup check to detect mismatches.

    Returns:
        :class:`ScreenerResult`. ``blocked=True`` iff any hard-block check
        failed. The sector_lookup check never sets ``blocked``; it surfaces
        a correction via ``corrected_sector_etf``.
    """
    headlines, fetch_error = _fetch_finviz_news_headlines(ticker)
    checks = [
        _check_litigation(ticker, headlines, fetch_error),
        _check_dilution(ticker, headlines, fetch_error),
        _check_earnings_blackout(ticker),
        _lookup_sector(ticker, claimed_sector_etf),
    ]
    # Hard-block checks (litigation, dilution, earnings_blackout). The
    # sector_lookup check is advisory-only.
    blocking_checks = [
        c.check for c in checks
        if not c.passed and c.check in {"litigation", "dilution", "earnings_blackout"}
    ]
    sector_check = next((c for c in checks if c.check == "sector_lookup"), None)
    corrected_sector_etf: Optional[str] = None
    if sector_check is not None and sector_check.evidence.get("mismatch"):
        corrected_sector_etf = sector_check.evidence.get("actual_sector_etf")

    return ScreenerResult(
        ticker=ticker,
        blocked=len(blocking_checks) > 0,
        blocking_checks=blocking_checks,
        corrected_sector_etf=corrected_sector_etf,
        checks=checks,
        computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def screen_as_trace_entry(
    ticker: str, *, claimed_sector_etf: Optional[str] = None,
) -> TraceEntry:
    """Same as :func:`screen` but returns the result wrapped in a
    :class:`TraceEntry` for direct insertion into a ledger's reasoning_trace.
    """
    result = screen(ticker, claimed_sector_etf=claimed_sector_etf)
    return TraceEntry(
        tool=TOOL,
        inputs={"ticker": ticker, "claimed_sector_etf": claimed_sector_etf},
        output=result.to_dict(),
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.auto_paper.screener",
        description="Pre-placement screener — strategy-blind disqualifier checks.",
    )
    p.add_argument("ticker")
    p.add_argument(
        "--claimed-sector",
        default=None,
        help="Sector ETF the candidate is currently tagged with (e.g. XLK)",
    )
    args = p.parse_args()
    entry = screen_as_trace_entry(args.ticker, claimed_sector_etf=args.claimed_sector)
    print(entry.to_json())


if __name__ == "__main__":
    main()
