"""Entry-trigger watcher (v1) — scheduled, deterministic, advisory-only.

Watches the structured `trigger` blocks on `journal/watchlist.json` entries
against live prices and pushes a Telegram alert when a name reaches its entry
zone / breakout. Composes the existing deterministic detectors (pullback_detect,
atr_compute, regime_check) — no new market math. Never places a trade.

Mirrors the observability cockpit pattern: edge-triggered + de-duplicated via a
small state file so a condition that stays true pages once, not every tick.
"""
