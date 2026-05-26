"""News-research Python primitives.

Counterpart to ``.claude/agents/news-research.md`` — the news-research
subagent's hourly snapshot pipeline composes from several deterministic
Python helpers. Modules:

* :mod:`tools.news_research.x_scanner` — cashtag-driven X tweet scanner
  for swing watchlist + open-position tickers. Sibling to
  :mod:`tools.thematic_portfolio.corpus.x_ingest` (handle-driven).

Both consume the same twitterapi.io credential via the shared
:mod:`tools.x_common.twitterapi_client`.
"""
from __future__ import annotations
