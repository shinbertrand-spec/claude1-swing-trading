"""Tests for tools.backtest.sharpe_stats — pinned to the published worked
example in Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio"
(SSRN 2460551), pp. 9-10, plus normal-inverse reference values.
"""
from __future__ import annotations

import math
import random

import pytest

from tools.backtest import sharpe_stats as ss


class TestNormalFunctions:
    def test_cdf_reference_values(self):
        assert ss.normal_cdf(0.0) == pytest.approx(0.5)
        assert ss.normal_cdf(1.959963985) == pytest.approx(0.975, abs=1e-9)
        assert ss.normal_cdf(-1.0) == pytest.approx(0.15865525393146, abs=1e-10)

    def test_ppf_reference_values(self):
        assert ss.normal_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
        assert ss.normal_ppf(0.975) == pytest.approx(1.959963985, abs=1e-8)
        assert ss.normal_ppf(0.99) == pytest.approx(2.326347874, abs=1e-8)
        assert ss.normal_ppf(0.01) == pytest.approx(-2.326347874, abs=1e-8)

    def test_ppf_cdf_roundtrip_precision(self):
        for p in (1e-10, 1e-6, 0.02, 0.3, 0.5, 0.7, 0.98, 1 - 1e-6, 1 - 1e-10):
            assert ss.normal_cdf(ss.normal_ppf(p)) == pytest.approx(p, abs=1e-12)

    def test_ppf_domain(self):
        for p in (0.0, 1.0, -0.1, 1.1):
            with pytest.raises(ValueError):
                ss.normal_ppf(p)


class TestPaperWorkedExample:
    """The paper's Treasury-seasonality example: N=100 trials, annualized
    V[{SR_n}]=0.5 (=> per-period 1/(2*250)), T=1250 daily obs, skew=-3,
    raw kurtosis=10, annualized SR 2.5 at 250 obs/year."""

    SR = 2.5 / math.sqrt(250)          # per-period observed Sharpe
    VAR_TRIALS = 1.0 / (2 * 250)       # per-period variance of trial SRs
    T = 1250

    def test_sr0_matches_paper(self):
        sr0 = ss.expected_max_sharpe(self.VAR_TRIALS, 100)
        assert sr0 == pytest.approx(0.1132, abs=5e-4)

    def test_dsr_matches_paper(self):
        out = ss.deflated_sharpe(
            self.SR, self.T, -3.0, 10.0,
            var_trials=self.VAR_TRIALS, n_trials=100,
        )
        assert out["dsr"] == pytest.approx(0.9004, abs=5e-4)
        assert out["dsr"] < 0.95  # the paper's conclusion: reject

    def test_dsr_at_46_trials_clears(self):
        out = ss.deflated_sharpe(
            self.SR, self.T, -3.0, 10.0,
            var_trials=self.VAR_TRIALS, n_trials=46,
        )
        assert out["dsr"] == pytest.approx(0.9505, abs=5e-4)

    def test_normal_returns_variant(self):
        # Normal returns (skew 0, raw kurtosis 3) tolerate N=88 at 0.9505
        out = ss.deflated_sharpe(
            self.SR, self.T, 0.0, 3.0,
            var_trials=self.VAR_TRIALS, n_trials=88,
        )
        assert out["dsr"] == pytest.approx(0.9505, abs=5e-4)


class TestPSR:
    def test_sr_equal_benchmark_is_half(self):
        assert ss.psr(0.1, 0.1, 100, 0.0, 3.0) == pytest.approx(0.5)

    def test_more_observations_more_confidence(self):
        lo = ss.psr(0.1, 0.05, 50, 0.0, 3.0)
        hi = ss.psr(0.1, 0.05, 500, 0.0, 3.0)
        assert hi > lo

    def test_negative_skew_reduces_confidence(self):
        normal = ss.psr(0.1, 0.05, 250, 0.0, 3.0)
        skewed = ss.psr(0.1, 0.05, 250, -2.0, 3.0)
        assert skewed < normal

    def test_fat_tails_reduce_confidence(self):
        normal = ss.psr(0.1, 0.05, 250, 0.0, 3.0)
        fat = ss.psr(0.1, 0.05, 250, 0.0, 12.0)
        assert fat < normal

    def test_hand_computed_value(self):
        # sr=0.1, benchmark=0, n=101, skew=0, kurt=3:
        # denom = sqrt(1 - 0 + (3-1)/4*0.01) = sqrt(1.005)
        # z = 0.1*10/sqrt(1.005) = 0.99751...
        z = 0.1 * 10.0 / math.sqrt(1.005)
        assert ss.psr(0.1, 0.0, 101, 0.0, 3.0) == pytest.approx(ss.normal_cdf(z))


class TestMomentsAndHelpers:
    def test_sharpe_moments_on_known_series(self):
        rng = random.Random(7)
        xs = [rng.gauss(0.001, 0.02) for _ in range(2000)]
        m = m_dict = ss.sharpe_moments(xs)
        assert m["n"] == 2000
        # a large Normal sample: skew ~ 0, raw kurtosis ~ 3
        assert abs(m_dict["skew"]) < 0.2
        assert m_dict["kurt_raw"] == pytest.approx(3.0, abs=0.4)
        mean = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
        assert m["sr"] == pytest.approx(mean / sd)

    def test_sharpe_moments_guards(self):
        with pytest.raises(ValueError):
            ss.sharpe_moments([0.01, 0.02])
        with pytest.raises(ValueError):
            ss.sharpe_moments([0.01] * 10)  # zero variance

    def test_effective_trials(self):
        assert ss.effective_trials(100, None) == 100.0
        assert ss.effective_trials(100, 0.0) == 100.0
        assert ss.effective_trials(100, 1.0) == 1.0
        assert ss.effective_trials(100, 0.5) == pytest.approx(50.5)

    def test_annualization_roundtrip(self):
        assert ss.per_period_to_annualized(
            ss.annualized_to_per_period(1.5)
        ) == pytest.approx(1.5)

    def test_expected_max_sharpe_grows_with_trials(self):
        v = 1.0 / 500
        assert ss.expected_max_sharpe(v, 100) > ss.expected_max_sharpe(v, 10)
