---
description: CLOUD ROUTINE variant of the morning candidate scan. Runs in a fresh Anthropic-hosted session via Method 2b (Cloud Routines). Delivers candidates by opening a GitHub Issue on the project repo — the user gets a phone push via the GitHub mobile app, no allowlist required (api.github.com is already on the sandbox's default egress allowlist).
---

# Morning Candidate Scan — Cloud Routine variant (GitHub Issue delivery)

You are running in a fresh Anthropic Cloud Routine session. **There is no local disk to write to beyond the cloned repo, and no git push back to origin** — output is via a new GitHub Issue on `shinbertrand-spec/claude1-swing-trading` and via this session's transcript.

The project's `CLAUDE.md`, `.claude/agents/`, `.claude/commands/`, `tools/`, `ledgers/_examples/`, `ledgers/_schema/`, and `pyproject.toml` ARE available — they travel with the project. **Phases 2–4 tools (`tools.regime_check`, `tools.trend_template`, `tools.earnings_calendar`) are usable** if `uv` + Python are present in the sandbox. What's NOT available: `~/.claude/channels/`, the local `journal/`, anything on Bertrand's machine, and `api.telegram.org` (blocked at egress).

**Persistent ledger writes are NOT possible here** — there's no git push. This routine produces an informational issue only. The actual ledger lifecycle happens in `/morning-deep-dive` at Bertrand's IDE/Telegram session.

## Pre-flight

1. **Read `CLAUDE.md`** to confirm framework rules are current.
2. **Verify `gh` CLI** — run `gh auth status`. If `gh` isn't installed or auth fails, fall back to plain `curl` against `https://api.github.com/repos/shinbertrand-spec/claude1-swing-trading/issues` using the `GITHUB_TOKEN` env var.
3. **Verify Python tooling** — run `uv --version` and `uv run python --version`. If either fails, you can still produce an issue but skip the Stage-4 circuit-breaker tool call; note the limitation in the issue body.
4. **Weekend check** — if today is Saturday or Sunday (US Eastern), print `weekend — skipping` and exit. Routine is paused on US market holidays manually.

## Step 1 — Sync deps (one-time per session)

If `uv` is available, run `uv sync --quiet` so `tools.*` modules resolve. If this errors, log it but continue to Step 2 — the subagent can fall back to LLM-only screening.

## Step 2 — Run the candidate scan

Invoke the `risk-and-compliance` subagent in Mode 1 (candidate-scan):

> Morning candidate scan for today's date. Mode 1 protocol per your prompt — invoke `tools.regime_check SPY` first (circuit-break and STOP if Stage 4). If the regime tool is unavailable in this sandbox, document that and proceed with WebSearch-based screening. Propose up to 3 swing-trade candidates that pass ALL framework hard rules in `CLAUDE.md`. For each candidate, attempt `tools.trend_template <ticker>` and `tools.earnings_calendar <ticker>`. Return the standard candidate-scan output schema. If you find fewer than 3 clean candidates, return what you have and say so — do not pad.

## Step 3 — Build the GitHub Issue body

GitHub-flavored Markdown. The mobile push notification shows the first ~150 chars of title plus the first line of body — front-load the most important info.

### Normal candidate-day body:

```
@shinbertrand-spec — morning candidates ready.

**🔔 Morning Candidates — YYYY-MM-DD**

### 1. `TICKER` · sub-theme · $price · setup <grade>
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>
- **Next earnings:** <date or "outside 10d window">

### 2. `TICKER` · sub-theme · $price · setup <grade>
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>
- **Next earnings:** <date or "outside 10d window">

### 3. `TICKER` · sub-theme · $price · setup <grade>
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>
- **Next earnings:** <date or "outside 10d window">

---
Pick up to 2 of these to deep-dive. At your desk, run:

`/morning-deep-dive TICKER1 TICKER2`

— Cloud routine `trig_01458eoZgMx6FyGLtz2K8PoT` · model claude-sonnet-4-7
```

If fewer than 3 passed, include only what passed and add `Only N candidates cleared the framework today.`

### Stage-4 circuit-breaker body:

```
@shinbertrand-spec — Stage 4 broad market, no scan today.

**🚫 Stage 4 Broad Market — YYYY-MM-DD**

SPY trend_template_passes: X/7 — per swing-regime-playbook circuit breaker,
no new entries today. Even perfect setups drop to 30-40% hit rate in Stage 4.

Manage existing positions; no scan needed.
```

### Zero-passed body:

```
@shinbertrand-spec — no-trade day.

**🔔 No candidates passed framework rules — YYYY-MM-DD**

No-trade day. Watchlist remains tradeable if triggers fire.
```

## Step 4 — Create the GitHub Issue

```bash
cat > /tmp/issue_body.md <<'BODY_EOF'
<BODY FROM STEP 3>
BODY_EOF

TODAY=$(date -u +%Y-%m-%d)
N_CANDIDATES=<count from researcher output>

gh issue create \
  --repo shinbertrand-spec/claude1-swing-trading \
  --title "🔔 Morning Candidates — ${TODAY} (${N_CANDIDATES} picks)" \
  --body-file /tmp/issue_body.md
```

If `gh` fails, fall back:

```bash
JSON_BODY=$(jq -Rs --arg title "🔔 Morning Candidates — ${TODAY} (${N_CANDIDATES} picks)" \
  '{title: $title, body: .}' < /tmp/issue_body.md)

curl -sS -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/shinbertrand-spec/claude1-swing-trading/issues" \
  -d "$JSON_BODY"
```

For Stage-4 circuit-breaker, use title `🚫 Stage 4 Broad Market — ${TODAY}` (no candidate count).

Verify the result. Successful issue creation returns JSON with an `html_url` — print that to the transcript.

## Step 5 — Final transcript line

Print one summary line so the routine history is glanceable:

- `MORNING_SCAN_OK YYYY-MM-DD — N candidates delivered as GitHub Issue #<number>: <html_url>`
- `MORNING_SCAN_STAGE_4 YYYY-MM-DD — circuit-breaker issue #<number>: <html_url>`
- `MORNING_SCAN_NONE YYYY-MM-DD — no-trade issue #<number>: <html_url>`
- `MORNING_SCAN_FAIL YYYY-MM-DD — <reason>` (on failure)

## Mobile push delivery (out of band)

The cloud routine does NOT POST to Discord, Telegram, or any other push channel directly — both are blocked at the sandbox egress allowlist.

Push delivery happens via the `.github/workflows/discord-doorbell.yml` GitHub Actions workflow, which fires on `issues.opened` events whose title matches `Morning Candidates`, `Stage 4 Broad Market`, or `Morning scan failed`. The workflow POSTs a doorbell (title + URL) to the Discord webhook stored in the repo's `DISCORD_WEBHOOK_URL` secret. Net effect: just create the issue and exit; the Actions workflow handles delivery.

## Guardrails

- **No user interaction.** This is a stateless scheduled run.
- **No persistent ledger writes** — there's no git push from the sandbox. The ledger workflow happens at `/morning-deep-dive` (IDE/Telegram session) which can write to `ledgers/` locally.
- **No local disk reads/writes** beyond project files that ship with the routine and `/tmp/` scratch. `journal/`, `~/.claude/channels/`, Windows paths NOT available.
- **Never auto-place trades.** The GitHub Issue is informational; the user runs `/morning-deep-dive` to act on it.
- **If `risk-and-compliance` errors**, still open a brief failure issue: title `⚠️ Morning scan failed — YYYY-MM-DD`, body with one-line reason. The Actions doorbell matches on this title and pushes to phone. Then exit non-zero so the routine history flags it.
- **If a Phase 2 tool errors** (e.g. `tools.regime_check` fails on a yfinance hiccup), include the tool error in the issue body so Bertrand knows the screening was LLM-only that day rather than tool-verified.
- **Sensitive information** — never include secrets, tokens, `.env` contents, or PII in the issue body. The issue is candidates only. See `CLAUDE.md` § Sensitive Information.
