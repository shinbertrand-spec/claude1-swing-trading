# Options delta-hedged straddle (daily hedge, BS off VIX) — proxy (pilot, 2026-08-04)

**Research-only. 1-mo ATM straddle, delta-hedged DAILY along realized SPY path; option-spread haircut 1% + 1bps/hedge + discrete-hedge error included. VIX as ATM-IV proxy, r=0, skew ignored. NOT deployable.**

- Monthly obs n=113 (2017-01..2026-05)
- SHORT straddle (delta-hedged): mean +0.00750 /notional/mo · **annualized Sharpe 1.95** · hit 81% · **skew -1.26** · |MDD| 8%
- LONG straddle (gamma scalp, delta-hedged) = exact mirror: Sharpe -1.95, skew 1.26 (positive), pays carry, wins the tail.

### Per-OOS-year annualized Sharpe (short straddle)
| year | Sharpe |
|---|---|
| 2020 | 0.90 |
| 2021 | 5.01 |
| 2022 | 1.04 |
| 2023 | 3.86 |
| 2024 | 1.35 |
| 2025 | 1.54 |

### Worst 5 months (short straddle — the gamma tail you eat)
| month | implied | realized | P&L/notional |
|---|---|---|---|
| 2020-03 | 33 | 91 | -0.05449 |
| 2025-04 | 22 | 53 | -0.04773 |
| 2018-02 | 13 | 29 | -0.02268 |
| 2020-02 | 18 | 26 | -0.01773 |
| 2024-07 | 12 | 15 | -0.01653 |

## CRITICAL CAVEAT — this Sharpe is the OPTIMISTIC bracket
- Implied vol is held FIXED at the month-start VIX, so this captures only the pure GAMMA (realized-vol) harvest held to expiry — it NEVER marks the intra-month VEGA spike. In 2020-03 VIX went 33->80 mid-month; a real short straddle takes a mark-to-market / margin-call hit on that, which this sim omits. That is why |MDD| here is only 8% and skew -1.26 looks mild.
- The companion **variance-swap proxy** (`2026-08-04-variance-risk-premium-probe`) is the PESSIMISTIC bracket: Sharpe 0.31, |MDD| 72%, skew -7.4 — closer to the margin-call experience.
- **The truth is bracketed between them:** the VRP gamma harvest has a genuinely decent Sharpe (~2.0) IF you can survive the vega marks — but surviving the vega marks (and margin) is exactly what destroys short-vol books in a crash. Neither number alone is honest; the pair is.
## Read
- Long gamma (the 'volatile market' play) is the mirror: pays every calm month, wins only in vol spikes.
- Not expressible in the equity-only, long-only stack; needs option-chain data + a hedge engine + an options broker + a negative-gamma/margin risk regime. See the feasibility scope.
- Suggest-only; nothing applied.
