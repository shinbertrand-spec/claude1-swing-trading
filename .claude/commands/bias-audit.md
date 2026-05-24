---
description: Run the universe-side bias audit over recent candidate ledgers. Audits sector + market-cap discovery skew vs the S&P 500 baseline; flags buckets with |z| >= 2.0. Default window = 30 days; use --days N or --since/--until for custom ranges. Writes the report to journal/bias-audit/YYYY-MM-DD.md and surfaces flagged buckets in chat.
---

# Bias audit — universe-side discovery skew

The 5-gate sequence (ledger_freshness_audit, trace_audit, stale_phrase_detector, hard-rule compliance, adversarial review) catches arithmetic / reasoning / staleness failures **per trade**. It cannot see *systematic skew* across many trades — that's Type 4 from `[[llm-financial-hallucination]]` and the only doctrine requirement currently addressed only by this periodic ritual.

**Cadence:**
- **Monthly auto-run** on the 1st of each month (when wired up via cron/Scheduled Tasks).
- **On-demand** via this slash command after notable drawdowns, strategy changes, or when you suspect drift.

## Step 1 — Determine the audit window

Parse the user's arguments:

- `/bias-audit` (no args) → audit the past 30 days (default).
- `/bias-audit --days 90` → audit the past 90 days.
- `/bias-audit --since 2026-01-01 --until 2026-03-31` → audit a specific date range.
- `/bias-audit --baseline tools/data/some-other-baseline.yml` → use a custom baseline (e.g. Russell 1000 instead of S&P 500).

If no args and today is the 1st of a month, default to the previous full calendar month.

## Step 2 — Run the audit tool

```
uv run python -m tools.bias_audit \
    --candidates-root ledgers/candidates \
    --days <N> \
    --baseline tools/data/universe_baseline.yml \
    --format markdown
```

(or substitute `--since`/`--until` for `--days` if a custom range was provided)

The tool outputs a Markdown report on stdout. Capture it.

## Step 3 — Write the report to the journal

Append the captured Markdown to `journal/bias-audit/YYYY-MM-DD.md` (create the directory if it doesn't exist). The filename is today's date.

If the file already exists for today (re-running on the same day), append a new section under a `## Re-run at HH:MM` header rather than overwriting.

## Step 4 — Surface the verdict in chat

Read the structured output (re-run with `--format trace` if you need the JSON). Surface a one-paragraph summary:

- **No flagged buckets** → *"Bias audit clean: N candidates over <date range>, no sector or market-cap bucket past |z| >= 2.0. Report: journal/bias-audit/YYYY-MM-DD.md."*
- **Flagged buckets present** → *"Bias audit flagged K bucket(s) over N candidates:*
  - *Sector X over-represented (observed Y%, baseline Z%, z=+W) — driven by tickers: [list of top 5]*
  - *Market-cap mega over-represented (...)
  - *...*
  - *Full report: journal/bias-audit/YYYY-MM-DD.md."*
- **Sample size inadequate** (n < 30) → *"Bias audit produced low-confidence findings (n=N candidates, below the 30-candidate threshold). Re-run when more candidates have accumulated. Partial report still written for the record."*

## Step 5 — Suggest a journal action (only if flagged)

If buckets were flagged, suggest ONE concrete journal action for Bertrand to consider:

- **Sector over-representation** → *"Consider an explicit anti-sector-bias rule in trade-researcher's working principles, or widen the candidate-scan brief beyond [over-rep sector]."*
- **Sector under-representation** → *"Consider whether [under-rep sector] has setups currently being overlooked. Could be a true market regime (defensive sector underperforming in risk-on) or a discovery blind spot."*
- **Mega-cap over-representation** → *"Possible name-recognition bias. Consider explicitly requesting mid-cap candidates in next morning-scan, or adjusting trade-researcher to filter by market-cap diversity."*
- **Mid-cap over-representation** → *"Less common direction. Check whether mega-caps are being filtered out by another rule (e.g. a P/E filter that excludes high-multiple mega-caps)."*

Do NOT propose code changes to trade-researcher autonomously — surface the observation and let Bertrand decide.

## Guardrails

- **Audit is informational, never blocking.** It does NOT veto candidates or trades. It surfaces patterns for Bertrand to weigh.
- **Baseline staleness matters.** The `universe_baseline.yml` carries an `as_of_quarter` field; if more than 2 quarters out of date, note it in the chat summary.
- **Sample size matters more than z-score.** A z-score of +2.5 with n=15 is noise. The tool flags below-threshold sample sizes with a low-confidence note; surface that prominently.
- **Don't act on a single audit.** The doctrine's premise is that bias *persists*; a single month's skew might be a regime artifact, not bias. Two consecutive monthly audits showing the same skew = signal worth acting on.
- **Re-runs are fine.** This is read-only over candidate ledgers; rerunning won't change any state.

## Edge cases

- **Zero candidates in window** → tool returns a clean empty report. Surface: *"No candidates evaluated in the past N days; audit skipped."*
- **Candidates root missing** → tool returns n=0 gracefully. Same as above.
- **Custom baseline file fails to load** → tool falls back to built-in default. Surface this in chat.
