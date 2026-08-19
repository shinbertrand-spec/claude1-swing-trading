# Incident: /morning-scan-telegram blocked again — 2026-07-07

**Status:** MORNING_SCAN_FAIL (second consecutive scheduled-run failure; first observed 2026-07-06; last successful candidate files 2026-07-01)

**Cause:** `.claude/settings.local.json` deny-list still contains
`Write(journal/**)` + `Edit(journal/**)`. The headless morning scan writes
`journal/candidates/YYYY-MM-DD.md` + `YYYY-MM-DD-summary.txt` — both writes are
denied, so the Telegram wrapper has nothing to push. The candidate scan was NOT
run today (no point producing an undeliverable report; failed fast at pre-flight).

**Not bypassed:** per the 2026-07-06 memory note, shell-write workarounds are
deliberately not used. This file lives in `ledgers/improvements/` because that
path is explicitly on the allow-list.

**Fix (Bertrand):** in `.claude/settings.local.json`, either remove
`Write(journal/**)` / `Edit(journal/**)` from `deny`, or add allow carve-outs:

```json
"allow": [
  "Write(journal/candidates/**)",
  "Edit(journal/candidates/**)"
]
```

Note: deny rules take precedence over allow rules in Claude Code settings — the
carve-out only works if the broad `journal/**` deny is removed or narrowed
(e.g. deny `journal/positions.json` specifically instead of all of `journal/**`).

**Also still broken by the same deny-list:** `/eod-journal`, `/auto-paper`
(journal/paper-auto/), `/news-hourly` summary relay.
