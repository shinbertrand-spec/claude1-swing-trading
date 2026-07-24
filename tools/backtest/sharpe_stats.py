"""Probabilistic + Deflated Sharpe Ratio (cherry-pick B3 + C1).

Re-implemented (~80 lines of math, no new dependency) from the primary
source: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting and Non-Normality", Journal of
Portfolio Management 40(5), 2014 (SSRN 2460551). Formula provenance,
fetched + read 2026-07-24:

* PSR (their Eq. 2 inner term; original in Bailey & Lopez de Prado 2012):

      PSR(SR*) = Phi[ (SR_hat - SR*) * sqrt(T - 1)
                      / sqrt(1 - g3*SR_hat + (g4 - 1)/4 * SR_hat^2) ]

  with SR_hat the PER-PERIOD (non-annualized) observed Sharpe, T the number
  of return observations, g3 the returns skewness and g4 the RAW kurtosis
  (Normal => g4 = 3, NOT excess).

* Expected maximum Sharpe across N independent trials (their Eq. 1 / A.1),
  used as the DSR rejection threshold SR0:

      SR0 = sqrt(V[{SR_n}]) * ( (1-gamma) * Zinv(1 - 1/N)
                                + gamma * Zinv(1 - 1/(N*e)) )

  with gamma the Euler-Mascheroni constant (0.5772...), V[{SR_n}] the
  variance of the trials' per-period Sharpe estimates.

* DSR = PSR(SR0)  (their Eq. 2). Gate convention per the 2026-07-24
  cherry-pick spec: DSR > 0.95 required in addition to the existing gate.

* Effective number of independent trials from M dependent trials with
  average pairwise correlation rho (their Appendix A.3, Eq. 9):

      N_eff = rho + (1 - rho) * M

Numerical cross-check fixtures from the paper's worked example (tested in
tests/test_sharpe_stats.py): N=100, V=0.5 annualized (=> 1/(2*250)
per-period), T=1250, g3=-3, g4=10, annual SR 2.5 => SR0 ~= 0.1132,
DSR ~= 0.9004 (< 0.95 => reject); N=46 => 0.9505; Normal returns
(g3=0, g4=3) => 0.9505 at N=88.

No scipy: Phi uses math.erf (exact); Phi^-1 uses Acklam's rational
approximation polished with one Halley step against the erf-exact CDF
(abs error < 1e-12 over (1e-12, 1-1e-12) — asserted in tests).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

EULER_MASCHERONI = 0.5772156649015329


def normal_cdf(x: float) -> float:
    """Standard normal CDF via the exact error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Acklam's inverse-normal-CDF rational approximation coefficients.
_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam + one Halley refinement step)."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0, 1), got {p}")
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = ((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
             / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    elif p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        x = ((((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q
             / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0))
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
              / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    # One Halley step against the erf-exact CDF pushes the ~1e-9 rational
    # approximation to ~machine precision.
    e = normal_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


def sharpe_moments(returns: Sequence[float]) -> dict[str, float]:
    """Per-period Sharpe + the PSR moment inputs from a return series.

    Returns dict(sr, skew, kurt_raw, n). Kurtosis is RAW (Normal = 3) per
    the paper's convention — note pandas' .kurt() is EXCESS kurtosis; this
    helper exists precisely so callers don't mix the two up.
    """
    xs = [float(r) for r in returns]
    n = len(xs)
    if n < 4:
        raise ValueError(f"need >= 4 return observations, got {n}")
    mean = sum(xs) / n
    devs = [x - mean for x in xs]
    m2 = sum(d * d for d in devs) / n
    if m2 <= 0:
        raise ValueError("zero-variance return series")
    m3 = sum(d ** 3 for d in devs) / n
    m4 = sum(d ** 4 for d in devs) / n
    std_sample = math.sqrt(m2 * n / (n - 1))
    return {
        "sr": mean / std_sample,
        "skew": m3 / m2 ** 1.5,
        "kurt_raw": m4 / (m2 * m2),
        "n": n,
    }


def psr(
    sr: float, benchmark_sr: float, n_obs: int, skew: float, kurt_raw: float,
) -> float:
    """Probabilistic Sharpe Ratio: P(true SR > benchmark_sr | observed).

    All Sharpe inputs PER-PERIOD (divide an annualized SR by sqrt(252)
    before calling). ``kurt_raw`` is raw kurtosis (Normal = 3).
    """
    if n_obs < 2:
        raise ValueError("need at least 2 observations")
    denom_sq = 1.0 - skew * sr + (kurt_raw - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0:
        # Extreme skew/kurtosis vs SR — the asymptotic variance breaks down.
        raise ValueError(
            f"PSR denominator non-positive ({denom_sq:.4f}) — moment inputs "
            "inconsistent with the asymptotic distribution"
        )
    z = (sr - benchmark_sr) * math.sqrt(n_obs - 1.0) / math.sqrt(denom_sq)
    return normal_cdf(z)


def expected_max_sharpe(var_trials: float, n_trials: int, mean_trials: float = 0.0) -> float:
    """E[max SR] across ``n_trials`` independent trials (paper Eq. 1) —
    the DSR rejection threshold SR0 under H0: true SR = 0 (mean 0).

    ``var_trials`` is the variance of the trials' PER-PERIOD Sharpe
    estimates.
    """
    if n_trials < 2:
        raise ValueError("n_trials must be >= 2")
    if var_trials < 0:
        raise ValueError("var_trials must be >= 0")
    g = EULER_MASCHERONI
    return mean_trials + math.sqrt(var_trials) * (
        (1.0 - g) * normal_ppf(1.0 - 1.0 / n_trials)
        + g * normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe(
    sr: float,
    n_obs: int,
    skew: float,
    kurt_raw: float,
    *,
    var_trials: float,
    n_trials: int,
) -> dict[str, float]:
    """DSR = PSR(SR0) with SR0 = E[max SR] under multiple testing.

    Returns dict(sr0, dsr). All Sharpe/variance inputs PER-PERIOD.
    """
    sr0 = expected_max_sharpe(var_trials, n_trials)
    return {"sr0": sr0, "dsr": psr(sr, sr0, n_obs, skew, kurt_raw)}


def effective_trials(m_trials: int, avg_correlation: Optional[float]) -> float:
    """N_eff = rho + (1 - rho) * M (paper Eq. 9). ``avg_correlation=None``
    returns M unchanged — using raw M OVERSTATES E[max SR] and therefore
    deflates MORE, i.e. the conservative default when rho is unmeasured."""
    if m_trials < 1:
        raise ValueError("m_trials must be >= 1")
    if avg_correlation is None:
        return float(m_trials)
    rho = max(0.0, min(1.0, avg_correlation))
    return rho + (1.0 - rho) * m_trials


def annualized_to_per_period(sr_annual: float, periods_per_year: int = 252) -> float:
    return sr_annual / math.sqrt(periods_per_year)


def per_period_to_annualized(sr_per_period: float, periods_per_year: int = 252) -> float:
    return sr_per_period * math.sqrt(periods_per_year)
