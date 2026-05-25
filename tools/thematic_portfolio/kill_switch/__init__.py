"""Kill-switch monitor (Process B) for the thematic-portfolio subagent.

Implements the Q10 CPPI 3-tier deleveraging schedule from
[[swing-thematic-portfolio-kill-switch-architecture]]:

* **Tier 1** — drawdown >= 20%, target allocation 17.5% (from 25%).
* **Tier 2** — drawdown >= 35%, target allocation 12.5%.
* **Tier 3** — drawdown >= 50% OR Aschenbrenner-kill-event, target 0%.

The architectural principle is **separation from the reasoning loop (Process A)**:
this package runs as its own process, has no LLM dependencies under normal
operation, and CANNOT be disabled by anything Process A does. Process A reads
the kill-switch event log to learn what allocation it's now constrained to,
but cannot tell Process B "the drawdown is fine, hold off."

## Session 1 scope (this commit)

* :mod:`tools.thematic_portfolio.kill_switch.ladder` — pure deterministic
  ``compute()`` for the 3-tier ladder. No I/O, no network.
* :mod:`tools.thematic_portfolio.kill_switch.state` — peak.json (rolling
  thematic-book peak), events.jsonl (append-only event log),
  heartbeat.json (last-cycle timestamp), aschenbrenner_kill_event.json
  (boolean flag set by the artifact classifier).
* :mod:`tools.thematic_portfolio.kill_switch.positions` — thematic-position
  identifier (intersect Tiger positions with
  ``journal/thematic-portfolio/positions.json`` thematic index).
* :mod:`tools.thematic_portfolio.kill_switch.clock` — US market-hours
  detector (chooses 60s vs 300s sleep cadence).
* :mod:`tools.thematic_portfolio.kill_switch.monitor` — the Process B
  ``while True`` loop. **Defaults to ``--dry-run``** — logs decisions but
  does NOT place orders. Session 2 wires real order placement +
  heartbeat watchdog + escalation routing.

## Hard refusals (encoded in every entry point)

* Process B refuses to run against a live Tiger account in v1
  (``allow_live=False`` is the only path that reaches the broker).
* Process B refuses to act on positions outside the thematic-portfolio
  index — paper-auto and human-discretionary positions are out of scope.
* Process B's decisions are append-only — the event log is never
  rewritten, only appended.
"""
from __future__ import annotations
