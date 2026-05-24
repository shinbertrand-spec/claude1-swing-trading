---
description: Continue the morning routine after the 9:45 AM ET scan fired. Reads today's candidate file, optionally accepts ticker args inline ("/morning-deep-dive JCI GOOGL"), runs deep-dive + 5-gate compliance verification + trade approval. Each picked ticker gets a fact-ledger at ledgers/candidates/YYYY-MM-DD/<TICKER>.yml; on fill confirmation the ledger is promoted to ledgers/positions/<TICKER>.yml. Works from the IDE OR via Telegram DM if the session was started with --channels plugin:telegram@claude-plugins-official.
---

# Morning Deep-Dive (Post-Scan)

The morning candidate scan already ran (via Task Scheduler at 9:45 AM ET, or via the Cloud Routine variant). The 3 candidates were pushed to your Telegram / GitHub Issues and the full report saved to `journal/candidates/YYYY-MM-DD.md`. Pick up from here.

This command assumes the Phases 1–4 infrastructure: ledgers in `ledgers/`, tools in `tools/`, and rewritten subagents that consume both. If a `uv run python -m tools.*` invocation fails at any step, **STOP** and report — verification depends on tool availability.

## Detect invocation context

1. **Inline ticker arguments** — Look at the user's invocation message. If it contains tickers after the command name (e.g., `/morning-deep-dive JCI GOOGL` or `/morning-deep-dive JCI, GOOGL`), parse those tickers and skip Step 2. If no tickers are inline, run Step 2 normally.

2. **Telegram delivery** — Check whether MCP tools from the `telegram` server (`reply`, `react`, `edit_message`) are available. If yes, you are running with `--channels plugin:telegram@claude-plugins-official`; use `reply` to send each major output block (chat_id from `~/.claude/channels/telegram/access.json` `allowFrom[0]`). If no, output normally — the user is at the IDE.

## Step 1 — Show today's candidates

1. Read `journal/candidates/YYYY-MM-DD.md` (today's actual date).
2. If the file doesn't exist: today's scan didn't fire (Task Scheduler issue, weekend, holiday) OR you're running this before the scan window. Tell the user: *"No candidates file for today. Either the scan didn't fire or it's not market open yet. Want me to run /morning-scan interactively instead?"* Stop here.
3. Display the candidates concisely. If telegram reply is available, use it; otherwise output normally.

## Step 2 — Get the user's picks (skip if inline tickers were provided)

Ask: **"Which 2 do you want to deep-dive? (or 'none' to pass on all)"**

If running via Telegram, send this via `reply` and wait for the user's next message. Accept ticker symbols separated by space or comma, or "none".

## Step 3 — Deep-dive on each pick

For EACH ticker the user picked, in parallel where possible:

1. Invoke the `trade-researcher` subagent:

   > Ticker deep-dive for <TICKER>. Today's date is <YYYY-MM-DD>. Write the fact-ledger YAML to `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml` per the schema; populate `meta`, `quote`, `fundamentals`, `technical`, `regime`, `setup_classification`, `catalyst` (+ `ep_specific` if EP), and `reasoning_trace`. Run the relevant Phase 2 tools and cite each conclusion's `trace_refs`. Return the Markdown report mirroring the ledger.

2. Capture the ledger path from the report header (`Ledger: ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`).

3. Propose specific trade parameters using the ledger's `setup_classification.pivot_price` and `stop_price`. Target should be ≥ 2× the entry-to-stop distance (R:R ≥ 1:2). For position sizing, run `tools.position_sizer` with the ATR from the ledger:

   ```
   uv run python -m tools.position_sizer \
     --account <portfolio_value> --entry <pivot> --atr <ledger.technical.atr_14> \
     --setup-grade <ledger.setup_classification.grade> \
     --regime <ledger.regime.broad_market_stage_class> \
     --cash-available <cash>
   ```

   Use the `output.shares` and `output.capital` from the tool — do not compute by hand.

4. Invoke `risk-and-compliance` in **Mode 2 (Verification)**:

   > Verify and validate. Ledger: `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`. Researcher report path (if saved to file): `<path>` (otherwise paste below). Proposed trade: <ticker>, entry $<E>, stop $<S>, target $<T>, intended size <N> shares ($<C>). Portfolio state: cash $<X>, open positions <list with sectors>, total portfolio $<P>. Run the 5-gate sequence per your prompt.

   The subagent runs:
   1. `tools.ledger_freshness_audit` — BLOCK if stale
   2. `tools.trace_audit` — BLOCK on empty trace_refs or divergent re-run
   3. `tools.stale_phrase_detector` on the report — BLOCK on flagged phrases
   4. Independent `tools.position_sizer` re-run for hard-rule compliance
   5. Adversarial review against independent sources

## Step 4 — Present verdicts

For each ticker, present the risk-and-compliance verdict block verbatim. Include the 5-gate results table. Don't summarise it — Bertrand needs to see the math.

If running via Telegram, chunk the output across multiple `reply` calls (Telegram limit ~4096 chars per message).

## Step 5 — Trade confirmation

For each APPROVE / APPROVE-WITH-CONDITIONS, present the proposed trade in this format (one message per ticker if running via Telegram). The reply options depend on whether the setup is **deployable** (has cleared rolling walk-forward — see § Deployable setups below):

### Deployable setups — Tiger paper-placement offered

> *TICKER — proposed trade*
> Setup: <setup_type> · Grade: <grade>
> Ledger: `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`
> Entry (limit): $X.XX · Stop: $Y.YY · T1: $Z.ZZ · T2: $W.WW
> Shares: N · R:R: A.AA:1 · Risk: $R (P% of portfolio)
>
> Reply one of:
> - `place TICKER` — auto-place a paper limit-buy via Tiger API at the proposed entry. Bot will confirm the order ID; once filled in the paper account, reply `TICKER @ <fill_price>`.
> - `TICKER @ <fill_price>` — confirm a fill (if you've already placed manually).
> - `skip TICKER` (or no reply) — pass on this trade.

### Non-deployable setups — manual entry only

If the ledger's `setup_classification.type` is NOT in the deployable list (see below), DO NOT offer `place TICKER`. Use this format instead:

> *TICKER — proposed trade*
> Setup: <setup_type> · Grade: <grade>
> Ledger: `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`
> Entry (limit): $X.XX · Stop: $Y.YY · T1: $Z.ZZ · T2: $W.WW
> Shares: N · R:R: A.AA:1 · Risk: $R (P% of portfolio)
>
> ⚠ Setup <setup_type> has not cleared the rolling walk-forward gate — auto-paper-placement not offered. Reply `TICKER @ <fill_price>` if you've placed manually, or `skip` to pass.

### Deployable setups (as of 2026-05-24)

- **SEPA-VCP** (with sell-aware exits) — rolling agg Sharpe 2.28, DD -10.96%, n=394
- **EP** (loosened + ma_trail on 109-ticker universe) — rolling agg Sharpe 2.13, DD -3.07%, n=43

This list is hard-coded in this prompt — update when new setups clear the gate. The list is the rolling-walk-forward gate's verdict, NOT a per-trade compliance check (the 5-gate sequence handles that separately).

### Reply parsing

**Wait for the user's next message.** Parse:

1. `place TICKER` (or `place TICKER1, place TICKER2`) — auto-place. See § 5p below.
2. `TICKER @ <number>` (variants: `JCI @ 145.33`, `JCI @ $145.33`, `JCI@145.33`, `jci @ 145.33`; multiple in one message: `JCI @ 145.33, GOOGL @ 401.50`) — confirmed fill. Go to § 5a.
3. `cancel TICKER` — if a Tiger order was placed via `place` and is still open, cancel via `TigerClient.cancel(order_id)`. Record as no-trade.
4. Anything else (or no reply, or unmentioned) — passed.

### 5p — Auto-place via Tiger (deployable setups only)

For each `place TICKER`:

```python
from tools.broker.tiger import TigerClient
c = TigerClient()  # paper-routed; refuses live by default
entry = c.place_limit_buy(symbol="<TICKER>", quantity=<N>, limit_price=<X.XX>)
# entry.output: {order_id, symbol, action, quantity, limit_price, is_paper}
```

Reply to the user:

> *TICKER — order placed*
> Order ID: #<order_id> · Account: <account_masked> · Paper: ✅
> Limit-buy <N> shares @ $<X.XX> (DAY)
> Once filled, reply `TICKER @ <fill_price>`. To cancel, reply `cancel TICKER`.

Track the `order_id` so a follow-on `cancel TICKER` can call `TigerClient.cancel(order_id)`.

If the placement raises `BrokerOrderError`, surface the error verbatim and DO NOT promote the ledger. The user can retry, place manually, or skip.

If the placement raises `BrokerConfigError`, surface "Tiger config not loadable: <reason>. Falling back to manual flow — reply `TICKER @ <fill>` if you place manually." Continue without Tiger.

For each confirmed fill, in this exact order:

### 5a. Promote the candidate ledger to a position ledger

1. Read `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`.
2. Set `meta.state = "starter"` (or `"stage-2"` if the entry is a Momentum-Burst add to an existing STARTER; ask the user if ambiguous).
3. Set `meta.updated_by = "morning-deep-dive"` and `meta.updated_at` to now.
4. Add a `position_state` block:
   ```yaml
   position_state:
     stage: STARTER                              # or Stage-2 / Stage-3 for adds
     intended_full_shares: <N>                   # full-size target (3× for STARTER, full for non-pyramid)
     intended_full_capital_pct: <C / portfolio>
     risk_budget_pct: <from tools.position_sizer output.base_risk_budget_pct>
     starter:
       trigger: <EPGap | VCPBreakout | PullbackReversal | manual>
       fill_date: <YYYY-MM-DD>
       fill_time: "<HH:MM ET>"
       shares: <N>
       fill_price: <user's fill price>
       limit_price_placed: <proposed limit>
       initial_stop: <proposed stop>
       broker_order_id: <Tiger order_id if placed via § 5p; omit if manual>
       broker: <"tiger_paper" if placed via § 5p; omit if manual>
       trace_refs: <ledger setup_classification.trace_refs>
     current_stop: <proposed stop>
     trail_ma: lows_of_day                       # initial trail
     mandatory_exit_date: <ledger.ep_specific.mandatory_exit_date if EP, else null>
     trail_state_legacy: initial
     alerts_sent: []
   ```
5. Write the result to `ledgers/positions/<TICKER>.yml`.
6. If the fill diverges from the proposed entry by >0.5%, flag it in your reply (slippage check — the framework's R:R math used the proposed entry).

### 5b. Append to `journal/positions.json`

Read the file, check no existing position for that ticker, append a new object per the v2 schema, set `updated`, write UTF-8. Use the USER'S replied fill price as `entry_price`. Schema:

```json
{
  "ticker": "TICKER",
  "ledger_path": "ledgers/positions/TICKER.yml",
  "entry_date": "YYYY-MM-DD",
  "entry_price": <user's fill price>,
  "shares": <N>,
  "stop": <proposed stop>,
  "target_1": <proposed T1>,
  "target_2": <proposed T2 or null>,
  "thesis": "<one line>",
  "sector": "<GICS sector>",
  "catalysts": [{"date": "YYYY-MM-DD", "event": "..."}],
  "trail_state": "initial",
  "alerts_sent": [],
  "stage": "STARTER",
  "setup_type": "<ledger.setup_classification.type>",
  "setup_grade": "<ledger.setup_classification.grade>"
}
```

### 5c. Log the trade to today's journal

Ticker, fill price (and proposed limit if different), shares, stop, target(s), thesis, recomputed R:R from fill price, ledger path.

### 5d. Confirm back

(via `reply` if Telegram, else normally): *"Logged TICKER @ $X.XX. Position-checker is now watching. Stop $Y, T1 $Z. Ledger: `ledgers/positions/<TICKER>.yml`."*

For each ticker NOT confirmed (user said `skip` or didn't include it): log to journal as *"evaluated, passed, not placed."* The candidate ledger remains in `ledgers/candidates/` for audit but is NOT promoted.

For each BLOCK from Step 4: don't trade. Add to watchlist with specific re-evaluation trigger conditions. The candidate ledger remains in `ledgers/candidates/` with `meta.state = "rejected"` and a `notes` field explaining the BLOCK reason.

## Step 6 — Update journal

Append to today's journal:

- The candidates considered (with ledger paths) and which 2 were picked
- Deep-dive 1-paragraph summary per pick + ledger path
- Each verdict block from risk-and-compliance (5-gate results table + final verdict)
- Trades placed (with ledger path)
- Watchlist updates

If running via Telegram, send a short final `reply` confirming journal update + ledger paths for any new positions.

## Guardrails

- **Never auto-execute.** Always require explicit fill confirmation per trade.
- **Mechanical gates BLOCK = trade does not happen.** No "the BLOCK feels overly strict" override.
- **Read `CLAUDE.md` before judging compliance** — don't rely on memory.
- **No-trade is a valid outcome.** If all candidates BLOCK or user declines all, journal still gets the no-trade entry.
- **When using telegram reply:** every send needs `chat_id` from `~/.claude/channels/telegram/access.json` and `text`. Use Markdown parse mode (`*bold*`).
- **Sensitive information** — never echo `.env` contents, bot tokens, or PII into Telegram. See `CLAUDE.md` § Sensitive Information.
