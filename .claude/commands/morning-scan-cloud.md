---
description: CLOUD ROUTINE variant of the morning candidate scan. Runs in a fresh Anthropic-hosted session via Method 2b (Cloud Routines). Delivers candidates by opening a GitHub Issue on the project repo — the user gets a phone push via the GitHub mobile app, no allowlist required (api.github.com is already on the sandbox's default egress allowlist).
---

# Morning Candidate Scan — Cloud Routine variant (GitHub Issue delivery)

You are running in a fresh Anthropic Cloud Routine session. **There is no local disk to write to beyond the cloned repo** — output is via a new GitHub Issue on `shinbertrand-spec/claude1-swing-trading` and via this session's transcript (captured in routine history).

The project's `CLAUDE.md`, `.claude/agents/`, and `.claude/commands/` ARE available in this session — they travel with the project. What's NOT available: anything under `~/.claude/channels/`, anything in `journal/`, anything on Bertrand's local machine. **`api.telegram.org` is NOT on the sandbox egress allowlist** — use GitHub Issues instead of any direct Telegram POST.

## Pre-flight

1. **Read `CLAUDE.md`** to confirm framework rules are current.
2. **Verify `gh` CLI is available + authenticated.** Run `gh auth status` in a Bash call. If `gh` isn't installed or auth fails, fall back to plain `curl` against `https://api.github.com/repos/shinbertrand-spec/claude1-swing-trading/issues` using the `GITHUB_TOKEN` env var (which the sandbox typically injects).
3. **Weekend check.** If today is Saturday or Sunday (US Eastern), print `weekend — skipping` and exit. Routine should be paused on US market holidays manually.

## Step 1 — Run the candidate scan

Invoke the `risk-and-compliance` subagent in candidate-scan mode:

> Morning candidate scan for today's date. Suggest 3 swing-trade candidates that pass ALL framework hard rules in `CLAUDE.md` (uptrend on 20/50 SMA, price above 200-day SMA in bull regime, no earnings within 10 trading days, market cap > $2B, avg daily volume > 500K, no active investigations, sector not in clear weekly downtrend, ≥2 positive fundamental indicators). Return the standard candidate-scan output schema. If you find fewer than 3 clean candidates, return what you have and say so — do not pad.

## Step 2 — Build the GitHub Issue body

Take the researcher's output and format as a GitHub-flavored Markdown issue body. Keep concise; the issue body has no hard length limit but the GitHub mobile push notification only shows the first ~150 chars of the title plus the first line of body, so front-load.

Body shape (exact — the leading `@shinbertrand-spec` line is required, it triggers GitHub's mention notification so the push fires regardless of Watch/authorship rules):

```
@shinbertrand-spec — morning candidates ready.

**🔔 Morning Candidates — YYYY-MM-DD**

### 1. `TICKER` · sub-theme · $price · setup X/5
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>

### 2. `TICKER` · sub-theme · $price · setup X/5
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>

### 3. `TICKER` · sub-theme · $price · setup X/5
- **Why:** <one-line thesis>
- **Risk:** <one-line risk>

---
Pick up to 2 of these to deep-dive. At your desk, run:

`/morning-deep-dive TICKER1 TICKER2`

— Cloud routine `trig_01458eoZgMx6FyGLtz2K8PoT` · model claude-sonnet-4-6
```

If fewer than 3 candidates passed, include only what passed and add a final line `Only N candidates cleared the framework today.`

If ZERO passed:

```
@shinbertrand-spec — no-trade day.

**🔔 No candidates passed framework rules — YYYY-MM-DD**

No-trade day. Watchlist remains tradeable if triggers fire.
```

## Step 3 — Create the GitHub Issue

Write the body to a temp file, then create the issue via `gh`:

```bash
cat > /tmp/issue_body.md <<'BODY_EOF'
<BODY FROM STEP 2>
BODY_EOF

TODAY=$(date -u +%Y-%m-%d)
N_CANDIDATES=<count from researcher output>

gh issue create \
  --repo shinbertrand-spec/claude1-swing-trading \
  --title "🔔 Morning Candidates — ${TODAY} (${N_CANDIDATES} picks)" \
  --body-file /tmp/issue_body.md
```

If `gh` fails, fall back to curl:

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

Verify the result. Successful issue creation returns JSON with an `html_url` field — print that to the transcript.

## Step 4 — Final transcript line

Print one summary line so the routine history is glanceable:

`MORNING_SCAN_OK YYYY-MM-DD — N candidates delivered as GitHub Issue #<number>: <html_url>`

(or `MORNING_SCAN_FAIL YYYY-MM-DD — <reason>` on failure)

## Mobile push delivery (out of band)

The cloud routine does NOT POST to Discord, Telegram, or any other push channel directly — `discord.com` and `api.telegram.org` are both blocked at the sandbox egress allowlist.

Push delivery happens via the **`.github/workflows/discord-doorbell.yml`** GitHub Actions workflow, which fires on `issues.opened` events whose title matches `Morning Candidates` or `Morning scan failed`. The workflow runs on GitHub-hosted infrastructure (unrestricted egress) and POSTs a doorbell (title + URL) to the Discord webhook stored in the repo's `DISCORD_WEBHOOK_URL` secret.

Net effect from this routine's perspective: just create the issue and exit. The Actions workflow handles delivery automatically.

## Guardrails

- **No user interaction.** This is a stateless scheduled run.
- **No local disk reads/writes** beyond the project files that ship with the routine and `/tmp/` scratch. `journal/`, `~/.claude/channels/`, and Windows paths are NOT available.
- **Never auto-place trades.** The GitHub Issue is informational; the user separately runs `/morning-deep-dive` to act on it.
- **If `risk-and-compliance` errors**, still open a brief failure issue so Bertrand isn't left wondering whether the routine fired at all: title `⚠️ Morning scan failed — YYYY-MM-DD`, body with one-line reason. The Actions doorbell workflow matches on this title and will push the failure to phone automatically. Then exit non-zero so the routine history flags it.
- **Sensitive information** — never include secrets, tokens, `.env` contents, or PII in the issue body. The issue is candidates only. See `CLAUDE.md` § Sensitive Information.
- **No Telegram or Discord POST from this routine.** `api.telegram.org` and `discord.com` are both blocked at the cloud sandbox egress allowlist. The routine writes to GitHub only; phone push happens out-of-band via the `discord-doorbell.yml` Actions workflow. The local persistent session (started via `scripts/start-telegram-session.ps1`) handles inbound Telegram-driven flows separately.
