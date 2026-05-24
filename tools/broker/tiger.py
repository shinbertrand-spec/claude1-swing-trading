"""Tiger Brokers Open Platform — paper-trading bridge (SKELETON).

This is a planning skeleton; the `tigeropen` SDK is not yet installed
(staged but commented in pyproject.toml). Build out next session.

## What this module will do

* Read Tiger credentials from a config file at ``$TIGER_CONFIG_PATH``
  (default: ``C:/Users/User/Desktop/tiger/private_key.pem``)
* Expose a thin :class:`TigerClient` wrapping ``tigeropen`` SDK with
  paper-account routing
* Provide paper-order primitives: ``place_limit_buy``, ``place_limit_sell``,
  ``cancel_order``, ``get_positions``, ``get_account_summary``
* Emit :class:`TraceEntry` for every order (auditable per doctrine
  Requirement 3 — every action cites a tool call)

## Why this exists

CLAUDE.md requires a paper-portfolio test of >= 1 quarter before any
live capital is deployed. Today's "paper portfolio" is hand-tracked
via journal/positions.json updated from finviz snapshots — error-prone
and doesn't exercise real fill/slippage behavior. The Tiger paper
account simulates fills using real market microstructure. Bridge it,
and the strategy ranks emitted by ``risk-and-compliance`` can be
auto-routed to a paper order without manual entry.

## Security contract

* The private key file is NEVER read into memory by anything outside
  this module's :func:`load_config` function
* The private key value is NEVER logged, NEVER returned from any
  public API function, NEVER included in any TraceEntry
* Account numbers are PII per CLAUDE.md § Sensitive Information and
  are MASKED in any output (last 4 digits only)
* Test fixtures NEVER hit the live Tiger API — they mock the SDK
  layer entirely

## Build plan (next session)

1. ``uv sync`` after uncommenting ``tigeropen`` in pyproject.toml
2. Implement :func:`load_config` — parse the desktop credentials file
3. Implement :class:`TigerClient` — thin wrapper, paper-routed
4. Wire ``/morning-deep-dive`` fill-confirmation step to optionally
   place via :class:`TigerClient` instead of just journal-write
5. Add ``positions sync`` mode to ``portfolio-manager`` — pull from
   Tiger API instead of manual broker-app screenshot
6. Tests:
   * ``test_broker_tiger_load_config.py`` — config parse, error paths
   * ``test_broker_tiger_client_mock.py`` — mocked SDK, verifies
     paper-account routing + order shape
7. Update ``CLAUDE.md`` § Subagent Workflow with the new flow
8. Update memory ``project_quant_subagent.md`` (or new
   ``project_broker_bridge.md``) with the build snapshot

## What this module is NOT

* Not a live-trading entry point — paper account only in v1
* Not a strategy generator — that's ``quant-strategist`` /
  ``trade-researcher``
* Not a real-time market-data feed — Phase 5 uses yfinance cache; the
  Tiger market-data API could replace finviz quote panels later but
  that's separate scope from order routing
"""
from __future__ import annotations

# Skeleton — no executable code yet. Next session uncomments tigeropen
# in pyproject.toml and builds out load_config + TigerClient.

CREDENTIALS_PATH_DEFAULT = "C:/Users/User/Desktop/tiger/private_key.pem"

# Sentinel for tests: when this is True the module has been built out.
SKELETON_ONLY = True
