# Morning scan blocked — 2026-07-13 (SIXTH consecutive)

**Status:** `/morning-scan-telegram` headless run skipped at pre-flight. The
`Write(journal/**)` / `Edit(journal/**)` deny in
`C:\Users\User\Desktop\Claude1\.claude\settings.local.json` is still active
(verified by live Write attempt to `journal/candidates/2026-07-13-summary.txt`
— denied). No candidate scan was run; nothing for the Telegram wrapper to push.

**Impact:** Seven trading days of morning candidates now undelivered:
07-02, 07-06, 07-07, 07-08, 07-09, 07-10, 07-13 (07-03 was the July-4
observed holiday). Last successful delivery: **2026-07-01**.

**Fix (requires Bertrand at the IDE):** open
`.claude/settings.local.json` and REMOVE or NARROW these two lines in
`permissions.deny`:

```json
"Write(journal/**)",
"Edit(journal/**)",
```

Deny takes precedence over allow, so adding
`"Write(journal/candidates/**)"` to the allow list does NOT work while the
broad deny stands — the deny itself must be narrowed, e.g. replace with:

```json
"Write(journal/positions.json)",
"Edit(journal/positions.json)",
```

(if the intent was to protect the positions index) or delete the two lines
entirely if the lockdown is no longer needed. Note the same deny also breaks
`/eod-journal`, `/auto-paper` (journal/paper-auto/), and `/news-hourly`
summary relays.

**Prior incidents:** `2026-07-07`, `2026-07-08`, `2026-07-09`, `2026-07-10`
notes in this directory. Memory:
`project_journal_write_deny_breaks_headless.md`.
