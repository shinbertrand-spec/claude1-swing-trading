---
description: /p_s — portfolio snapshot. Three input modes — (a) read journal/positions.json (no args, default), (b) parse positions from an image attachment (a broker screenshot — works from Telegram or IDE), (c) inline positions paste in $ARGUMENTS. When invoked from a Telegram channel session, also replies via the Telegram reply tool with the report. Read-only — does not modify any file. Canonical caption is /p_s (short for portfolio-snapshot).
---

# /p_s — Portfolio Snapshot — image / inline / positions.json

You are running a portfolio-wide assessment. Three input sources, in priority order. **Use the FIRST one available**:

1. **Image attachment** — the conversation has a recently-attached image. From Telegram, this arrives as a `<channel source="telegram" ...>` tag with `image_path` (read directly) or `attachment_file_id` (call `mcp__plugin_telegram_telegram__download_attachment` first to fetch the file, then Read the returned path). From the IDE, the user may have pasted a screenshot directly. Either way, Read the image — Claude is multimodal and will see the table.
2. **Inline positions paste** in `$ARGUMENTS` — look for a `--positions` block matching the format below.
3. **`journal/positions.json`** — fall through to this if neither above is provided. Today this file's `positions[]` is empty; the snapshot will report "no open positions" if you reach this branch.

## $ARGUMENTS parsing

Parse from `$ARGUMENTS` (may be empty):

- `--total-portfolio-usd <N>` — total portfolio value in USD; passed through to the subagent.
- `--peak-portfolio-usd <N>` — historical peak USD; passed through to the subagent.
- `--positions ... --end-positions` block — inline positions paste. Format: one position per line, whitespace-separated `TICKER SHARES COST_BASIS [SECTOR]`.

## Step 1 — Acquire positions

### Input source A: image attachment

1. Locate the image file path. From Telegram: read the `<channel>` tag's `image_path` attribute (preferred). If only `attachment_file_id` is present, call `mcp__plugin_telegram_telegram__download_attachment` with that file_id; the tool returns a file path; Read it. From the IDE: the user has already attached the image so its path is in your context.
2. **Read the image** with the Read tool. Claude's vision parses the table directly.
3. **Extract positions** into a structured list. Most broker screenshots have columns: Symbol/Name, Position/Mkt Value, Current/Cost, P&L. Per row, extract:
   - `ticker` (uppercase, e.g. `BABA`)
   - `shares` (integer — the "Position" or share-count column)
   - `cost_basis` (per-share cost; usually the smaller of the two numbers in the Current|Cost column, often labelled "Cost")
   - `current_price` (per-share current price; usually the larger / first of the Current|Cost column, often labelled "Current" or "Mkt Price")
   - `market_value` (shares × current_price; also often shown explicitly under "Position" or "Mkt Value")
   - `unrealized_pnl_usd` (the P&L column — green positive, red negative)
4. **Sanity check**: `shares × current_price ≈ market_value` (within $1) and `shares × (current_price − cost_basis) ≈ unrealized_pnl_usd`. If a row fails, flag it in the report's "Notes" section and skip that position.
5. **Capture the total portfolio value if visible**. Most broker apps show a "US Mkt Cap" or "Total Value" line at the top. If visible AND `--total-portfolio-usd` was NOT passed in `$ARGUMENTS`, use the screenshot's value. Surface what you used and where it came from in section 7 of the report.

### Input source B: inline positions paste

Parse the `--positions ... --end-positions` block per the subagent's documented format. Pass through verbatim.

### Input source C: journal/positions.json

No additional acquisition — the subagent handles this case itself.

## Step 2 — Invoke the portfolio-manager subagent

Build the brief and invoke `portfolio-manager`. Pass:

- `--total-portfolio-usd <N>` if known (from `$ARGUMENTS` or the screenshot)
- `--peak-portfolio-usd <N>` if provided in `$ARGUMENTS`
- For source A or B: the parsed positions inline in `--positions ... --end-positions` format
- For source C: no positions block (subagent reads positions.json itself)

The subagent prompt knows what to do. Wait for its Markdown report.

## Step 3 — Deliver the report

### If a Telegram channel tag is in the immediate conversation context

The original message arrived from Telegram. Capture `chat_id` and `message_id` from the `<channel source="telegram" chat_id="..." message_id="...">` tag.

Reply to that chat with the subagent's Markdown report via `mcp__plugin_telegram_telegram__reply`. Pass:

- `chat_id`: from the channel tag
- `text`: the subagent's report

Telegram messages cap at 4096 bytes. If the report exceeds that, send the report in two messages: first message = sections 1-3 (regime / position table / rule check); second message = sections 4-7 (sector heatmap / unmanaged / drawdown / notes).

### If no Telegram channel tag (running from the IDE)

Print the report directly to the user. Do not invoke any Telegram tool.

## Step 4 — Confirm positions parsed (image source only)

For input source A (image), the parsing step is non-deterministic — Claude's vision may misread a row. After delivering the report, append a brief verification block listing what was parsed:

```
Parsed from screenshot:
  BABA   86 shares  cost 151.21  current 129.50
  CEG    10 shares  cost 277.22  current 293.57
  ...
If any row is wrong, re-send the screenshot with caption: /portfolio-snapshot --positions
TICKER SHARES COST
...
--end-positions
```

This gives the user a fast correction path without re-OCR.

## Guardrails

- **Read-only.** Do not Write or Edit any file. The snapshot is ephemeral by design; persisting positions is `onboard` mode (deferred).
- **Do not invent positions** if the image parse is ambiguous. Flag the ambiguous row in "Notes" and skip it rather than guessing.
- **Sensitive information** — never echo broker account numbers, names, or other PII from the screenshot. The position table needs only ticker / shares / prices. If the screenshot shows an account number (e.g. "Prime Account(50968016)"), do NOT include it in any output — Telegram or otherwise. See CLAUDE.md § Sensitive Information.
- **Telegram parse-mode**: send as plain text (no `parse_mode`). The report has pipes and asterisks that would break Markdown rendering otherwise.
- **No trade recommendations.** The snapshot reports state and flags violations only; trim proposals belong to `rebalance` mode (deferred).
