---
description: /watch-check — run the entry-trigger watcher once. Evaluates every watchlist entry that has a structured `trigger` block against live prices + the SPY regime, and pushes a Telegram alert when a name escalates toward a buyable entry (approaching → in_zone → fired). Advisory only — never trades. Default pushes on state-change; pass `--dry-run` to evaluate + print without pushing. This is the same command the hourly cron fires.
---

# /watch-check — entry-trigger watcher

Run the deterministic entry watcher (`tools.watcher.run`). It is the
advisory "tap on the shoulder" layer: instead of the operator coming back to
prompt a check, this watches the watchlist's structured triggers and pings
Telegram when a setup actually reaches its buy zone / breakout.

## What to do

1. Run it (pass through `$ARGUMENTS`, e.g. `--dry-run`):
   ```bash
   uv run python -m tools.watcher.run $ARGUMENTS
   ```
2. Relay the per-ticker status table (status + price + reason) and call out
   anything that PUSHED (escalated to approaching / in_zone / fired) this run.
3. If a name FIRED, surface its entry / stop / target / R:R from the trigger
   payload so the operator can act — but do NOT place the trade (discretionary
   track requires manual confirmation).

## How it works (for context)

- Reads `journal/watchlist.json` entries that carry a structured `trigger`
  block (`pullback_zone` / `breakout`; an entry may carry a list of both).
  Legacy free-text-only entries are skipped.
- Composes the existing deterministic detectors (`pullback_detect` for the
  reversal candle, volume ratio for the breakout, `regime_check`/`trend_template`
  for the SPY stage gate). No new market math.
- Edge-triggered + de-duped via `journal/watcher/_state.json`: a name that stays
  in the same state pages once, not every hour; a `fired` window re-pages once
  per ET day.
- Regime gate: a would-be entry is BLOCKED (not fired) when SPY is stage 3/4.

## Guardrails

- **Advisory only.** It surfaces where to enter; it never places an order.
  Anything it fires still goes through the operator's manual confirmation.
- **Don't edit `tools/watcher/` from this command.** If a run errors, surface
  the stderr — don't patch.
- To add a name to the watcher, give its watchlist entry a structured `trigger`
  block (zone / level + entry / stop / target). Without one, the watcher ignores it.
