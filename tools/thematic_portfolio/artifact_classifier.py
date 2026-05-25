"""Substantive-artifact classifier — deterministic pre-filter + rate-limit + persistence.

Closes the operational gap in [[swing-thematic-portfolio-substantive-artifact-definition]]:
the spec defines the 4-tier classification + 3/wk rate limit + mandatory-escalation
overrides, but the Loop 1 ``trigger.type: substantive_artifact`` path is dead code
without something that decides whether an incoming artifact should fire Loop 1.

Architecture: deterministic pre-filter in this module handles ~70% of cases
(Tier 1 auto-trigger sources by URL/source pattern; Tier 3 hard-excludes by
author + length). Only the **ambiguous boundary** cases (Aschenbrenner /
Shulman / Trammell X posts that meet the length bar but require content
judgment) flow through to the LLM call described in
[`thematic-artifact-classifier.md`](../../.claude/agents/_draft/thematic-artifact-classifier.md).

This module does NOT make the LLM call itself — the orchestrator (Weeks 5-8
``/thematic-portfolio`` slash command) invokes the classifier subagent
via the ``Agent`` tool and feeds the result back through
:func:`apply_rate_limit` + :func:`record_firing`.

## Public surface

* :class:`ArtifactInput` — typed input dataclass
* :class:`ClassificationResult` — typed output dataclass
* :func:`pre_filter` — deterministic Tier-1 + Tier-3 hard-rules; returns
  ``None`` when the artifact's tier requires LLM judgment
* :func:`apply_rate_limit` — given a tier verdict + firing log, decide
  whether the firing is rate-limited or mandatory-escalation-overridden
* :func:`record_firing` — atomic append to the firing log state file
* :func:`load_firing_log` / :func:`save_firing_log` — JSON I/O for state

## Mandatory-escalation signals (also surfaced by the LLM)

Three patterns the wrapper detects deterministically against the snippet
(belt-and-suspenders with the LLM's `mandatory_escalation_signals` array):

1. **Thesis abandonment** — verbatim presence of one of the canonical
   phrases (``we've exited``, ``we sold``, ``no longer hold``, etc.)
2. **Strategy pivot** — ``we've pivoted``, ``we've moved from``, etc.
3. **SA LP event** — ``SA LP closure``, ``fund unwinding``, etc.

Plus an automatic mandatory escalation when **≥ 3 firings have been
recorded within the past 24 hours** (Aschenbrenner press tour or major
announcement; the spec calls this out explicitly).
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..cli import emit
from ..contract import TraceEntry

TOOL = "tools/thematic_portfolio/artifact_classifier.py"

# Default state-file location. The orchestrator may override per call.
DEFAULT_FIRING_LOG_PATH = Path("ledgers/thematic/loop1/_state/firing_log.json")

# Rate-limit policy per substantive-artifact-definition § rate limit
MAX_FIRINGS_PER_WEEK = 3
RATE_LIMIT_WINDOW_HOURS = 24 * 7
PRESS_TOUR_BURST_THRESHOLD = 3
PRESS_TOUR_WINDOW_HOURS = 24

# Signal types that trip the Aschenbrenner-kill-event flag read by Process B
# (the kill-switch monitor). Per [[swing-thematic-portfolio-kill-switch-architecture]]
# § Aschenbrenner-specific kill events. Detected either by the deterministic
# regex scan (_scan_mandatory_escalation) OR by the LLM classifier surfacing
# the same signal_type in its mandatory_escalation_signals output.
#
# IMPORTANT: setting the flag triggers a Tier 3 full-unwind. False-positive
# cost is preferred over false-negative cost per the design — Bertrand can
# always clear the flag via state.clear_kill_event after manual review.
KILL_EVENT_SIGNAL_TYPES = frozenset({
    "thesis_abandonment",  # "we've exited", "no longer hold"
    "sa_lp_event",         # "SA LP closure", "fund unwinding"
    "sa_lp_closure",       # explicit closure synonym the LLM might use
    "regulatory_action",   # SEC enforcement, fine, suspension
    "principal_incident",  # Aschenbrenner death / incapacitation / criminal investigation
})

# Tier-1 source-pattern catalog. Per substantive-artifact-definition § Tier 1.
TIER1_ESSAY_VENUES = (
    "forourposterity.com",
    "ft.com",
    "wsj.com",
    "nytimes.com",
    "theatlantic.com",
    "foreignaffairs.com",
    "thefp.com",  # The Free Press
)
TIER1_PRESS_DOMAINS = (
    "fortune.com",
    "ft.com",
    "wsj.com",
    "nytimes.com",
    "bloomberg.com",
    "semafor.com",
    "theinformation.com",
    "tabletmag.com",
    "city-journal.org",
)
TIER1_PODCAST_TOKENS = (
    "dwarkesh",
    "lex-fridman",
    "lexfridman",
    "all-in",
    "bg2",
    "acquired",
    "joe-lonsdale",
    "lonsdale",
    "conversations-with-tyler",
    "tyler-cowen",
    "honestly",
    "bari-weiss",
    "the-information",
    "a16z",
    "patrick-oshaughnessy",
    "invest-like-the-best",
    "ilttb",
)
LISTED_X_AUTHORS = ("@leopoldasch", "@CarlShulman", "@philip_trammell")

# Tier-2 content signal regexes (compiled lazily)
_TICKER_LIKE_RE = re.compile(
    r"\b(NVDA|AVGO|AMD|MU|TSM|ASML|INTC|ORCL|GOOGL|MSFT|META|AMZN|CEG|VST|"
    r"BE|GEV|CRWV|SNDK|IREN|CORZ|APLD|RIOT|CLSK|BITF|BTDR|HIVE|WYFI|SMH|"
    r"GLW|INFY|PSIX|SEI|BW|PUMP|TE)\b",
    re.IGNORECASE,
)
_THESIS_UPDATE_PHRASES = (
    "now think",
    "updated my view",
    "was wrong about",
    "see this differently",
    "changed my mind",
    "no longer believe",
    "still hold the view",
    "we've exited",
    "we've added",
    "we sold",
    "we bought",
    "thesis intact",
    "thesis break",
)
_MARKET_EVENT_TOKENS = (
    " today ",
    " this week ",
    " yesterday ",
    " q1 ",
    " q2 ",
    " q3 ",
    " q4 ",
    "earnings",
    "fomc",
    "fed meeting",
    "13f",
)
_THESIS_ABANDONMENT_PHRASES = (
    "we've exited",
    "we exited",
    "we sold our",
    "we've sold our",
    "no longer hold",
    "we're out of",
    "exiting the ",
    " thesis is closed",
)
_STRATEGY_PIVOT_PHRASES = (
    "we've pivoted",
    "we've moved from",
    "we're now short instead of long",
    "we're now long instead of short",
)
_SA_LP_EVENT_PHRASES = (
    "sa lp closure",
    "sa lp closing",
    "sa lp wind",
    "fund unwinding",
    "fund closure",
    "winding down the fund",
)


@dataclass(frozen=True)
class ArtifactInput:
    """One incoming artifact + the metadata the classifier needs."""

    source: str
    snippet: str
    snippet_length_chars: int
    fetched_at: str
    url: str | None = None
    is_thread: bool = False
    thread_total_words: int = 0
    author_handle: str | None = None


@dataclass
class ClassificationResult:
    """Output of the classifier pipeline (pre-filter OR LLM + post-processing)."""

    tier: int  # 1, 2, 25, or 3
    reason: str
    matched_rule: str
    pre_filter_decided: bool
    would_fire_loop1: bool
    mandatory_escalation_signals: list[dict[str, str]] = field(default_factory=list)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pre-filter (deterministic)
# ---------------------------------------------------------------------------


def _scan_mandatory_escalation(snippet: str) -> list[dict[str, str]]:
    """Scan snippet for any of the three deterministic escalation signal patterns."""
    out: list[dict[str, str]] = []
    lower = snippet.lower()
    for phrase in _THESIS_ABANDONMENT_PHRASES:
        if phrase in lower:
            out.append(
                {
                    "signal_type": "thesis_abandonment",
                    "matched_phrase": phrase,
                    "severity": "high",
                }
            )
    for phrase in _STRATEGY_PIVOT_PHRASES:
        if phrase in lower:
            out.append(
                {
                    "signal_type": "strategy_pivot",
                    "matched_phrase": phrase,
                    "severity": "high",
                }
            )
    for phrase in _SA_LP_EVENT_PHRASES:
        if phrase in lower:
            out.append(
                {
                    "signal_type": "sa_lp_event",
                    "matched_phrase": phrase,
                    "severity": "high",
                }
            )
    return out


def _has_tier2_content_signal(snippet: str) -> tuple[bool, str | None]:
    """Return (has_signal, which_signal) — used to disambiguate Tier 2 vs 2.5."""
    if _TICKER_LIKE_RE.search(snippet):
        return True, "tier_2_x_with_ticker"
    lower = snippet.lower()
    for phrase in _THESIS_UPDATE_PHRASES:
        if phrase in lower:
            return True, "tier_2_x_with_thesis_update"
    padded = f" {lower} "
    for token in _MARKET_EVENT_TOKENS:
        if token in padded:
            return True, "tier_2_x_with_market_event"
    return False, None


def pre_filter(artifact: ArtifactInput) -> ClassificationResult | None:
    """Deterministic Tier-1 auto-trigger + Tier-3 hard-exclude rules.

    Returns a ClassificationResult when the artifact's tier can be decided
    without LLM judgment. Returns ``None`` for ambiguous boundary cases
    (X posts from listed authors that meet the length bar; the LLM resolves
    Tier 2 vs Tier 2.5 boundary).

    Calling code that gets ``None`` should invoke the
    `thematic-artifact-classifier` subagent and pass its JSON output to
    :func:`finalize_from_llm_verdict`.
    """
    escalation_signals = _scan_mandatory_escalation(artifact.snippet)
    source = artifact.source.lower()

    # ----------------------------------------------------------------
    # Tier 1 — auto-trigger sources (no judgment needed)
    # ----------------------------------------------------------------
    if source.startswith("13f:") or source.startswith("regulatory:"):
        return ClassificationResult(
            tier=1,
            reason="SEC EDGAR filing (13F/13D/13G/Form-ADV) or regulatory filing — Tier 1 auto-trigger",
            matched_rule=(
                "tier_1_filing" if source.startswith("13f:") else "tier_1_regulatory"
            ),
            pre_filter_decided=True,
            would_fire_loop1=True,
            mandatory_escalation_signals=escalation_signals,
        )

    if source.startswith("essay:"):
        url = (artifact.url or "").lower()
        if any(v in url for v in TIER1_ESSAY_VENUES):
            return ClassificationResult(
                tier=1,
                reason=f"Aschenbrenner-byline essay at Tier-1 venue: {artifact.url}",
                matched_rule="tier_1_essay",
                pre_filter_decided=True,
                would_fire_loop1=True,
                mandatory_escalation_signals=escalation_signals,
            )

    if source.startswith("podcast:"):
        suffix = source[len("podcast:") :]
        if any(token in suffix for token in TIER1_PODCAST_TOKENS):
            return ClassificationResult(
                tier=1,
                reason=f"Aschenbrenner appearance on Tier-1 podcast: {suffix}",
                matched_rule="tier_1_podcast",
                pre_filter_decided=True,
                would_fire_loop1=True,
                mandatory_escalation_signals=escalation_signals,
            )

    if source.startswith("press:"):
        url = (artifact.url or "").lower()
        suffix = source[len("press:") :]
        if any(d in url for d in TIER1_PRESS_DOMAINS) or any(
            d in suffix for d in TIER1_PRESS_DOMAINS
        ):
            return ClassificationResult(
                tier=1,
                reason=f"Tier-1 press profile of Aschenbrenner / SA LP: {artifact.url}",
                matched_rule="tier_1_press",
                pre_filter_decided=True,
                would_fire_loop1=True,
                mandatory_escalation_signals=escalation_signals,
            )

    # ----------------------------------------------------------------
    # Tier 3 — deterministic hard excludes
    # ----------------------------------------------------------------

    # X posts from non-listed authors (or unidentifiable authors) → Tier 3
    if source.startswith("x:"):
        author = (artifact.author_handle or "").strip()
        if author not in LISTED_X_AUTHORS:
            return ClassificationResult(
                tier=3,
                reason=(
                    f"X post by non-listed handle ({author or 'unknown'}); "
                    "Tier 2 reserved for @leopoldasch / @CarlShulman / @philip_trammell"
                ),
                matched_rule="tier_3_other",
                pre_filter_decided=True,
                would_fire_loop1=False,
                mandatory_escalation_signals=escalation_signals,
            )
        # X post from listed author but very short + not in a thread → Tier 3
        if artifact.snippet_length_chars < 150 and not (
            artifact.is_thread and artifact.thread_total_words >= 500
        ):
            return ClassificationResult(
                tier=3,
                reason=(
                    "Listed-handle X post < 150 chars and not in a ≥500-word thread "
                    "— per Tier 3 short-post exclusion"
                ),
                matched_rule="tier_3_short_post",
                pre_filter_decided=True,
                would_fire_loop1=False,
                mandatory_escalation_signals=escalation_signals,
            )
        # Listed-handle X post that meets length bar — flow to LLM for Tier 2 vs 2.5
        return None

    # Press from non-Tier-1 domains → Tier 3 lower-tier press
    if source.startswith("press:"):
        return ClassificationResult(
            tier=3,
            reason=f"Press source not in Tier-1 outlet list: {artifact.url or source}",
            matched_rule="tier_3_lower_tier_press",
            pre_filter_decided=True,
            would_fire_loop1=False,
            mandatory_escalation_signals=escalation_signals,
        )

    # Adjacent-fund press / coverage → Tier 3 (flows through Loop 2 calibration)
    if source.startswith("press_adjacent_fund:") or source.startswith("press_critic:"):
        rule = (
            "tier_3_adjacent_press"
            if source.startswith("press_adjacent_fund:")
            else "tier_3_critic_essay"
        )
        return ClassificationResult(
            tier=3,
            reason="Adjacent-fund coverage / critic essay — refresh quarterly, not per-artifact",
            matched_rule=rule,
            pre_filter_decided=True,
            would_fire_loop1=False,
            mandatory_escalation_signals=escalation_signals,
        )

    if source.startswith("policy:"):
        return ClassificationResult(
            tier=3,
            reason="Wider AI-policy news — Aschenbrenner not named; flows to Tier 3 corpus",
            matched_rule="tier_3_wider_policy",
            pre_filter_decided=True,
            would_fire_loop1=False,
            mandatory_escalation_signals=escalation_signals,
        )

    # Unknown source pattern — refuse to guess; surface to caller
    return None


# ---------------------------------------------------------------------------
# Post-LLM verdict finalization
# ---------------------------------------------------------------------------


VALID_LLM_TIERS = (1, 2, 25, 3)


def finalize_from_llm_verdict(
    llm_verdict: dict[str, Any], snippet: str
) -> ClassificationResult:
    """Validate + supplement the classifier subagent's JSON output.

    The wrapper merges:

    * The LLM's tier + reason + matched_rule + notes (trusted as-is, modulo
      schema validation)
    * The LLM's mandatory_escalation_signals UNION with our deterministic
      :func:`_scan_mandatory_escalation` pass over the snippet (belt-and-suspenders;
      the LLM might miss a phrase, our regex might miss a paraphrase — we
      take the union)

    Raises:
        ValueError: malformed LLM output (missing keys, invalid tier).
    """
    required = {"tier", "reason", "matched_rule"}
    missing = required - set(llm_verdict.keys())
    if missing:
        raise ValueError(f"LLM verdict missing required keys: {missing}")
    tier = int(llm_verdict["tier"])
    if tier not in VALID_LLM_TIERS:
        raise ValueError(f"LLM tier {tier!r} not in {VALID_LLM_TIERS}")

    llm_signals = llm_verdict.get("mandatory_escalation_signals", []) or []
    deterministic_signals = _scan_mandatory_escalation(snippet)
    # Dedupe by (signal_type, matched_phrase)
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, str]] = []
    for sig in [*llm_signals, *deterministic_signals]:
        key = (sig.get("signal_type", ""), sig.get("matched_phrase", "").lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(sig)

    natural_fire = tier in (1, 2)

    return ClassificationResult(
        tier=tier,
        reason=str(llm_verdict["reason"]),
        matched_rule=str(llm_verdict["matched_rule"]),
        pre_filter_decided=False,
        would_fire_loop1=natural_fire,
        mandatory_escalation_signals=merged,
        notes=llm_verdict.get("notes"),
    )


# ---------------------------------------------------------------------------
# Rate limit + firing log
# ---------------------------------------------------------------------------


@dataclass
class RateLimitDecision:
    """How the wrapper resolved a candidate Loop 1 firing."""

    fired: bool
    rate_limited: bool
    mandatory_escalation_applied: bool
    firings_in_window: int
    burst_in_24h: int
    rationale: str


def _parse_iso(s: str) -> datetime:
    cleaned = s.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)


def load_firing_log(path: Path) -> dict[str, Any]:
    """Load the firing-log state file. Returns an empty schema-1.0 doc when missing."""
    if not path.exists():
        return {"schema_version": "1.0", "updated_at": None, "firings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_firing_log(path: Path, log: dict[str, Any]) -> None:
    """Atomic write of the firing-log state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2), encoding="utf-8")


def apply_rate_limit(
    result: ClassificationResult,
    firing_log: dict[str, Any],
    proposed_fired_at: str,
) -> RateLimitDecision:
    """Decide whether a candidate Loop 1 firing actually happens.

    Logic per [[swing-thematic-portfolio-substantive-artifact-definition]]:

    1. If ``result.would_fire_loop1`` is false → never fires; not rate-limited
       (it just didn't qualify in the first place).
    2. Count firings whose ``fired_at`` falls within the past 7 days.
    3. Count firings whose ``fired_at`` falls within the past 24 hours
       (press-tour burst detection).
    4. Mandatory escalation overrides the 3/wk cap when ANY of:

       - ``result.mandatory_escalation_signals`` is non-empty
       - ``burst_in_24h >= PRESS_TOUR_BURST_THRESHOLD``
       - source is ``13f:*`` (calibration non-negotiable — but the source-based
         check is here for robustness even though the pre-filter already
         resolves 13f sources to Tier 1)
    5. Otherwise apply the 3/wk cap.
    """
    if not result.would_fire_loop1:
        return RateLimitDecision(
            fired=False,
            rate_limited=False,
            mandatory_escalation_applied=False,
            firings_in_window=0,
            burst_in_24h=0,
            rationale="Tier output does not fire Loop 1 (Tier 2.5 / 3 / pre-filter decline).",
        )

    proposed_dt = _parse_iso(proposed_fired_at)
    week_cutoff = proposed_dt - timedelta(hours=RATE_LIMIT_WINDOW_HOURS)
    burst_cutoff = proposed_dt - timedelta(hours=PRESS_TOUR_WINDOW_HOURS)

    firings_in_window = 0
    burst_in_24h = 0
    for entry in firing_log.get("firings", []):
        if "fired_at" not in entry:
            continue
        try:
            ts = _parse_iso(entry["fired_at"])
        except (TypeError, ValueError):
            continue
        if ts >= week_cutoff:
            firings_in_window += 1
        if ts >= burst_cutoff:
            burst_in_24h += 1

    has_escalation_signal = bool(result.mandatory_escalation_signals)
    burst_override = burst_in_24h >= PRESS_TOUR_BURST_THRESHOLD
    mandatory_escalation = has_escalation_signal or burst_override

    if firings_in_window < MAX_FIRINGS_PER_WEEK:
        return RateLimitDecision(
            fired=True,
            rate_limited=False,
            mandatory_escalation_applied=False,
            firings_in_window=firings_in_window,
            burst_in_24h=burst_in_24h,
            rationale=(
                f"Within {MAX_FIRINGS_PER_WEEK}/wk cap "
                f"({firings_in_window} firings in past 7d). Fires normally."
            ),
        )
    # Cap consumed
    if mandatory_escalation:
        rationale_bits = []
        if has_escalation_signal:
            rationale_bits.append("mandatory_escalation_signal present")
        if burst_override:
            rationale_bits.append(
                f"press-tour burst ({burst_in_24h} firings in 24h)"
            )
        return RateLimitDecision(
            fired=True,
            rate_limited=True,
            mandatory_escalation_applied=True,
            firings_in_window=firings_in_window,
            burst_in_24h=burst_in_24h,
            rationale=(
                f"Cap consumed ({firings_in_window}/{MAX_FIRINGS_PER_WEEK}) but "
                f"mandatory escalation overrides: {'; '.join(rationale_bits)}."
            ),
        )
    return RateLimitDecision(
        fired=False,
        rate_limited=True,
        mandatory_escalation_applied=False,
        firings_in_window=firings_in_window,
        burst_in_24h=burst_in_24h,
        rationale=(
            f"Cap consumed ({firings_in_window}/{MAX_FIRINGS_PER_WEEK}) and "
            "no mandatory-escalation override — queued for next monthly base."
        ),
    )


def extract_kill_event_signal(
    signals: list[dict[str, str]],
) -> dict[str, str] | None:
    """Return the first signal whose ``signal_type`` is in
    :data:`KILL_EVENT_SIGNAL_TYPES`, or None if none match.

    Used by :func:`classify_pipeline` to decide whether to set the
    Aschenbrenner-kill-event flag that Process B (the kill-switch monitor)
    polls each cycle.
    """
    for sig in signals:
        if sig.get("signal_type") in KILL_EVENT_SIGNAL_TYPES:
            return sig
    return None


def record_firing(
    firing_log_path: Path,
    fired_at: str,
    trigger_type: str,
    triggering_artifact: dict[str, Any] | None,
    mandatory_escalation: bool,
    loop1_firing_id: str | None,
) -> dict[str, Any]:
    """Atomic append to the firing log state file. Returns the updated log doc."""
    log = load_firing_log(firing_log_path)
    log.setdefault("firings", []).append(
        {
            "fired_at": fired_at,
            "trigger_type": trigger_type,
            "triggering_artifact": triggering_artifact,
            "mandatory_escalation": mandatory_escalation,
            "loop1_firing_id": loop1_firing_id,
        }
    )
    log["updated_at"] = fired_at
    log["schema_version"] = log.get("schema_version", "1.0")
    save_firing_log(firing_log_path, log)
    return log


# ---------------------------------------------------------------------------
# Composite pipeline (callable end-to-end with optional LLM-verdict injection)
# ---------------------------------------------------------------------------


def classify_pipeline(
    artifact: ArtifactInput,
    *,
    firing_log_path: Path = DEFAULT_FIRING_LOG_PATH,
    llm_verdict: dict[str, Any] | None = None,
    persist: bool = False,
    loop1_firing_id: str | None = None,
    kill_switch_state_dir: Path | None = None,
) -> TraceEntry:
    """End-to-end pipeline.

    1. Run :func:`pre_filter`. If it decides, the result is final.
    2. Otherwise the orchestrator MUST have already invoked the classifier
       subagent and passed its JSON via ``llm_verdict``. We
       :func:`finalize_from_llm_verdict` it.
    3. Run :func:`apply_rate_limit` against the firing log.
    4. If ``persist=True`` AND the firing happens, :func:`record_firing`.
    5. If ``persist=True`` AND the classification's mandatory_escalation_signals
       contain a kill-event signal type (see :data:`KILL_EVENT_SIGNAL_TYPES`),
       set the Aschenbrenner-kill-event flag read by Process B. The flag set
       happens regardless of rate-limit suppression — kill-events override
       cost-control.

    Returns a TraceEntry whose output is the merged decision dict.

    Raises:
        ValueError: pre-filter returned None AND no ``llm_verdict`` was provided.
    """
    result = pre_filter(artifact)
    if result is None:
        if llm_verdict is None:
            raise ValueError(
                f"pre_filter could not decide source={artifact.source!r}; "
                "orchestrator must invoke the thematic-artifact-classifier subagent "
                "and pass its JSON verdict via llm_verdict"
            )
        result = finalize_from_llm_verdict(llm_verdict, artifact.snippet)

    firing_log = load_firing_log(firing_log_path)
    decision = apply_rate_limit(result, firing_log, artifact.fetched_at)

    if persist and decision.fired:
        firing_log = record_firing(
            firing_log_path=firing_log_path,
            fired_at=artifact.fetched_at,
            trigger_type="substantive_artifact",
            triggering_artifact={
                "source": artifact.source,
                "url": artifact.url,
                "tier": result.tier,
            },
            mandatory_escalation=decision.mandatory_escalation_applied,
            loop1_firing_id=loop1_firing_id,
        )

    kill_event_flag_set = None
    if persist:
        kill_signal = extract_kill_event_signal(
            result.mandatory_escalation_signals
        )
        if kill_signal is not None:
            from .kill_switch import state as kill_switch_state

            flag = kill_switch_state.set_kill_event(
                signal_type=kill_signal.get("signal_type", "unknown"),
                matched_phrase=kill_signal.get("matched_phrase", ""),
                source_artifact_url=artifact.url,
                notes=(
                    f"Auto-set from artifact_classifier; source={artifact.source}, "
                    f"loop1_firing_id={loop1_firing_id or 'n/a'}"
                ),
                state_dir=kill_switch_state_dir,
                now_iso=artifact.fetched_at,
            )
            kill_event_flag_set = {
                "fired": flag.fired,
                "fired_at": flag.fired_at,
                "signal_type": flag.signal_type,
                "matched_phrase": flag.matched_phrase,
            }

    return TraceEntry(
        tool=TOOL,
        inputs={
            "artifact_source": artifact.source,
            "artifact_url": artifact.url,
            "artifact_length_chars": artifact.snippet_length_chars,
            "artifact_is_thread": artifact.is_thread,
            "artifact_author_handle": artifact.author_handle,
            "fetched_at": artifact.fetched_at,
            "firing_log_path": str(firing_log_path),
            "llm_verdict_provided": llm_verdict is not None,
            "persist": persist,
            "loop1_firing_id": loop1_firing_id,
            "kill_switch_state_dir": (
                str(kill_switch_state_dir) if kill_switch_state_dir else None
            ),
        },
        output={
            "classification": result.to_dict(),
            "rate_limit_decision": asdict(decision),
            "n_firings_in_log_after": len(firing_log.get("firings", [])),
            "kill_event_flag_set": kill_event_flag_set,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.artifact_classifier",
        description=(
            "Run the deterministic pre-filter against an artifact JSON file. "
            "When the pre-filter cannot decide, the CLI exits with code 2 + a "
            "message instructing the orchestrator to invoke the LLM classifier."
        ),
    )
    p.add_argument("--artifact-json", type=Path, required=True)
    p.add_argument(
        "--firing-log",
        type=Path,
        default=DEFAULT_FIRING_LOG_PATH,
        help=f"Firing log state file (default {DEFAULT_FIRING_LOG_PATH}).",
    )
    p.add_argument(
        "--llm-verdict-json",
        type=Path,
        default=None,
        help="Optional path to the classifier subagent's JSON verdict.",
    )
    p.add_argument(
        "--persist",
        action="store_true",
        help="If set + firing decision is fire, append to the firing log.",
    )
    args = p.parse_args()

    raw = json.loads(args.artifact_json.read_text(encoding="utf-8"))
    artifact = ArtifactInput(
        source=raw["source"],
        snippet=raw["snippet"],
        snippet_length_chars=int(
            raw.get("snippet_length_chars", len(raw["snippet"]))
        ),
        fetched_at=raw["fetched_at"],
        url=raw.get("url"),
        is_thread=bool(raw.get("is_thread", False)),
        thread_total_words=int(raw.get("thread_total_words", 0)),
        author_handle=raw.get("author_handle"),
    )

    llm_verdict = None
    if args.llm_verdict_json:
        llm_verdict = json.loads(args.llm_verdict_json.read_text(encoding="utf-8"))

    try:
        entry = classify_pipeline(
            artifact,
            firing_log_path=args.firing_log,
            llm_verdict=llm_verdict,
            persist=args.persist,
        )
    except ValueError as exc:
        # Surface the pre-filter-deferral case as exit code 2 so the orchestrator
        # can distinguish "need LLM" from "real error".
        print(json.dumps({"error": str(exc), "needs_llm_classifier": True}, indent=2))
        raise SystemExit(2)
    emit(entry)


if __name__ == "__main__":
    main()
