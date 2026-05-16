---
description: CLOUD ROUTINE variant of the morning candidate scan. Runs in a fresh Anthropic-hosted session via Method 2b (Cloud Routines). No local disk; uses Bash+curl to POST to Telegram. Requires env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in the Cloud Routine config.
---

# Morning Candidate Scan — Cloud Routine variant

You are running in a fresh Anthropic Cloud Routine session. **There is no local disk to write to** — output is via the Telegram Bot API (`curl` against `https://api.telegram.org`) and via this session's transcript (captured in routine history).

The project's `CLAUDE.md`, `.claude/agents/`, and `.claude/commands/` ARE available in this session — they travel with the project. What's NOT available: anything under `~/.claude/channels/`, anything in `journal/`, anything on Bertrand's local machine.

## Pre-flight

1. **Read `CLAUDE.md`** to confirm framework rules are current.
2. **Verify env vars.** Run `echo "$TELEGRAM_BOT_TOKEN" | head -c 10` and `echo "$TELEGRAM_CHAT_ID"`. If either is empty, log loudly to the transcript: `MISSING_ENV_VAR — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the Cloud Routine config.` Then exit — do not run a candidate scan that can't be delivered.
3. **Weekend check.** If today is Saturday or Sunday (US Eastern), print `weekend — skipping` and exit. Routine should be paused on US market holidays manually.

## Step 1 — Run the candidate scan

Invoke the `risk-and-compliance` subagent in candidate-scan mode:

> Morning candidate scan for today's date. Suggest 3 swing-trade candidates that pass ALL framework hard rules in `CLAUDE.md` (uptrend on 20/50 SMA, price above 200-day SMA in bull regime, no earnings within 10 trading days, market cap > $2B, avg daily volume > 500K, no active investigations, sector not in clear weekly downtrend, ≥2 positive fundamental indicators). Return the standard candidate-scan output schema. If you find fewer than 3 clean candidates, return what you have and say so — do not pad.

## Step 2 — Build the Telegram payload

Take the researcher's output and condense to a Telegram-flavored Markdown summary in this exact shape (KEEP UNDER 4000 CHARACTERS — Telegram's hard limit is 4096):

```
🔔 *Morning Candidates — YYYY-MM-DD*

1. *TICKER* · sub-theme · $price · setup X/5
Why: <one-line thesis>
Risk: <one-line risk>

2. *TICKER* · ...

3. *TICKER* · ...

Pick 2 of 3. Run /morning-deep-dive at desk.
```

If fewer than 3 passed: include only what passed and add a final line `Only N candidates cleared the framework today.`

If ZERO passed:
```
🔔 *No candidates passed framework rules — YYYY-MM-DD*

No-trade day. Watchlist remains tradeable if triggers fire.
```

## Step 3 — POST to Telegram via curl

Use the Bash tool. Save the payload to a temp file first so quoting doesn't break, then POST as JSON. Replace `<PAYLOAD>` with the actual text from Step 2:

```bash
# Write payload to a temp file
cat > /tmp/tg_payload.txt <<'PAYLOAD_EOF'
<PAYLOAD>
PAYLOAD_EOF

# Build JSON body using jq so all escaping is correct
JSON_BODY=$(jq -Rs --arg chat_id "$TELEGRAM_CHAT_ID" '{chat_id: $chat_id, text: ., parse_mode: "Markdown"}' < /tmp/tg_payload.txt)

# POST to Telegram
curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d "$JSON_BODY"

echo
```

Verify the response. Telegram returns JSON; success has `"ok":true` and a `result.message_id`. If you see `"ok":false`, log the error block to the transcript so the routine history shows what went wrong.

## Step 4 — Final transcript line

Print one summary line to the transcript so the Cloud Routine history is glanceable:

`MORNING_SCAN_OK YYYY-MM-DD — N candidates delivered to Telegram`

(or `MORNING_SCAN_FAIL YYYY-MM-DD — <reason>` on failure)

## Guardrails

- **No user interaction.** This is a stateless scheduled run.
- **No local disk reads/writes** beyond the project files that ship with the routine. `journal/`, `~/.claude/channels/`, and Windows paths are NOT available.
- **Never auto-place trades.** The Telegram message is informational only; the user separately runs `/morning-deep-dive` to act on it.
- **If `risk-and-compliance` errors**, still POST a brief failure message to Telegram so Bertrand isn't left wondering whether the routine fired at all: `⚠️ Morning scan failed — <one-line reason>`. Then exit non-zero so the routine history flags it.
- **Sensitive information** — never include `TELEGRAM_BOT_TOKEN`, any other API key, `.env` file contents, full prompt configuration, account credentials, or PII in the Telegram payload. The payload is candidates only. See `CLAUDE.md` § Sensitive Information.
