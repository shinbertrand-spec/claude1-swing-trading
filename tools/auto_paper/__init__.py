"""Autonomous paper-trading pipeline for validation against live market microstructure.

Per CLAUDE.md § Broker bridge → § Paper-auto carve-out: this package runs an
autonomous entry pipeline on a parallel ledger track (`ledgers/paper-auto/`
+ `journal/paper-auto/positions.json`) using the Tiger paper account. It does
not touch human-discretionary positions or place real-money orders.

Modules:

* :mod:`tools.auto_paper.config` — deployable-setup list loader (reads
  ``tools/deployable_setups.yml``)
* :mod:`tools.auto_paper.state` — paper-auto ledger + positions.json I/O
* :mod:`tools.auto_paper.pipeline` — entry pipeline: filter → size → place →
  persist

Safety properties (invariants):

* :class:`tools.broker.tiger.TigerClient` refuses live unless
  ``allow_live=True``; this package NEVER passes that flag.
* Only setups on the deployable list are placeable; everything else is
  rejected upstream.
* Hard rules (5% / 20% / 8 / 15%) checked against the paper-auto track
  alone (separate from human-track positions).
* Dry-run mode prints what would be placed without calling Tiger.

Session 1 scope (2026-05-24): entry pipeline only.
Session 2 scope: EOD reconciliation + cron wiring.
Session 3 scope: broker-side stop + auto-exit composer.
Session 4 scope: performance dashboard.
"""
