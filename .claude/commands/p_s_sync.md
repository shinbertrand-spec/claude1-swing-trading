---
description: /p_s_sync — portfolio sync. Compares journal/positions.json against the live Tiger paper account (read-only). Surfaces drift — positions in Tiger not in journal, positions in journal not in Tiger, share-count / cost-basis mismatches, and (with --include-orders) any open Tiger orders without matching journal positions. Does NOT reconcile — the caller decides whether to onboard, close, or amend. Canonical caption is /p_s_sync (short for portfolio-sync).
---

# /p_s_sync — Portfolio Sync — journal vs Tiger paper

You are running a portfolio-wide drift check. **Read-only — no file writes.**

## $ARGUMENTS parsing

Parse from `$ARGUMENTS` (may be empty):

- `--tiger-props-dir <path>` — override the default Tiger credentials directory (default: `$TIGER_PROPS_DIR` or `C:/Users/User/Desktop/tiger/`). Pass through to the subagent.
- `--include-orders` — also pull `TigerClient.open_orders()` and check for orders without matching journal positions. Useful right after `/morning-deep-dive` § 5p placements to see in-flight orders. Pass through to the subagent.

No image attachment, no inline `--positions` — the source-of-truth for the broker side is the live Tiger API. If `--positions` appears in `$ARGUMENTS`, ignore it.

## Step 1 — Invoke the portfolio-manager subagent

Build the brief and invoke `portfolio-manager`:

```
sync
--tiger-props-dir <path>      # only if explicitly passed
--include-orders              # only if explicitly passed
```

The subagent prompt knows what to do:
1. Reads `journal/positions.json` (the framework's view)
2. Constructs `TigerClient()` (paper-routed)
3. Calls `account_summary()`, `positions()`, optionally `open_orders()`
4. Diffs the two views
5. Returns a Markdown report with four sections of drift (matched-with-mismatches, journal-only, Tiger-only, orphan-orders)

Wait for the subagent's Markdown report.

## Step 2 — Handle the failure cases

If the subagent returns `SYNC_FAILED — <reason>`:

- `broker config:` → the Tiger credentials directory is missing, the props file is missing, or the SDK failed to parse. Surface the reason to the user and suggest checking `C:/Users/User/Desktop/tiger/tiger_openapi_config.properties` exists. Stop.
- `broker API:` → the Tiger API call failed. Surface the reason; could be network, expired token, or paper-account stalled. Stop.

For both: do NOT fall back to a journal-only "soft" snapshot — that's what `/p_s` is for. Sync's job is to compare the two; if one source is unreachable, the answer is "I don't know".

## Step 3 — Deliver the report

### If a Telegram channel tag is in the immediate conversation context

The original message arrived from Telegram. Capture `chat_id` and `message_id` from the `<channel source="telegram" chat_id="..." message_id="...">` tag.

Reply to that chat with the subagent's Markdown report via `mcp__plugin_telegram_telegram__reply`. Pass:

- `chat_id`: from the channel tag
- `text`: the subagent's report

Telegram messages cap at 4096 bytes. If the report exceeds that, send in two messages: first = sections 1-3 (summary + matched mismatches + journal-only); second = sections 4-7 (Tiger-only + orphan orders + account + notes).

### If no Telegram channel tag (running from the IDE)

Print the report directly to the user. Do not invoke any Telegram tool.

## Step 4 — Suggest follow-up commands (do NOT execute them)

After the report, append a short "what to do" line based on which sections were non-empty:

- Tiger-only positions present → suggest `/p_s_onboard` to bring them into the framework
- Journal-only positions present → ask: were these closed manually in Tiger? If yes, the user should remove them from `journal/positions.json` and mark the corresponding `ledgers/positions/<TICKER>.yml` as `meta.state: closed` (manual cleanup — not automated yet)
- Share/cost mismatches present → manual review; partial fills or share-counts from a different account
- Orphan orders present → likely in-flight from a recent `/morning-deep-dive` § 5p placement; user should confirm whether to wait, cancel, or treat as filled

DO NOT execute any of these follow-ups yourself. Sync's contract is read-only.

## Guardrails

- **Read-only.** Do not Write or Edit any file. Do not call `TigerClient.place_limit_*` or `cancel`. Reconciliation is a separate decision by the caller.
- **Paper-only.** Sync uses `TigerClient()` with default `allow_live=False`. It refuses to talk to a live account.
- **Sensitive information** — Tiger account number is already masked by `TigerClient` (`account_masked`). Never reconstruct the full number, never echo PII into Telegram. See CLAUDE.md § Sensitive Information.
- **Telegram parse-mode**: plain text (no `parse_mode`). The report has pipes that would break Markdown rendering.
- **No trade recommendations.** Drift is information, not a trade trigger. The user decides whether to onboard, close, or amend.
