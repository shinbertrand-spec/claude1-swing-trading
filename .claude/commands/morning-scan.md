---
description: Morning swing-trading routine — candidate scan, deep-dive, 5-gate compliance verification, trade approval (9:30–10:30 AM ET). Interactive at the IDE.
---

# Morning Trading Routine

It is morning ET on a US market day. Run the morning routine end-to-end. This is the **interactive** variant — the headless --print variant (Windows Task Scheduler) is at `morning-scan-telegram.md`; the cloud variant is at `morning-scan-cloud.md`.

The pipeline assumes the Phases 1–4 infrastructure exists: ledgers in `ledgers/`, tools in `tools/`, and the rewritten subagents in `.claude/agents/` that consume both. If `uv run python -m tools.regime_check SPY` errors at Step 2, **STOP** and report — the rest of the pipeline depends on tool availability.

## Step 1 — State of the world

Read in parallel:

- `CLAUDE.md` (the framework)
- `ledgers/README.md` (the ledger contract subagents follow)
- `tools/README.md` (catalog so you can read tool output verbatim)
- The most recent file in `journal/` (current portfolio state, open positions, watchlist)
- `journal/positions.json` (open positions index)

Confirm: market is open, no overnight stop triggers on open positions, no stale `journal/positions.json` entries.

## Step 2 — Candidate scan (9:45 AM ET)

Invoke `risk-and-compliance` in **Mode 1 (candidate-scan)** with the prompt:

> Morning candidate scan for today's date. Suggest 3 swing-trade candidates that pass ALL framework hard rules. Mode 1 protocol per your prompt — run regime_check SPY first (circuit-breaker if Stage 4), then propose 3 candidates with per-candidate trend_template + earnings_calendar tool runs. Return fewer than 3 if you cannot find 3 clean ones — do not pad.

The subagent will internally invoke `tools.regime_check SPY` and STOP if broad market is Stage 4. If it returns the circuit-breaker message, present it to the user and skip directly to Step 5 (journal-only update).

Otherwise present the 3 (or fewer) candidates cleanly. **Wait for the user to pick up to 2** (or pass on all of them).

## Step 3 — Deep-dive on picks (10:00 AM ET)

For EACH ticker the user picks, in parallel where possible:

1. Invoke `trade-researcher` with the prompt:

   > Ticker deep-dive for <TICKER>. Today's date is <YYYY-MM-DD>. Write the fact-ledger YAML to `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml` per the schema; populate `meta`, `quote`, `fundamentals`, `technical`, `regime`, `setup_classification`, `catalyst` (+ `ep_specific` if EP), and `reasoning_trace`. Run the relevant Phase 2 tools and cite each conclusion's `trace_refs`. Return the Markdown report mirroring the ledger.

2. Capture the ledger path returned in the report header (`Ledger: ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`).

3. Propose specific trade parameters using the ledger's pivot + suggested stop. Don't re-derive — use the values from `setup_classification.pivot_price` and `setup_classification.stop_price`. Target should be ≥ 2× the entry-to-stop distance (R:R ≥ 1:2).

4. Invoke `risk-and-compliance` in **Mode 2 (Verification)** with the prompt:

   > Verify and validate. Ledger: `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`. Researcher report path: `<path or paste>`. Proposed trade: <ticker>, entry $<E>, stop $<S>, target $<T>, intended size <N> shares ($<C>). Portfolio state: cash $<X>, open positions <list with sectors>, total portfolio $<P>. Run the 5-gate sequence per your prompt. Return the verdict block.

   The subagent will run:
   1. `tools.ledger_freshness_audit` — BLOCK if stale
   2. `tools.trace_audit` — BLOCK on empty trace_refs or divergent re-run
   3. `tools.stale_phrase_detector` on the report — BLOCK on flagged phrases
   4. Independent `tools.position_sizer` re-run for hard-rule compliance
   5. Adversarial review against independent sources

## Step 4 — Verdict and trade placement

Present each verdict from risk-and-compliance verbatim (it includes the 5-gate results table + adversarial findings + final verdict).

- **APPROVE** → ask the user to confirm; on confirmation, log the proposed limit order to today's journal.
- **APPROVE-WITH-CONDITIONS** → present the numbered conditions; user decides whether to proceed, modify, or pass.
- **BLOCK** → don't trade; add to watchlist with the specific re-evaluation trigger ("re-enter when X happens").

**When the user confirms a fill** (paper-portfolio "TICKER @ <fill_price>" format), promote the candidate ledger to a position ledger:

1. Read `ledgers/candidates/<YYYY-MM-DD>/<TICKER>.yml`.
2. Set `meta.state` to `"starter"` (or appropriate stage if not pyramiding).
3. Add a `position_state` block with the filled `starter` leg (trigger, fill_date, fill_time, shares, fill_price, limit_price_placed, initial_stop, trace_refs from setup_classification).
4. Update `meta.updated_at` / `meta.updated_by`.
5. Write the result to `ledgers/positions/<TICKER>.yml`.
6. Append to `journal/positions.json` per the v2 schema; populate `ledger_path: "ledgers/positions/<TICKER>.yml"`, `stage: "STARTER"` (or appropriate), `setup_type` + `setup_grade` from the candidate ledger.

If the fill diverges from the proposed entry by >0.5%, flag it (slippage check — the R:R math used the proposed entry).

## Step 5 — Update journal

Append to today's journal (`journal/YYYY-MM-DD.md` or `journal/Trading.md`):

- Market context (SPY + sector + VIX)
- Pre-trade portfolio snapshot
- The candidates considered (with ledger paths) and which the user picked
- Deep-dive 1-paragraph summary per pick + ledger path
- Each verdict block from risk-and-compliance (5-gate results + verdict)
- Trades placed (limit price, fill price, slippage if any, size, thesis, stop, target, ledger path)
- Watchlist additions / changes
- End-of-day reflection placeholder (filled later by `eod-journal`)

## Guardrails

- **Never recommend or place a trade that fails any mechanical gate (Gates 1–4).** No "the BLOCK feels overly strict, override it" reasoning. If risk-and-compliance BLOCKs, the trade does not happen.
- **Always read `CLAUDE.md` before judging anything** — do not rely on memory of the rules.
- **No-trade is a valid outcome.** Stage 4 circuit-breaker, all candidates BLOCKed, user passes on all — all are valid no-trade days.
- **The user has the final say on every APPROVE / APPROVE-WITH-CONDITIONS** — never auto-execute.
- **Stale-phrase scan applies to your own output too.** If you write "as of late 2024" or "I don't have access to real-time", a downstream review will flag it.
