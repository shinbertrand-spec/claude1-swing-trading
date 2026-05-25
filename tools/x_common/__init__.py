"""Shared X / Twitter primitives.

This subpackage hosts code shared between the two X consumers:

* :mod:`tools.thematic_portfolio.corpus.x_ingest` — handle-driven polling
  for the thematic-portfolio Aschenbrenner/SA-LP signal stack.
* :mod:`tools.news_research.x_scanner` — cashtag-driven scanning for
  swing-trading news-research hourly snapshots.

Both consume the same twitterapi.io credential (one paid account, shared
under ``~/.claude/channels/twitterapi/.env``) and share the same low-level
HTTP / pagination / retry primitives — see
:mod:`tools.x_common.twitterapi_client`. Higher-level concerns (ledger
schemas, classification, deduplication, routing) live with each consumer.

Design references:

* ``Bertieboo/wiki/notes/swing-thematic-portfolio-x-ingest-decision.md``
* ``Bertieboo/wiki/notes/swing-news-research-x-scanner-design-spec.md``
"""
from __future__ import annotations
