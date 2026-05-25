---
description: /auto-paper-perf — paper-auto performance dashboard. Reads closed paper-auto ledgers, computes realized TradeStats + ReturnStats per setup, compares against backtest expectations from tools/deployable_setups.yml, and pulls current unrealized P&L on open positions from Tiger. Read-only; emits a Markdown table. Answers "is the live edge holding vs the backtest?" Supports --setup <SEPA-VCP|EP>.
---

# /auto-paper-perf — Paper-Auto Performance Dashboard

You are running the paper-auto performance dashboard. **Read-only — no file writes; no broker writes.** Read-only across both tracks; the human-discretionary track is never consulted.

This command answers the question that justifies the paper-auto track existing at all: *are the deployable strategies actually working live, or did the walk-forward backtest overstate the edge?* It compares realized paper-trade results against the rolling-walk-forward verdicts in `tools/deployable_setups.yml` and flags meaningful edge erosion.

## $ARGUMENTS parsing

- `--setup <name>` — restrict realized stats to one setup (e.g. `SEPA-VCP`, `EP`). The per-setup comparison table still shows all deployable setups.
- `--risk-per-trade <fraction>` — equity-curve construction fraction (default 0.01 = 1%). Lower = more conservative Sharpe.
- `--tiger-props-dir <path>` — override the default Tiger credentials directory (default: `$TIGER_PROPS_DIR` or `C:/Users/User/Desktop/tiger/`).

## Step 1 — Run the performance calculator

```python
from tools.auto_paper.performance import compute_performance, compute_open_pnl
report = compute_performance(setup_filter=<args.setup or None>,
                              risk_per_trade=<args.risk_per_trade or 0.01>)
open_pnl = compute_open_pnl()   # constructs TigerClient() internally
```

`compute_performance` reads `journal/paper-auto/positions.json` + each per-ticker ledger. Closed ledgers contribute to realized stats only if they have `position_state.exit_price` set (Session 3's close-out writer). Closed ledgers without `exit_price` (DAY-expired unfilled, or pre-Session-3 closes) are flagged in `report.notes`, not counted toward realized.

`compute_open_pnl` calls `TigerClient().positions()` and intersects with paper-auto's open positions. If Tiger is unreachable, it returns `error` populated + zero totals — the dashboard still renders.

## Step 2 — Render the report

Compose this Markdown:

```
**Mode:** auto-paper performance  |  Asof: YYYY-MM-DD HH:MM ET
**Track:** paper-auto

### 1. Headline

- Closed trades: <report.n_realized>
- Open positions: <report.n_open> (in starter, unrealized $<open_pnl.total_unrealized_pnl_usd>)
- Submitted: <report.n_submitted> (pending fill)
- Realized cumulative R: <sum of report.realized_trades r_multiple>
- Realized P&L $ (at <risk_per_trade*100>% risk per trade): <synthetic from cumulative_return_pct>

### 2. Realized vs backtest expectation

| Setup | n | Realized Sharpe | Backtest Sharpe | Δ | Realized DD | Backtest DD | Δ | Status |
| <setup> | <n> | <realized_sharpe.2f> | <backtest_sharpe.2f> | <sharpe_delta.2f> | <realized_dd.1f%> | <backtest_dd.1f%> | <dd_delta.1f%> | <emoji + note> |
| TOTAL | <n_all> | <overall_sharpe.2f> | — | — | <overall_dd.1f%> | — | — | — |

Status emoji per `comparison.status`:
- `ok`     → ✅
- `warn`   → ⚠
- `fail`   → ❌
- `no_data` → ◻ (no trades yet for this setup)

Tolerance bands defined in `tools.auto_paper.performance` constants:
- ✅ realized within 25% of backtest Sharpe AND n ≥ 30
- ⚠ realized within 50% of backtest OR n < 30
- ❌ realized < 50% of backtest AND n ≥ 30 — meaningful edge erosion

### 3. Per-trade detail (closed)

| Ticker | Setup | Grade | Fill | Exit | R | Days held | Exit reason |
| <one row per t in report.realized_trades> |

If the list is long (>20), show the 10 most recent.

### 4. Open positions (with current unrealized P&L from Tiger)

| Ticker | Setup | Entry | Current | Unrealized $ | Unrealized % | Stop | Days open |
| <one row per p in open_pnl.by_position> |

If `open_pnl.error` is non-null: surface the error string; emit a single row noting Tiger was unreachable; do NOT crash.
If `open_pnl.missing_quotes` is non-empty: emit a "missing broker quotes for: <tickers>" note — these positions are recorded in the paper-auto ledger but not currently held at the broker (e.g. a previous close-out not yet reflected in positions.json).

### 5. Notes

Render each entry in `report.notes` as a bullet. Always include:
- "Equity curve simplification: Sharpe / max-DD computed via trade-sequence equity at <risk_per_trade>%; intra-trade drawdown not captured."
- Any "<TICKER>: closed ledger has no exit_price …" entries — these are positions that Session 3's close-out path should have written; flag for manual inspection.

## Step 3 — Deliver

### If a Telegram channel tag is in the immediate conversation context

The original message arrived from Telegram. Capture `chat_id` from the `<channel source="telegram" chat_id="..." message_id="...">` tag.

Reply via `mcp__plugin_telegram_telegram__reply`:
- `chat_id`: from the channel tag
- `text`: the Markdown report

Telegram caps at 4096 bytes. If exceeded, split sections: first message = Headline + Realized-vs-backtest table; second message = per-trade detail + open positions; third (only if needed) = notes.

### If no Telegram channel tag (running from the IDE)

Print the report directly. Do not invoke any Telegram tool.

## Step 4 — Suggest follow-up (do NOT execute)

Append a single-line "what to do" hint based on what surfaced:

- ❌ status on any setup → "Edge erosion on <setup> — consider pulling it off `tools/deployable_setups.yml` until walk-forward re-verifies. Update `project_swing_phases` memory's verdict table."
- ⚠ status with n < 30 → "Verdict preliminary; need <30 - n> more closed trades for a meaningful read."
- Notes contain "no exit_price" → "<N> closed ledger(s) missing `position_state.exit_price`. Session 3's close-out writer may have skipped them — manual inspection via `cat ledgers/paper-auto/<TICKER>.yml`."
- `open_pnl.error` populated → "Tiger paper account unreachable; rerun later or check `C:/Users/User/Desktop/tiger/tiger_openapi_config.properties`."

DO NOT auto-promote, auto-park, or auto-edit anything. Performance reporting is observational — decisions belong to Bertrand.

## Guardrails

- **Read-only.** Do not Write or Edit any ledger or positions.json. Do not call `TigerClient.place_limit_*` or `cancel`. The dashboard is observational.
- **Paper-only.** `TigerClient()` defaults to `allow_live=False`; the performance module never overrides.
- **Track-bound.** Never reads `journal/positions.json` or `ledgers/positions/`. Same boundary as `/auto-paper`.
- **Empty-track is a valid outcome.** No closed trades yet → empty headline + status=no_data rows. Still emit the dashboard; the no_data state IS the report.
- **Sensitive information** — account number is masked by `TigerClient` (`account_masked`). Never reconstruct full PII in the report.
- **Telegram parse-mode**: plain text (no `parse_mode`). The tables have pipes that would break Markdown rendering on Telegram.
- **No trade recommendations.** Edge erosion is information; the response is to verify with backtest, NOT to override the deployable list ad-hoc.

## What's NOT done by this command

- **Bar-by-bar intra-trade drawdown.** The Sharpe / max-DD use a trade-sequence equity curve at fixed risk-per-trade; the bar-by-bar simulator used in backtests is more accurate but requires per-bar OHLCV we don't store on paper-auto ledgers.
- **Rolling Sharpe / setup decay detection.** A point-in-time number, not a rolling series. Comparing across `--asof` snapshots is manual.
- **Per-grade attribution** (A+/A/B/C/SuperSwan/etc.). Available via `report.by_setup_*` but not in the default table — drill in by querying `compute_performance()` directly.
- **Factor analysis.** Out of scope for v1.
