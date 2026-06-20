---
description: /denoise — cut a research firehose down to the names worth a deep-dive. Default - processes the screenshots in research/screenshots/ (dedupe -> extract tickers skeptically -> applicability screen -> log -> delete the images). Ticker-list mode - "/denoise AMD INTC LITE CRDO OKLO" screens a raw list directly (no images). Returns only the applicable shortlist + dismissed-with-reasons + any overt pumps to avoid. Triage only - never trades, never replaces the trade-researcher -> trade-skeptic -> risk-and-compliance pipeline.
---

# /denoise — research firehose triage

Dispatch the **`denoise`** subagent (`.claude/agents/denoise.md`) to cut noise
down to signal. Two modes, decided by `$ARGUMENTS`:

1. **Screenshot mode (no args, default):** the subagent processes the images in
   `research/screenshots/` — dedupes by hash, reads the unique set, extracts
   tickers + source + claim skeptically (strips PII, flags pumps), runs
   `scripts/screen_tickers.py` on the surfaced tickers, appends one consolidated
   entry to `research/screenshots/_log.md`, then **deletes the images**.
2. **Ticker-list mode (args present):** if `$ARGUMENTS` contains tickers (e.g.
   `/denoise AMD INTC LITE CRDO OKLO`), pass them straight to the subagent to
   screen — no image reading, no log/delete. Optionally forward a `--cap N` flag
   if the operator is testing a wider stop cap.

## What to do

1. If `$ARGUMENTS` is empty, first check the folder has images:
   `ls research/screenshots/*.jpeg research/screenshots/*.jpg research/screenshots/*.png 2>/dev/null`.
   If none, tell the operator the folder is empty and stop (nothing to denoise).
2. Invoke the `denoise` subagent via the Agent tool. In screenshot mode the
   prompt is "denoise the screenshots in research/screenshots/". In ticker-list
   mode pass the tickers verbatim: "denoise these tickers: <$ARGUMENTS>".
3. Relay the subagent's result to the operator: the **applicable shortlist**
   (cleanest entry flagged), a compact **dismissed** summary (grouped by reason),
   the **avoid** list (overt pumps), and the **one recommendation** for the full
   pipeline — or a plain "0 of N applicable" if nothing passed.

## Guardrails

- Triage only — passing the screen is necessary, not sufficient. Never imply a
  buy; anything worth pursuing goes through the full deep-dive pipeline next.
- The subagent owns log-before-delete + PII discipline (no balances/account
  numbers/operator name ever logged or returned). Don't override either.
- Do not deep-dive or place anything from this command — surface the shortlist
  and let the operator pick what to send to `trade-researcher`.
