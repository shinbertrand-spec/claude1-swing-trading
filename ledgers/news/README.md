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
    social-active-hour.yml      # Schema 1.2: BOTH social_signals[] AND x_signals[] firing — full sentiment-stack demo
  _state/
    social_baseline.json        # Per-ticker rolling 24-hr StockTwits message-volume baseline (Phase 1.5)
  YYYY-MM-DD/
    HH.yml                      # One file per hour (ET HH, top of hour)
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

`news-research` is a single subagent invocation that runs six sequential
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
3. **Social — StockTwits** (Phase 1.5) — pull StockTwits sentiment for open
   positions + top-movers flagged `potential_ep / news_driven / gap_up / gap_down`.
   Spam-filter, count bullish vs bearish (using StockTwits' built-in user
   tags), compute `volume_z` vs the trailing 24-hr baseline in
   [`_state/social_baseline.json`](_state/social_baseline.json), classify per
   the ladder (`climax_warning` / `bearish_pile_on` / `buzz_spike` / `cooling` /
   `quiet`). Informational only — never gates a trade. Cross-link target:
   `tools.climax_top_detect` in the EOD sell pipeline.
4. **X scanner** (schema 1.2) — invoke `tools.news_research.x_scanner` to
   poll twitterapi.io for cashtag mentions ($TICKER) on watchlist + position
   names. The module runs a deterministic Stage 1 hard-floor filter
   (engagement / author followers / language / recency), invokes a
   subagent-supplied Haiku classifier for Stage 2 (material / sentiment /
   themes), then enforces a top-5-per-ticker cap. Output lands in
   `x_signals[]`. Informational only — same authority as Pass 3. Sibling
   to `tools.thematic_portfolio.corpus.x_ingest` (handle-driven); the same
   tweet appears in BOTH artifacts when an Aschenbrenner-tier handle
   mentions a swing-watchlist ticker, and `x_signals[].cross_consumer_ref`
   points at the thematic ledger entry.
5. **Bear / skeptic** — for any item flagged `severity ≥ medium`, plus any
   `buzz_spike` top-mover from Pass 3, search for disconfirming sources on
   different domains than the original.
6. **Synth** — compose the hourly YAML snapshot + select items for the
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
| `schema_version` | `"1.0"` / `"1.1"` / `"1.2"` (current; new snapshots write 1.2) |
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

### `x_signals[]` (schema 1.2)

Material X (Twitter) posts about watchlist + position tickers, surfaced by
[`tools.news_research.x_scanner`](../../tools/news_research/x_scanner.py).
Cashtag-driven via twitterapi.io's `advanced_search` endpoint with a
three-stage filter:

1. **Stage 1 (deterministic):** engagement floor (likes ≥ 100 OR rts ≥ 20
   OR quotes ≥ 10), author followers ≥ 1000, lang en, not-retweet, past-60min
   recency.
2. **Stage 2 (LLM):** Haiku classifier returns `material` (true/false),
   `sentiment_tag` (bullish / bearish / neutral / breaking-news),
   `named_themes[]`, and a rationale.
3. **Stage 3 (deterministic):** top-5 per ticker by engagement rank,
   dropping `material=false` first.

Each entry carries `cross_consumer_ref.thematic_ledger_path` — non-null
when the same tweet was ALSO ingested by
[`tools.thematic_portfolio.corpus.x_ingest`](../../tools/thematic_portfolio/corpus/x_ingest.py)
(handle-driven sibling consumer; same twitterapi.io credential, different
query pattern).

x_signals are informational only — they never gate a trade. Material
verdicts of `bullish` / `bearish` / `breaking-news` on watched names produce
the three `x_signal_*` material_delta reasons; `neutral` stays in
`x_signals[]` as color but never triggers a push.

### `material_deltas[]`

What gets pushed to Telegram this hour. Each entry has a `reason` keyed to
the Phase 1 threshold table below and a one-sentence `summary` ready to
drop into the message body. Empty array = no push.

---

## Material-delta thresholds (Phase 1 + 1.5 baseline; tune after first day's data)

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
| `social_climax_open_pos` (1.5) | `social_signals[]` entry on an open position with `classification: climax_warning` (bull_share ≥ 0.85 AND volume_z ≥ 2.0) | Any |
| `social_bearish_open_pos` (1.5) | `social_signals[]` entry on an open position with `classification: bearish_pile_on` (bull_share ≤ 0.20 AND volume_z ≥ 2.0) | Any |
| `social_buzz_top_mover` (1.5) | `social_signals[]` entry on a top-mover (`potential_ep`) with `classification: buzz_spike` (volume_z ≥ 3.0) | Any |
| `x_signal_bullish_open_pos` (1.2) | `x_signals[]` entry on an open position with `classifier_result.material: true` AND `sentiment_tag: bullish` | Any |
| `x_signal_bearish_open_pos` (1.2) | `x_signals[]` entry on an open position with `classifier_result.material: true` AND `sentiment_tag: bearish` | Any |
| `x_signal_breaking_news_watched` (1.2) | `x_signals[]` entry on a watched ticker (watchlist / position / top-mover) with `classifier_result.material: true` AND `sentiment_tag: breaking-news` | Any |

"Watched" = present in `journal/watchlist.json` or `journal/positions.json`.
Push criteria are deliberately conservative for Phase 1; we expect to
loosen `sector_move` and tighten `top_mover_new` after observing one full
day's noise floor. The three `social_*` reasons are Phase 1.5; the three
`x_signal_*` reasons are schema 1.2 — thresholds for both will be re-tuned
after the first week of live data lands.

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

**One in-`ledgers/news/` exception:** Pass 3 (Social) mutates
[`_state/social_baseline.json`](_state/social_baseline.json) incrementally
(append current msg_count, trim to last 24, recompute mean/std). This is
state owned by the news pipeline, not a per-trade artifact, so the no-cross-
write rule doesn't apply.

## Cross-link consumers (read-only)

Two routines outside `news-research` read social_signals[] from the latest
snapshot:

- **`eod-journal`** (4:15 PM ET) — joins the social_signals[] block against
  each open position's `climax_top_detect` / `violations_detect` output.
  When a position has BOTH a `social_signals[]` entry classified
  `climax_warning` AND `climax_top_detect` firing, OR a `bearish_pile_on`
  classification, the Step 4 alert surfaces this as confluence — higher-
  conviction sell-evaluation signal. See `Step 3a` in
  [`../../.claude/commands/eod-journal.md`](../../.claude/commands/eod-journal.md).
- **`portfolio-manager snapshot`** (read-only) may optionally surface
  open-position social classifications in its heatmap. Phase 1.5 leaves
  this as a queued enhancement; not wired yet.

Neither modifies `social_signals[]` or any other news file.

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
