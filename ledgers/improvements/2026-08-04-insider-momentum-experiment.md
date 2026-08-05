# Insider x momentum — momentum-rank selection experiment (pilot, 2026-08-04)

Retired insider KIND re-attacked at its diagnosed bottleneck (selection/capacity, not signal). Identical accounting loop as the committed best-first experiment; only selection varies. Momentum = trailing 126d return to the bar before fill (no lookahead). Net-of-cost; gate Sharpe>1.0 & |MDD|<25.0% & n>=30.

| arm | Sharpe | |MDD|% | n | fill% | ret% | gate |
|---|---|---|---|---|---|---|
| A FIRST-COME baseline | 0.20 | 25.4 | 209 | 25 | 12.7 | FAIL |
| B MOM-GATE first-come | 0.52 | 16.6 | 135 | 68 | 29.6 | FAIL |
| C MOM-BEST-FIRST W=10 | 0.35 | 22.4 | 221 | 26 | 28.2 | FAIL |
| D MOM-BEST-FIRST W=21 | 0.38 | 25.9 | 221 | 26 | 31.1 | FAIL |
| E MOM-GATE + BEST-FIRST W=21 | 0.45 | 40.8 | 160 | 80 | 96.3 | FAIL |
| F = E + 63d hold | 0.40 | 40.9 | 207 | 91 | 73.6 | FAIL |

- MOM-GATE dropped 650 of 850 signals (momentum<=0).
- Best arm: **B MOM-GATE first-come** — DSR **0.19** (n_trials=12; needs >0.95).
- Prior reference points: first-come RETIRE 0.14 · conviction-best-first 0.67.

## Verdict: **no arm clears the full gate (incl. DSR)**
- Trials-registry note: these 6 arms belong in ledgers/trials.yml on any merge.
- Suggest-only; nothing applied.
