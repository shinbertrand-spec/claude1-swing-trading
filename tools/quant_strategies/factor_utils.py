"""Cross-sectional factor math — winsorize, z-score, sector-neutralize.

Pure functions over ``{ticker: value}`` maps. Shared by every fundamental-factor
KIND (value, quality, profitability). No pandas dependency at the interface so
the KINDs can call these on plain dicts built from PIT fundamentals.

Sector-neutralization (demean-by-sector then standardize) removes the factor's
sector tilt — e.g. value naturally overweights banks/energy; neutralizing means
"cheap *relative to its sector peers*", which is the academic-standard
construction (AQR, Fama-French industry-adjusted).
"""
from __future__ import annotations

import math
from typing import Optional


def winsorize(values: dict[str, float], limit: float = 0.02) -> dict[str, float]:
    """Clip each value to the [limit, 1-limit] cross-sectional quantiles.

    Caps the influence of a handful of extreme prints (a busted ratio from a
    near-zero denominator) without dropping the name. ``limit=0.02`` = 2%/98%.
    """
    if not values or limit <= 0:
        return dict(values)
    xs = sorted(values.values())
    n = len(xs)
    lo = xs[max(0, int(math.floor(limit * (n - 1))))]
    hi = xs[min(n - 1, int(math.ceil((1 - limit) * (n - 1))))]
    return {k: min(max(v, lo), hi) for k, v in values.items()}


def zscore(values: dict[str, float]) -> dict[str, float]:
    """Standardize to mean 0 / std 1 across the cross-section (sample std).

    Returns all-zeros when there is <2 names or zero dispersion.
    """
    if len(values) < 2:
        return {k: 0.0 for k in values}
    xs = list(values.values())
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mu) / sd for k, v in values.items()}


def sector_neutralize(
    values: dict[str, float],
    sectors: dict[str, str],
) -> dict[str, float]:
    """Demean within sector, then z-score the residuals across the whole cross-
    section. Names with an unknown sector are demeaned against the global mean.
    """
    if not values:
        return {}
    # group by sector (fallback bucket for unknowns)
    groups: dict[str, list[str]] = {}
    for t in values:
        groups.setdefault(sectors.get(t, "UNKNOWN"), []).append(t)
    demeaned: dict[str, float] = {}
    for _sec, members in groups.items():
        if len(members) >= 2:
            mu = sum(values[m] for m in members) / len(members)
        else:
            # singleton sector: demean against the global mean instead (a lone
            # name can't be "neutral" against itself)
            mu = sum(values.values()) / len(values)
        for m in members:
            demeaned[m] = values[m] - mu
    return zscore(demeaned)


def standardize_factor(
    raw: dict[str, float],
    sectors: Optional[dict[str, str]] = None,
    *,
    winsor_limit: float = 0.02,
) -> dict[str, float]:
    """Full pipeline: winsorize → (sector-neutralize | z-score)."""
    if not raw:
        return {}
    w = winsorize(raw, winsor_limit)
    if sectors:
        return sector_neutralize(w, sectors)
    return zscore(w)


def combine(
    factors: dict[str, dict[str, float]],
    weights: dict[str, float],
) -> dict[str, float]:
    """Weighted sum of standardized factors over the common ticker set.

    ``factors``: {factor_name: {ticker: z}}; ``weights``: {factor_name: w}.
    Only tickers present in EVERY weighted factor are scored (a name missing a
    component is excluded that period rather than scored on partial info).
    """
    active = {name: w for name, w in weights.items() if w != 0 and name in factors}
    if not active:
        return {}
    common: Optional[set[str]] = None
    for name in active:
        ks = set(factors[name])
        common = ks if common is None else (common & ks)
    if not common:
        return {}
    # sorted() → deterministic key order (set iteration is PYTHONHASHSEED-dependent)
    return {t: sum(weights[name] * factors[name][t] for name in active)
            for t in sorted(common)}
