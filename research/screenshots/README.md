# Research screenshots — drop folder (log-then-delete)

Drop chart / broker / news screenshots here. I **extract the research into
`_log.md`, then delete the image** — so the folder never accumulates binary bloat
and the durable record is plain text I (and you) can grep.

## The workflow

1. **Drop the image(s)** here (PNG / JPG). Name with the ticker prefix so I
   auto-associate: `MSFT_daily.png`, `NBIS_chart.png`, `CRWV_news.jpg`.
2. **Ping me** — "log the screenshots" / "check the MSFT shot".
3. **I parse → append a dated entry to `_log.md`** (ticker, source type, the
   extracted facts/levels, any action) → **delete the image.**
4. The text log is what persists. The image is gone once logged.

I won't delete an image until its content is safely written to `_log.md`. If I
can't read one, I leave it and flag it.

## What gets logged (per source type)

- **Chart shots** → price, the visible MA/structure + levels (support/resistance,
  trend), then I cross-check against live `atr_compute` / `trend_template` before
  any call. The image is the prompt; the tools are the truth.
- **Broker panels** → tickers / shares / fills / P&L. **Account numbers, full
  names, and other PII are NEVER written to the log or echoed back** (per
  CLAUDE.md § Sensitive Information).
- **News / analyst tables** → the claim + source, logged as a *lead to verify*,
  not as fact on its own.

## Housekeeping

- `_log.md` + this README are the only things that live here long-term.
- The whole folder is **gitignored** (local research scratch; may reference broker
  positions) — the log stays on this machine, not in git history.
- A full deep-dive still lands in `ledgers/candidates/<date>/<TICKER>.yml`; a watch
  item in `journal/watchlist.json`. `_log.md` is the lightweight scratch layer
  that feeds those.
