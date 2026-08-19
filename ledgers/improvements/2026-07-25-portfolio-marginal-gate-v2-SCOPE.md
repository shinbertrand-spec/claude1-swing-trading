# Portfolio-marginal gate v2 — pre-registration (2026-07-25)

Operator-authorized single v2 with a more principled weight rule. **Exactly one
thing changes from v1: the combine weight.** The four gate clauses M1-M4 and their
thresholds are UNCHANGED (0.90 / 1.10 / 50% / 0.70) — relaxing those too would be
double-gerrymandering. This is pre-registered BEFORE the v2 run.

## The only change: weight rule

v1 combined at **50/50 equal-risk** (each sleeve vol-matched to 10% annual, then
equal weight). That over-weights a low-Sharpe diversifier — the v1 FAIL was driven
by the weak sleeve dragging combined Calmar (1.79 → 1.61) at 50% weight.

v2 combines at **walk-forward Sharpe-proportional** weight — the mean-variance-
optimal weighting for (near-)uncorrelated sleeves:

For each OOS year Y, using ONLY trailing-year (Y-1) data (no look-ahead):
1. Vol-match each sleeve to 10% annual using its Y-1 realized vol (as in v1).
2. Compute each sleeve's **trailing Sharpe** on Y-1's daily returns.
3. Weight ∝ **max(trailing Sharpe, 0)**, normalized to sum 1 (a sleeve that lost
   money last year gets 0 weight — don't fund a losing sleeve).
4. If both trailing Sharpes ≤ 0, fall back to 50/50.

This adapts the diversifier's allocation to its recent risk-adjusted merit: when
ts's trailing Sharpe dwarfs cross-asset's, cross-asset gets a small slice (≈ its
Sharpe share, ~15-25%); when cross-asset is doing well it gets more.

## Commitment (anti-fishing)

This is the ONE authorized v2. Sharpe-proportional is a single, standard,
principled rule chosen a-priori (not selected from a menu after seeing results).
I will report the v2 verdict honestly — PASS or FAIL — and will NOT iterate
further weight rules to manufacture a pass. If v2 also fails, that is the end:
the cross-asset diversifier does not earn a book slot on this data.

## Unchanged from v1

Book = ts_momentum_liquid_us · Candidate = cross-asset long-only crypto-free;
both via psim; rolling 1-yr OOS 2019-2025; trailing-year seeds weights; same
validation (book-alone agg Sharpe ~1.0-1.3); gate clauses M1-M4 and thresholds
identical. Per-year weights will be reported for transparency.
