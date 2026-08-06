# event_earnings_drift Stage A — validation chain steps 2-6 (2026-08-06)

Step 1 (run_spec two-clause + enforced DSR vs 91 trials) PASSED for both combos below.
This artifact runs steps 2-6 with the retirement-grade net-of-cost machinery.

## rank1 ear20/hist_off/hold21

- signals 8125
- **2a net split replica**: FULL Sharpe 0.65 / |MDD| 14.1% / n 926 / fill 11% -> FAIL · OOS(30%) Sharpe 0.46 / |MDD| 8.5% / n 272 -> FAIL
- **2b net walk-forward (LIVE fills)**: agg Sharpe **0.60** / |MDD| 12.3% / n 515 / fill 11% · aggregate FAIL · per-window 0/5 FAIL
  - windows: 2020:0.33x 2021:0.32x 2022:0.27x 2023:0.20x 2024:0.42x
- **3 DSR (net curve, N=91)**: **0.027** -> FAIL
- **4 corr vs ts_momentum**: **+0.42**
- **6 fill guard**: fill 11% · filled fwd 1.57% vs missed fwd 10.03% -> **EXECUTION-MODEL MISMATCH**
- **CHAIN (2-4,6)**: **FAIL -> retire per kill rule**

## rank2 ear10/hist_on/hold10

- signals 2812
- **2a net split replica**: FULL Sharpe 0.21 / |MDD| 13.9% / n 1148 / fill 41% -> FAIL · OOS(30%) Sharpe 0.12 / |MDD| 13.3% / n 339 -> FAIL
- **2b net walk-forward (LIVE fills)**: agg Sharpe **0.49** / |MDD| 13.9% / n 644 / fill 39% · aggregate FAIL · per-window 0/5 FAIL
  - windows: 2020:0.36x 2021:0.17x 2022:0.01x 2023:0.19x 2024:0.45x
- **3 DSR (net curve, N=91)**: **0.011** -> FAIL
- **4 corr vs ts_momentum**: **+0.38**
- **6 fill guard**: fill 41% · filled fwd 0.52% vs missed fwd 6.91% -> **EXECUTION-MODEL MISMATCH**
- **CHAIN (2-4,6)**: **FAIL -> retire per kill rule**

