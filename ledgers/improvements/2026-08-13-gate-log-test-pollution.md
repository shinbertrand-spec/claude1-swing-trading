# Gate-log test pollution — found + fixed 2026-08-13 (ET)

Found during the midday `/auto-paper-monitor` pass (12:30 PM ET), while auditing
an anomalous `ledgers/paper-auto/_gates/2026-08-13.jsonl`.

## Finding

Every `uv run pytest` run since the B1 gate chain shipped (2026-07-24) appended
fixture gate-chain verdicts to the PRODUCTION audit trail and rewrote the
PRODUCTION circuit-breaker state:

- `tests/test_auto_paper_intent_log.py`'s `paper_dirs` fixture redirected the
  state paths and stubbed the screener + regime, but — unlike its siblings
  `test_auto_paper_pipeline.py` (stubs `_run_gate_chain`) and
  `test_auto_paper_pipeline_gate_chain.py` (sets `GATE_CHAIN_LOG_DIR` /
  `GATE_CHAIN_BREAKER_PATH`) — left the real `_run_gate_chain` live with no
  env redirect.
- Each pytest run therefore wrote ~7 NVDA fixture chains (net_liq $1,000,000,
  limit 850.50, stop 820, p=0.55, `source: "paper-auto-pipeline"`) to
  `ledgers/paper-auto/_gates/<date>.jsonl` and called `save_breaker_state`
  against `journal/paper-auto/sleeve_breaker.json`.

Extent: ALL four gate files (07-24, 07-27, 08-06, 08-13) were 100% fixture rows
— every chain carries the $1M fixture equity, every ticker is NVDA. Zero real
gate verdicts have ever been logged (no candidate has flowed through
`place_candidate` since the chain was wired — book flat, scans empty), so
nothing real was lost.

Breaker state: uncorrupted by luck. Fixture equity ($1.0M) equals the recorded
high-water ($1.0M, set 2026-07-24), so test writes were byte-identical no-ops.
The latent hazard was real: a future fixture with equity ≥20% below the
production high-water would have TRIPPED the production breaker — which stays
tripped until operator reset — silently vetoing all placements, including the
pre-registered fill-fidelity cycle (placement morning 2026-08-14).

## Remediation

1. `tests/test_auto_paper_intent_log.py` — `paper_dirs` now stubs
   `_run_gate_chain` to a pass-through, mirroring `test_auto_paper_pipeline.py`
   (also removes those tests' hidden dependency on the live data cache for
   NVDA ADV/correlation).
2. The four polluted files moved to
   `ledgers/paper-auto/_gates/_test_pollution_quarantine/` (content preserved;
   nothing reads `_gates` programmatically yet — grep confirmed the only
   `_gates` code refs are the writer itself).

## Verification

- `uv run pytest tests/test_auto_paper_intent_log.py` → 27 passed; no new
  `_gates` file created; `sleeve_breaker.json` mtime unchanged.
- Sibling files (`test_auto_paper_pipeline.py`,
  `test_auto_paper_pipeline_gate_chain.py`, `test_auto_paper_gate_chain.py`)
  → 82 passed; `_gates` still clean.

## Residual

- The production gate ledger is now empty-by-truth: the first REAL verdict rows
  will appear when a candidate actually reaches the chain (possibly the
  2026-08-14 ts_momentum placement).
- Structural note for a future hardening pass: `append_gate_log` /
  `save_breaker_state` default to production paths on import; any new test (or
  script) that reaches `place_candidate` without a stub/env repeats this class
  of leak. A conftest-level guard (fail loudly if `GATE_CHAIN_LOG_DIR` unset
  under pytest) would close the class, not just the instance.
