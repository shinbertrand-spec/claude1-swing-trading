"""Corpus-ingest sub-package for the thematic-portfolio subagent (v1 scope).

V1 ships:

* :mod:`tools.thematic_portfolio.corpus.thirteen_f` — pulls SA LP + 3 ensemble
  13Fs via the already-verified edgartools pipeline; normalizes each to
  long-book / put-complex / call-book JSON files matching the sizer's
  expected :class:`Position` input shape.

* :mod:`tools.thematic_portfolio.corpus.manifest` — builds the
  ``corpus_snapshot`` dict the Loop 1 prompt consumes (see
  ``.claude/agents/_draft/thematic-portfolio.md`` § Input contract).
  Walks ``ledgers/thematic/corpus/`` for artifact subdirectories,
  composes the snapshot + recent-artifacts list since the prior
  Loop 1 firing.

Deferred to followup sessions:

* X timeline fetcher (twitterapi.io primary; Nitter fallback) — requires
  Bertrand to register a twitterapi.io account first per pre-build checklist.
  See [[swing-thematic-portfolio-x-ingest-decision]].
* Podcast RSS + Whisper transcription — needs yt-dlp + whisper deps.
* Press feed RSS parsers (Fortune / FT / WSJ / Bloomberg / Semafor) —
  multiple source-specific quirks; defer until X fetcher lands first.
* Substantive-artifact classifier (Haiku 4.5 LLM call) — Loop-1-trigger
  filter per [[swing-thematic-portfolio-substantive-artifact-definition]];
  separate LLM prompt, not a deterministic module.
* Tier 3 real-world signals (power-sector quarterly earnings, semiconductor
  inventory, hyperscaler capex guidance, energy futures) — out of scope
  until v2; v1 ships with `tier3_signals: null` in the manifest.

## Entity CIKs (confirmed 2026-05-24 — see project_thematic_portfolio memory)

Hardcoding these as module constants is acceptable per session-2 design
change #6 ("specific position-fund pairs are illustrative, not contracts")
because CIKs are *fund identity*, NOT position-fund pairs. The session-2
discipline applies to constants that encode "fund X holds ticker Y";
CIKs (identifying the funds themselves) are the inputs the live overlap
computation reads.
"""
from __future__ import annotations

# SA LP primary entity (filed Q1 2026 13F 2026-05-18; 42-row infotable)
SA_LP_CIK_PRIMARY = "0002045724"
# SA LP secondary entity (Situational Awareness Partners LP — parallel filer)
SA_LP_CIK_PARTNERS = "0002038540"

# Ensemble funds locked Q6 (per swing-thematic-portfolio-ensemble-funds)
ENSEMBLE_CIKS: dict[str, str] = {
    "altimeter": "0001541617",  # Altimeter Capital Management LP
    "coatue": "0001135730",  # Coatue Management LLC
    "light_street": "0001569049",  # Light Street Capital Management LLC
}

# 4th-seat ensemble candidate; 0 13F filings yet as of 2026-05-25 — deferred.
LIGHT_STREET_PHOTON_CIK = "0002009519"

# Default SEC EDGAR identity. Override with the EDGAR_IDENTITY env var if a
# different identity is required (e.g., shared-account run). The SEC requires
# a contact email for programmatic access per its fair-access guidelines.
DEFAULT_EDGAR_IDENTITY = "Bertrand Shin shinbertrand@gmail.com"
