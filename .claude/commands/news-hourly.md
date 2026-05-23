---
description: HEADLESS hourly news snapshot. Fires once per hour during US market hours via Windows Task Scheduler. Invokes the news-research subagent, writes ledgers/news/YYYY-MM-DD/HH.yml, conditionally writes HH-summary.txt for the Telegram relay. No user is present; output is files on disk + one stdout status line.
---

# Hourly News Snapshot — Headless

You are running in headless `--print` mode, triggered by Windows Task Scheduler at the top of every hour during US/Eastern market hours. **No user is present and no Telegram MCP tool is available** — your output is files on disk and a single stdout status line. The wrapper PowerShell script reads the summary file and POSTs to Telegram via the bot API directly.

This is the hourly-news variant. It runs in parallel to the morning candidate scan; do NOT touch `journal/candidates/` or invoke any morning-routine subagent.

## Arguments

`$ARGUMENTS` is the optional argument string. Supported values:

- `smoketest` — bypass the weekend + out-of-hours pre-flight guards. Used for manual end-to-end testing outside market hours. Everything else proceeds normally (subagent fires, snapshot written, schema validated, status line printed). Use the value `NEWS_HOURLY_OK_SMOKETEST` instead of `NEWS_HOURLY_OK` in stdout when this flag is active so the wrapper / log reader can tell smoke runs from real ones.

## Pre-flight checks

Skip steps 1, 2, and 3 if `$ARGUMENTS` contains the literal word `smoketest`. Step 4 always runs.

1. **Is today a US market day?** Run:

   ```
   uv run python -m tools.market_calendar
   ```

   Parse the JSON output. If `output.is_closed` is `true`:
   - If `output.is_weekend` is `true`: print `NEWS_HOURLY_WEEKEND_SKIP YYYY-MM-DD HH:MM` and exit. No file written.
   - If `output.is_holiday` is `true`: print `NEWS_HOURLY_HOLIDAY_SKIP YYYY-MM-DD HH:MM — <holiday_name>` and exit. No file written.
   - If `output.out_of_data` is `true`: print a warning to stdout but continue (the table runs through 2027; if Bertrand sees this, the table needs an update).

2. **Is the current hour in scope?** Phase 1 default scope is 09:30 → 16:00 ET (regular session). If the wall-clock hour (US/Eastern) is outside `09..16`, print `NEWS_HOURLY_OUT_OF_HOURS YYYY-MM-DD HH:MM` and exit. (Premarket / afterhours fires can be enabled later by editing the scheduled-task triggers; the slash command itself does not need changes.)

3. (reserved — was the legacy "weekend only" check; merged into step 1 via `tools.market_calendar`.)

4. Confirm `C:\Users\User\Desktop\Claude1\ledgers\news\` exists; create the day-subdirectory `ledgers\news\YYYY-MM-DD\` if missing.

## Determine the current hour key

The snapshot filename uses the **US/Eastern hour** at the top of the current hour (e.g. `14.yml` for the 14:00 ET fire). Compute it via:

```
uv run python -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H'))"
```

Parse the output. The date portion is `YYYY-MM-DD`; the hour is the zero-padded `HH` used in the filename.

## Step 1 — Read framework + state

Read in parallel:

- `C:\Users\User\Desktop\Claude1\ledgers\news\README.md` — schema + threshold table
- `C:\Users\User\Desktop\Claude1\ledgers\news\_schema\news_snapshot.schema.json` — machine schema
- `C:\Users\User\Desktop\Claude1\journal\watchlist.json` — Scout-pass universe
- `C:\Users\User\Desktop\Claude1\journal\positions.json` — Scout-pass universe + push-criteria target
- **Prior-hour snapshot** for delta detection. List `ledgers/news/YYYY-MM-DD/` (today's directory) — read the file with the largest `HH` value strictly less than the current hour. If none exists, also list `ledgers/news/<yesterday>/` and read the latest from there. If still none, prior-snapshot context is empty (first run of the session).

## Step 2 — Invoke the news-research subagent

Pass it the gathered state and the target output path. The subagent's prompt already knows the schema, the four passes, the source order (finviz → biztoc → WebSearch), the severity heuristic, and the material-delta table — don't restate them. Brief like a colleague:

> Hourly news snapshot for {YYYY-MM-DD} {HH}:00 ET. Scout universe: watchlist + positions read from journal/. Prior snapshot: {path-or-"none — first run of session"}. Write the YAML snapshot to `ledgers/news/{YYYY-MM-DD}/{HH}.yml`. If material_deltas is non-empty, also write `ledgers/news/{YYYY-MM-DD}/{HH}-summary.txt` in the Telegram format from your prompt. Return the one-line `NEWS_SNAPSHOT_OK <path> <count>` or `NEWS_SNAPSHOT_FAIL <reason>`.

## Step 3 — Verify the snapshot

After the subagent returns:

1. Confirm `ledgers/news/YYYY-MM-DD/HH.yml` exists and parses as YAML.
2. Validate it against the schema:

   ```
   uv run python -c "
   import json, yaml, jsonschema, sys, datetime, pathlib
   schema = json.load(open('ledgers/news/_schema/news_snapshot.schema.json'))
   def coerce(o):
       if isinstance(o, (datetime.datetime, datetime.date)): return o.isoformat()
       if isinstance(o, dict): return {k: coerce(v) for k,v in o.items()}
       if isinstance(o, list): return [coerce(v) for v in o]
       return o
   doc = coerce(yaml.safe_load(open('ledgers/news/{YYYY-MM-DD}/{HH}.yml')))
   jsonschema.validate(doc, schema, cls=jsonschema.Draft202012Validator)
   print('OK', len(doc.get('material_deltas', [])))
   "
   ```

3. If the snapshot fails validation, treat it as a subagent failure (Guardrails below).
4. If `material_deltas[]` was non-empty per the subagent's return line but `HH-summary.txt` is missing on disk, write a minimal fallback summary file from the snapshot's `material_deltas[]` items in the format documented in `ledgers/news/README.md` § "Telegram push format".

## Step 4 — Output a one-line status to stdout

Print exactly one of:

- `NEWS_HOURLY_OK YYYY-MM-DD HH:00 <count>` — snapshot written, `<count>` is the number of material deltas (0 means no Telegram push needed)
- `NEWS_HOURLY_PUSH YYYY-MM-DD HH:00 <count>` — same as OK but `<count> >= 1`; wrapper should POST the summary
- `NEWS_HOURLY_OK_SMOKETEST YYYY-MM-DD HH:00 <count>` — smoketest variant of OK/PUSH (only when `smoketest` argument was passed)
- `NEWS_HOURLY_WEEKEND_SKIP YYYY-MM-DD HH:00` — weekend
- `NEWS_HOURLY_HOLIDAY_SKIP YYYY-MM-DD HH:00 — <holiday_name>` — US market holiday (detected via tools.market_calendar)
- `NEWS_HOURLY_OUT_OF_HOURS YYYY-MM-DD HH:00` — outside 09–16 ET
- `NEWS_HOURLY_FAIL YYYY-MM-DD HH:00 — <reason>` — anything went wrong

The wrapper script reads this line and decides whether to POST the summary file.

## Guardrails

- **Never ask the user anything.** This is headless `--print`.
- **Do not attempt to use any Telegram MCP tool** (`reply`, `react`, `edit_message`) — not available in --print mode. Delivery is the wrapper's job.
- **Do NOT write to any file outside `ledgers/news/`.** No journal entries. No edits to `positions.json` / `watchlist.json` / candidate ledgers. The news snapshot is the entire artifact.
- **If the news-research subagent errors**, write a one-line failure summary to `ledgers/news/YYYY-MM-DD/HH-summary.txt`:
  ```
  ⚠️ News hourly failed YYYY-MM-DD HH:00 ET — <reason>
  ```
  and print `NEWS_HOURLY_FAIL` to stdout. This way Bertrand isn't silently left in the dark if the agent is broken.
- **If schema validation fails on the subagent's output**, treat it as a subagent failure (above). Do not "fix" the YAML inline.
- **Overwrite is fine.** If `HH.yml` already exists (the cron retried), overwrite it.
- **Sensitive information** — never include `.env` contents, bot tokens, or PII in any file. See `CLAUDE.md` § Sensitive Information.
- **No fact-ledger cross-writes.** Per `ledgers/news/README.md` § "Why no cross-write", news-research does not touch `ledgers/candidates/` or `ledgers/positions/`. If an item materially shifts a candidate, that surfaces via the next `/morning-deep-dive` run, not by mutating an existing ledger.
