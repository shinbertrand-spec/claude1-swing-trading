# /auto-paper — quant-scanner entry orchestrator (v2 — filesystem-protocol boundary)

Trigger auto-paper for the current trading day. Five steps; Python owns all state, LLM only fires subagents and saves envelopes.

Per [auto-paper LLM/Python boundary refactor 2026-05-28]. Architecture-level rationale at the spec; this command is the LLM coordination layer only.

## $ARGUMENTS parsing

- `--dry-run` — **true-shadow mode.** Steps 1–4 run identically (scan, screener, shell ledgers, skeptic + critic panel all fire for real). Step 5 still aggregates + sizes + builds + validates each candidate, but passes `--dry-run` to `--phase post_panel` so **no broker order is placed and no submitted ledger / positions.json row is written** — placement rows report `status=dry_run`. Use this to validate the v2 path places-clean at a real market open without trading. Only Step 5's Python invocation changes; everything upstream is byte-identical to a live run.

## Step 1 — Initialize the run

Run:
```bash
uv run python -m tools.auto_paper.run_entry --phase init
```

Expected stdout final line: `PHASE_INIT_OK run_dir=<path> invocations=<N>`.

**Pre-session orphan sweep (Priority 2, automatic).** Before touching candidates, `phase_init` runs a fresh read-only orphan check (`reconcile.presession_sweep`): it compares live broker holdings against the paper-auto starter ledgers. If the broker holds a position with **no ledger (Mode B orphan)** or an **unparseable ledger (corrupt-held)**, it persists `journal/paper-auto/orphan_discovery_<date>.yml`, sets the cron gate, and exits with `PHASE_INIT_GATED reason=presession_orphan_sweep` (exit code 2). This is defense-in-depth: it catches orphans even if the post-RTH reconciler never ran. Stuck-closing (Mode A) positions are surfaced as a `NOTE` but NOT gated (the post-RTH reconciler owns those). To resume: reconcile the orphan (onboard or flatten) then `uv run python -m tools.auto_paper.cron_gate` clear, or `python -c "from tools.auto_paper import cron_gate; cron_gate.clear_gate()"`.

If exit code is `2` with `PHASE_INIT_GATED`: the run is halted by design — surface the orphan/corrupt tickers to Bertrand and STOP (do not clear the gate autonomously; the operator reconciles first).

If exit code is non-zero (and not the gated `2` above) or the expected stdout marker is absent: surface the full stderr to Bertrand via Telegram (use `tools.telegram_notify` if available, else print to cron log) and STOP. Do not proceed.

Save the `run_dir` path from stdout for subsequent steps.

If `invocations=0` — that means the scanner returned no candidates today (or all hit the Phase 1 screener). Proceed to Step 3 (skipping Step 2 since there's nothing to fire); Step 5 will produce an empty placement_results and exit clean.

## Step 2 — Fire skeptic subagents

Read `{run_dir}/03_skeptic_invocations.yml`. It contains an `invocations` list. For each entry:

1. Invoke the `trade-skeptic` subagent via the Agent tool. Use the entry's `prompt` field verbatim as the agent prompt.
2. The subagent returns a structured bear thesis. Extract the terminal JSON envelope (per `.claude/agents/trade-skeptic.md` output schema).
3. Save the JSON envelope to the entry's `envelope_path`.

**Fire all skeptics in PARALLEL** — single message, one Agent tool call per ticker. Wait for all to complete before Step 3.

If any skeptic fails to return a valid envelope, save a stub envelope at `envelope_path` with `{"status": "subagent_unavailable", "ticker": "<TICKER>", "verdict": "WEAK"}` so Phase post_skeptic can proceed (Phase 2 design is log-only; missing skeptic doesn't block placement).

## Step 3 — Build panel invocations

Run:
```bash
uv run python -m tools.auto_paper.run_entry --phase post_skeptic
```

Expected stdout final line: `PHASE_POST_SKEPTIC_OK panel_invocations=<N>`.

If `MissingEnvelopeError` is raised in stderr, surface to Bertrand and STOP — Step 2 was incomplete.

## Step 4 — Fire critic panel subagents

Read `{run_dir}/05_panel_invocations.yml`. For each `invocations[*]` entry:

1. For each `critic_name` in the entry's `critics_to_fire`:
   - Invoke the named subagent (located at `.claude/agents/swing-critics/<critic_name>.md`).
   - Prompt: `"Apply the swing-critic invocation contract to this input dict: <entry.panel_input as JSON>. Emit ONLY the JSON envelope per the template's output schema."`
   - Save the envelope to `{entry.envelope_dir}/<critic_name>.json`.

**Fire ALL critics for ALL candidates in PARALLEL** — single message, one Agent tool call per (ticker × critic). Typical fire count: 3 candidates × 4 critics = 12 parallel calls. Wait for all before Step 5.

If individual critics fail: skip silently (aggregator handles partial votes). If ALL critics for a ticker fail: that's an LLM failure surface — proceed to Step 5 and let `MissingEnvelopeError` raise on the full-fail.

## Step 5 — Aggregate and place

Run (append `--dry-run` to this invocation **iff** `--dry-run` was passed to the command):
```bash
uv run python -m tools.auto_paper.run_entry --phase post_panel
# true-shadow window:
uv run python -m tools.auto_paper.run_entry --phase post_panel --dry-run
```

Expected stdout final line: `PHASE_POST_PANEL_OK placed=<N>`. In `--dry-run` the marker still appears with `placed=0` (nothing was actually placed); per-ticker rows in `07_placement_results.yml` carry `status: dry_run`.

If non-zero exit or missing marker: surface to Bertrand via Telegram with the stderr. Read `{run_dir}/07_placement_results.yml` for the per-ticker placement table and post the summary to Telegram.

## Guardrails

- **Never edit Python files in `tools/auto_paper/`** from this slash command. If a step's Python fails, surface the error — don't patch.
- **Never skip a step.** If you re-enter mid-run (e.g. after a transient failure), restart from Step 1 with a new `run_dir`; do not try to resume across runs.
- **Telegram surfacing on ANY non-zero exit.** Silent failure is the failure mode this entire architecture is designed against.

## Phase progression

- **Phase 3 v1 (current)**: panel in shadow mode (`shadow_mode=True` default in run_entry post_panel). All candidates place at sizing_multiplier=1.0 regardless of verdict; defer verdicts still block placement.
- **Phase 3 v2 (~2026-06-10)**: pass `--apply-panel-sizing` to `--phase post_panel` after 1-2 weeks of calibration data. Sizing becomes load-bearing.

## Migration note (2026-05-28 → 2026-05-29)

This command (`auto-paper-v2.md`) is the refactored shape. The v1 command (`auto-paper.md`) is preserved at `.claude/commands/_archive/auto-paper-v1-2026-05-28.md` for the parallel shadow-validation window. After 3 trading days of identical placement outcomes, v1 is retired and this file is renamed to `auto-paper.md`.
