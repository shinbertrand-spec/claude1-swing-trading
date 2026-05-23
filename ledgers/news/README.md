# News Snapshots

Hourly point-in-time market-context store produced by the `news-research`
subagent and consumed by the morning-routine agents (`trade-researcher`,
`risk-and-compliance`) and the Telegram push relay.

This is a **parallel artifact** to the per-ticker fact ledger at
[`../candidates/`](../candidates) / [`../positions/`](../positions). News
snapshots are global (multi-ticker, time-windowed); fact ledgers are
per-trade-lifecycle. The two do not cross-write — see "Why no cross-write"
below.

Source of truth schema:
[`_schema/news_snapshot.schema.json`](_schema/news_snapshot.schema.json) (JSON
Schema 2020-12).

---

## Directory layout

```
ledgers/news/
  _schema/
    news_snapshot.schema.json   # JSON Schema for structural validation
  _examples/
    quiet-hour.yml              # No material deltas, just hourly heartbeat
    catalyst-hour.yml           # Analyst action + gap + macro event firing
  YYYY-MM-DD/
    HH.yml                      # One file per hour (UTC HH, top of hour)
    HH-summary.txt              # Telegram-ready summary (only when material_deltas non-empty)
  README.md                     # this file
```

Snapshots are dated and hour-stamped; we keep them indefinitely (delete /
archive in Phase 2 when the directory gets noisy).

---

## When snapshots are written

A snapshot is written once per hour by `/news-hourly` (slash command,
typically driven by Windows Task Scheduler) during US market hours. Pattern:

| Phase | Time (ET) | Snapshots written |
|---|---|---|
| Premarket | 08:00, 09:00 | optional; off by default in Phase 1 |
| Regular | 09:30, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00 | required |
| Afterhours | 17:00 | optional; off by default |

Phase 1 enables the regular-hours fires only. Premarket / afterhours can
be added by editing the scheduled-task triggers without changing any code.

---

## Subagent pipeline

`news-research` is a single subagent invocation that runs four sequential
internal passes:

1. **Scout** — gather news / price / analyst-action for tickers in
   [`watchlist.json`](../../journal/watchlist.json) + open positions
   (`journal/positions.json`). Per-ticker news is pulled from
   [finviz.com/quote.ashx?t=TICKER](https://finviz.com/quote.ashx) (has a
   per-ticker news panel with sources and timestamps) and cross-referenced
   against the aggregated feed at [biztoc.com](https://biztoc.com/).
   Record the immediate publisher in `source` (e.g. `web:reuters.com`),
   not the aggregator, when the aggregator links out.
2. **Top-movers** — query for US gainers/losers > 5% intraday; cross-ref
   against watchlist + positions to identify NEW names (potential EP gap
   candidates the morning scan hasn't seen yet). Primary screener source
   is [finviz.com/screener.ashx](https://finviz.com/screener.ashx) (filter
   `ta_change_u5` for gainers ≥ 5%); also pull headline context from
   [biztoc.com](https://biztoc.com/) and [finviz.com/news](https://finviz.com/news).
3. **Bear / skeptic** — for any item flagged `severity ≥ medium`, search
   for disconfirming sources on different domains than the original.
4. **Synth** — compose the hourly YAML snapshot + select items for the
   `material_deltas` array, which drives Telegram pushes.

The orchestrator (`/news-hourly`) then diffs the new snapshot against the
prior hour's snapshot, populates `delta_vs_prior` on each
`per_ticker_item`, and writes the `HH-summary.txt` file if and only if
`material_deltas[]` is non-empty.

---

## Section reference

### `meta`

| Field | Notes |
|---|---|
| `schema_version` | const `"1.0"` |
| `snapshot_id` | ISO timestamp at top of hour (matches filename) |
| `asof` / `fetched_at` | When the snapshot represents / when gather completed |
| `ticker_count` | Distinct tickers across `per_ticker_items` + `top_movers` |
| `prior_snapshot_id` | For delta detection. Null on first snapshot of session |
| `session` | `premarket` / `regular` / `afterhours` / `closed` |
| `created_by` | Agent / skill name |

### `market_context`

SPY, QQQ, optional IWM, VIX (with `regime: calm / elevated / stressed /
panic`), and optional `sector_etfs[]`. Each quote has `change_pct` (today)
and `vs_prior_snapshot_pct` (delta since prior hour). VIX regime
thresholds: < 15 calm, 15-20 elevated, 20-30 stressed, > 30 panic.

### `per_ticker_items[]`

The bulk of the snapshot. Each item:

| Field | Notes |
|---|---|
| `ticker` / `type` | type ∈ {analyst_action, sec_filing, news, gap, macro, earnings_revision, price_action} |
| `source` / `url` / `title` / `summary` | Provenance + one-sentence summary |
| `severity` | low / medium / high |
| `delta_vs_prior` | new / unchanged / escalated / resolved |
| `in_position` / `in_watchlist` | Membership flags |
| `bear_check` | Populated when `severity ≥ medium`; lists disconfirming sources from different domains |

### `top_movers[]`

US gainers/losers > 5% intraday. NEW names (not in watchlist or positions)
flagged as `potential_ep` / `gap_up` / `gap_down` for human review.
Phase 1 source is `WebSearch` heuristics; Phase 2 may add Finviz or Alpaca
screener.

### `macro_events[]`

Scheduled or breaking macro events — Fed speak, FOMC release, CPI, NFP.
`relevant_tickers[]` tells the synth pass which positions or watchlist
names this event ought to wake up.

### `material_deltas[]`

What gets pushed to Telegram this hour. Each entry has a `reason` keyed to
the Phase 1 threshold table below and a one-sentence `summary` ready to
drop into the message body. Empty array = no push.

---

## Material-delta thresholds (Phase 1 baseline; tune after first day's data)

| `reason` | Trigger | Threshold |
|---|---|---|
| `analyst_action_watched` | Analyst PT or rating change on watched ticker | Any |
| `gap_watched` | Gap on watched ticker | ≥ 5% (open or intraday) |
| `sector_move` | Sector ETF intraday move | ± 2% |
| `filing_watched` | FDA / M&A / 8-K on watched ticker | Any |
| `fed_speak` | Fresh Fed-speak / FOMC release | Any |
| `fomc_release` | FOMC statement / minutes | Any |
| `top_mover_new` | NEW gainer not in watchlist/positions | ≥ 10% gap on high volume |
| `position_news` | Any news touching an open position | Any |

"Watched" = present in `journal/watchlist.json` or `journal/positions.json`.
Push criteria are deliberately conservative for Phase 1; we expect to
loosen `sector_move` and tighten `top_mover_new` after observing one full
day's noise floor.

---

## Why no cross-write into the fact ledger

When `news-research` finds analyst action on a ticker that has a candidate
ledger from today, it does NOT update that ledger's `catalyst` or
`reasoning_trace`. Reasons:

1. `risk-and-compliance` runs the 5-gate verification on a stable ledger
   snapshot. Mid-routine mutations from a parallel subagent would create
   race conditions in trace_audit and freshness checks.
2. The fact-ledger schema is per-trade-lifecycle. Hourly heartbeat data
   doesn't fit its shape.
3. Provenance is cleaner: trade-researcher's trace_refs cite tool outputs
   it ran itself; news-research's items live in their own snapshot.

If a news item should influence a candidate, it surfaces via the morning
routine — Bertrand sees the Telegram push, re-runs `/morning-deep-dive`,
trade-researcher refetches and rebuilds the ledger fresh.

---

## Provenance & staleness

Each `per_ticker_item.source` follows the same `source_tag` convention as
the fact ledger: `web:<domain>`, `sec_filing`, `fed_release`,
`broker_api`, `tool:<name>`, `manual`. URLs are required for `news`,
`analyst_action`, `sec_filing` types.

Snapshots have no `fetched_at` staleness rule — they ARE the timestamp.
The orchestrator's job is to fire on schedule. A missing `HH.yml` for an
hour during regular session = the cron didn't run, not stale data; the
PowerShell wrapper's stdout status code surfaces this.

---

## Telegram push format

`HH-summary.txt` is plain text, ≤ 4096 bytes (Telegram message limit). One
line per `material_delta`, leading with the ticker (or `MACRO`/`SECTOR`
for non-ticker items) and `[severity]` tag:

```
[news] 14:00 ET 2026-05-19 — 3 material deltas

NVDA  [high]  Wedbush PT 200 (was 175), Outperform reiterated — web:wedbush.com
TSLA  [med]   Gap −6.4% on Reuters Shanghai-recall report — web:reuters.com
MACRO [med]   Powell at Jackson Hole 14:00 ET — fed_release
```

The PowerShell relay (`scripts/send-news-to-telegram.ps1`) reads this file
and POSTs it via the Telegram Bot API.

---

## Related

- [`_schema/news_snapshot.schema.json`](_schema/news_snapshot.schema.json) — machine-checkable schema
- [`../README.md`](../README.md) — per-trade fact ledger spec (different artifact, same provenance discipline)
- [`../../journal/watchlist.json`](../../journal/watchlist.json) — tracked-but-not-positioned tickers feeding the Scout pass
- [`../../journal/positions.json`](../../journal/positions.json) — open positions feeding the Scout pass
- [`../../.claude/agents/news-research.md`](../../.claude/agents/news-research.md) — subagent definition
- [`../../.claude/commands/news-hourly.md`](../../.claude/commands/news-hourly.md) — slash-command orchestrator
- [`../../scripts/send-news-to-telegram.ps1`](../../scripts/send-news-to-telegram.ps1) — Telegram relay
