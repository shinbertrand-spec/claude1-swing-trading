"""Tests for the C2 CPCV splitter (tools.backtest.cpcv).

The leakage test is the load-bearing one: on synthetic data with a known
label span, no train sample's evaluation window may intersect any test
span (+ embargo) of its combination.
"""
from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pytest

from tools.backtest import cpcv


def _times(n, label_span=5):
    """Sample i predicts at t=i and its label evaluates through t=i+span."""
    pred = list(range(n))
    ev = [i + label_span for i in range(n)]
    return pred, ev


class TestCombinatorics:
    def test_combination_count(self):
        cv = cpcv.CombinatorialPurgedCV(n_groups=12, n_test_groups=2)
        pred, ev = _times(240)
        splits = list(cv.split(pred, ev))
        assert len(splits) == math.comb(12, 2) == 66

    def test_each_group_appears_in_c_n1_k1_combos(self):
        appearances = {g: 0 for g in range(12)}
        for tg in combinations(range(12), 2):
            for g in tg:
                appearances[g] += 1
        assert all(v == math.comb(11, 1) == 11 for v in appearances.values())
        assert cpcv.n_paths(12, 2) == 11

    def test_path_assignment_covers_every_group_once_per_path(self):
        n, k = 12, 2
        mapping = cpcv.path_assignment(n, k)
        by_path: dict[int, list[int]] = {}
        for (_, g), pid in mapping.items():
            by_path.setdefault(pid, []).append(g)
        assert len(by_path) == cpcv.n_paths(n, k)
        for pid, groups in by_path.items():
            assert sorted(groups) == list(range(n)), f"path {pid} incomplete"

    def test_constructor_validation(self):
        with pytest.raises(ValueError):
            cpcv.CombinatorialPurgedCV(n_groups=1)
        with pytest.raises(ValueError):
            cpcv.CombinatorialPurgedCV(n_groups=4, n_test_groups=4)

    def test_too_few_samples_raises(self):
        cv = cpcv.CombinatorialPurgedCV(n_groups=12, n_test_groups=2)
        pred, ev = _times(5)
        with pytest.raises(ValueError):
            list(cv.split(pred, ev))


class TestPurgeEmbargoNoLeakage:
    def test_no_train_eval_window_intersects_test_span(self):
        span = 7
        cv = cpcv.CombinatorialPurgedCV(n_groups=8, n_test_groups=2)
        pred, ev = _times(400, label_span=span)
        for split in cv.split(pred, ev):
            bounds = cv.group_bounds(400)
            for g in split.test_groups:
                s, e = bounds[g]
                t_start, t_end = pred[s], max(ev[s:e])
                for i in split.train_idx:
                    # train sample [pred[i], ev[i]] must not intersect [t_start, t_end]
                    assert ev[i] < t_start or pred[i] > t_end, (
                        f"leak: train {i} [{pred[i]},{ev[i]}] intersects "
                        f"test [{t_start},{t_end}]"
                    )

    def test_purge_drops_boundary_train_samples(self):
        # samples just before a test group whose labels extend into it must go
        span = 5
        cv = cpcv.CombinatorialPurgedCV(n_groups=4, n_test_groups=1)
        pred, ev = _times(100, label_span=span)
        split = next(s for s in cv.split(pred, ev) if s.test_groups == (1,))
        bounds = cv.group_bounds(100)
        s, _ = bounds[1]
        # the `span` samples immediately before the test start are purged
        for i in range(s - span, s):
            assert i not in split.train_idx
        # but a sample safely before the purge zone survives
        assert (s - span - 1) in split.train_idx

    def test_embargo_drops_post_test_train_samples(self):
        span = 0  # isolate the embargo effect
        embargo = 10
        cv = cpcv.CombinatorialPurgedCV(n_groups=4, n_test_groups=1, embargo=embargo)
        pred, ev = _times(100, label_span=span)
        split = next(s for s in cv.split(pred, ev) if s.test_groups == (1,))
        bounds = cv.group_bounds(100)
        _, e = bounds[1]
        t_end = max(ev[bounds[1][0]:e])
        for i in split.train_idx:
            if pred[i] > t_end:                 # post-test side only
                assert pred[i] > t_end + embargo
        # embargo zone is actually non-trivial: some sample was dropped
        post_test = [i for i in range(e, 100)]
        dropped = [i for i in post_test if i not in set(split.train_idx)]
        assert len(dropped) >= embargo

    def test_zero_embargo_keeps_adjacent_post_test_sample(self):
        cv = cpcv.CombinatorialPurgedCV(n_groups=4, n_test_groups=1, embargo=0)
        pred, ev = _times(100, label_span=0)
        split = next(s for s in cv.split(pred, ev) if s.test_groups == (1,))
        _, e = cv.group_bounds(100)[1]
        assert e in split.train_idx  # first sample after the test group survives

    def test_train_test_disjoint_and_test_complete(self):
        cv = cpcv.CombinatorialPurgedCV(n_groups=6, n_test_groups=2)
        pred, ev = _times(300, label_span=3)
        for split in cv.split(pred, ev):
            assert set(split.train_idx).isdisjoint(set(split.test_idx))
            bounds = cv.group_bounds(300)
            expected = sorted(
                i for g in split.test_groups
                for i in range(bounds[g][0], bounds[g][1])
            )
            assert list(split.test_idx) == expected


class TestSeriesStats:
    def test_stats_sane_on_known_series(self):
        rng = np.random.default_rng(2)
        r = rng.normal(0.001, 0.01, size=500)
        sharpe, mdd, cum = cpcv._series_stats(r)
        assert sharpe == pytest.approx(
            np.mean(r) / np.std(r, ddof=1) * np.sqrt(252), rel=1e-9,
        )
        assert mdd <= 0
        assert cum == pytest.approx((np.prod(1 + r) - 1) * 100, rel=1e-9)

    def test_short_series_degrades(self):
        assert cpcv._series_stats(np.zeros(2)) == (0.0, 0.0, 0.0)
