---
description: Continue the morning routine after the 9:45 PM scan fired. Reads today's candidate file, optionally accepts ticker args inline ("/morning-deep-dive JCI GOOGL"), runs deep-dive + compliance + trade approval. Works from the IDE OR via Telegram DM if the session was started with --channels plugin:telegram@claude-plugins-official.
---

# Morning Deep-Dive (Post-Scan)

The morning candidate scan already ran (via Task Scheduler at 9:45 PM MYT). The 3 candidates were pushed to your Telegram and the full report saved to `journal/candidates/YYYY-MM-DD.md`. Pick up from here.

## Detect invocation context

Two things to detect at the start:

1. **Inline ticker arguments** — Look at the user's invocation message. If the message that triggered this skill contains tickers after the command name (e.g., `/morning-deep-dive JCI GOOGL` or `/morning-deep-dive JCI, GOOGL`), parse those tickers and skip Step 2. If no tickers are inline, run Step 2 normally.
2. **Telegram delivery** — Check whether MCP tools from the `telegram` server (`reply`, `react`, `edit_message`) are available. If yes, you are running in a session that was started with `--channels plugin:telegram@claude-plugins-official` and the user is likely interacting from Telegram. Use the `reply` tool to send each major output block to the user's chat (chat_id from `~/.claude/channels/telegram/access.json` allowFrom[0]). If no, output normally — the user is at the IDE.

## Step 1 — Show today's candidates

1. Read `journal/candidates/YYYY-MM-DD.md` (today's actual date).
2. If the file doesn't exist:
   - Either today's scan didn't fire (Task Scheduler issue, weekend, holiday) OR you're running this before tonight's 9:45 PM ET fire.
   - Tell the user: "No candidates file for today. Either Task Scheduler didn't fire or it's not market open yet. Want me to run /morning-scan interactively instead?"
   - Stop here.
3. Display the candidates concisely. If telegram reply is available, use it; otherwise output normally.

## Step 2 — Get the user's picks (skip if inline tickers were provided)

Ask: **"Which 2 do you want to deep-dive? (or 'none' to pass on all)"**

If running via Telegram, send this via `reply` and wait for the user's next message. The next inbound user message contains the picks.

Accept ticker symbols separated by space or comma, or "none".

## Step 3 — Deep-dive on each pick

For EACH ticker the user picked:

1. Invoke `trade-researcher` subagent for the full 6-section deep-dive.
2. Propose specific trade parameters that comply with the framework. **Show your math**:
   - Stop ≤ 8% from entry
   - R:R ≥ 1:2
   - Position ≤ 5% of portfolio (and halve if bear regime)
3. Invoke `risk-and-compliance` in **verification mode** with:
   - The full trade-researcher report (paste it in)
   - The proposed entry / stop / target / size
   - Current portfolio state (cash, open positions, sector exposure — read from latest journal)

## Step 4 — Present verdicts

For each ticker, present:

- **Verdict:** APPROVE / APPROVE-WITH-CONDITIONS / BLOCK
- One-sentence reason
- Key hard-rule check summary (stop %, R:R, sector, earnings window)

If running via Telegram, chunk the output across multiple `reply` calls if needed (Telegram limit ~4096 chars per message — keep each block under that).

## Step 5 — Trade confirmation

For each APPROVE / APPROVE-WITH-CONDITIONS, present the proposed trade in this format (one message per ticker if running via Telegram):

> *TICKER — proposed trade*
> Entry (limit): $X.XX  ·  Stop: $Y.YY  ·  T1: $Z.ZZ  ·  T2: $W.WW
> Shares: N  ·  R:R: A.AA:1
>
> Reply `TICKER @ <fill_price>` once you've actually placed AND filled the order in your paper account. Reply anything else (or `skip`) to pass on this trade.

**Wait for the user's next message** before continuing. Parse it as a position-confirmation if it matches the format `TICKER @ <number>`:

- Accept tolerant variants: `JCI @ 145.33`, `JCI @ $145.33`, `JCI@145.33`, `jci @ 145.33`, etc.
- Multiple tickers in one message also work: `JCI @ 145.33, GOOGL @ 401.50`.
- If the message doesn't parse as a fill confirmation for any of the proposed tickers, treat all unmentioned tickers as passed.

For each confirmed fill, in this exact order:

1. **Append to `journal/positions.json`.** Read the file, check no existing position for that ticker, append a new object, set `updated`, write UTF-8. Use the USER'S REPLIED FILL PRICE as `entry_price` (not the proposed limit — the actual fill is what matters). Stop and targets stay as proposed (those were sent with the order). Schema:
   ```json
   {
     "ticker": "TICKER",
     "entry_date": "YYYY-MM-DD",
     "entry_price": <user's replied fill price>,
     "shares": <integer>,
     "stop": <proposed stop>,
     "target_1": <proposed T1>,
     "target_2": <proposed T2 or null>,
     "thesis": "<one line>",
     "sector": "<GICS sector>",
     "catalysts": [{"date": "YYYY-MM-DD", "event": "..."}],
     "trail_state": "initial",
     "alerts_sent": []
   }
   ```
   If the fill diverges from the proposed entry by >0.5%, flag it in your reply (slippage check — the framework's R:R math used the proposed entry).

2. **Log the trade** to today's journal with ticker, fill price (and proposed limit if different), shares, stop, target(s), thesis, recomputed R:R from fill price.

3. **Confirm back** (via `reply` if Telegram, else normally): "Logged TICKER @ $X.XX. Position-checker is now watching. Stop $Y, T1 $Z."

For each ticker NOT confirmed (i.e. user said `skip` or didn't include it): log to journal as "evaluated, passed, not placed." Do NOT touch positions.json.

For each BLOCK from Step 4: don't trade. Add to watchlist with specific re-evaluation trigger conditions. Do NOT touch positions.json.

## Step 6 — Update journal

Append to today's journal:

- The candidates considered (and which 2 were picked)
- Deep-dive 1-paragraph summary per pick
- Each verdict + decision
- Trades placed (if any)
- Watchlist updates

If running via Telegram, send a short final `reply` confirming the journal was updated.

## Guardrails

- **Never auto-execute.** Always require `y/n` confirmation per trade.
- **Read `CLAUDE.md` before judging compliance** — don't rely on memory of the rules.
- **No-trade is a valid outcome.** If both candidates BLOCK or user declines all, journal still gets the no-trade entry.
- **When using telegram reply:** every send needs `chat_id` from `~/.claude/channels/telegram/access.json` (the first numeric ID in `allowFrom`) and `text` (your message). Use Markdown parse mode (`*bold*`).
