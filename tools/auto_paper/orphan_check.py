"""Orphan + stuck-closing detection for the paper-auto track.

Shared core for two consumers:
  * the orphan-zero baseline (one-shot inventory, scripts/orphan_zero_baseline.py)
  * the post-RTH stuck-closing reconciler (Mode A fix, reconcile.reconcile_stuck_closing)

Reads ``ledgers/paper-auto/*.yml``, classifies each by ``meta.state``, and
reconciles that view against a broker-holdings snapshot.

State vocabulary (verified 2026-06-07): submitted -> starter -> {closed,
pending_close}. There is NO "open" state; ``starter`` is the active-held state.

PROTECT model (decided 2026-06-07): **dynamic = the set of tickers that
currently have a ``starter`` ledger.** So ``orphan_set = broker_holdings -
starter_tickers``. No ticker names are hard-coded anywhere, so the allowlist
cannot rot as positions turn over.

Corrupt-ledger guard: a ledger whose YAML fails to parse is surfaced as a
DISTINCT ``corrupt`` finding -- it is NEVER silently treated as an orphan or a
starter. This is the exact failure mode the candidate-ledger gate hit on
2026-06-06 (a sub-agent appended trace entries outside the mapping and broke the
document); without this guard a corrupt starter ledger would make its live
broker position look like an unknown orphan.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from . import state

# --- state vocabulary ---
SUBMITTED = "submitted"
STARTER = "starter"
CLOSED = "closed"
PENDING_CLOSE = "pending_close"
# Ledger says the position is done; the broker may still hold it (Mode A).
STUCK_STATES = (CLOSED, PENDING_CLOSE)


@dataclass
class LedgerScan:
    """Result of scanning every paper-auto ledger on disk."""
    by_state: dict[str, set[str]]          # state -> {tickers}
    corrupt: list[tuple[str, str]]         # [(path, first_error_line)]
    docs: dict[str, dict[str, Any]]        # ticker -> parsed doc (parseable only)

    @property
    def starter(self) -> set[str]:
        return set(self.by_state.get(STARTER, set()))

    def tickers_in(self, *states: str) -> set[str]:
        out: set[str] = set()
        for s in states:
            out |= self.by_state.get(s, set())
        return out


def _meta_state(doc: dict[str, Any]) -> str:
    return ((doc.get("meta") or {}).get("state")) or "unknown"


def scan_ledgers(ledger_dir: Optional[str] = None) -> LedgerScan:
    """Parse every ``*.yml`` under the paper-auto ledger dir, grouped by state.

    Parse failures land in ``corrupt`` and are excluded from ``by_state`` /
    ``docs`` -- the caller decides how to surface them.
    """
    d = ledger_dir or state.PAPER_AUTO_LEDGER_DIR
    by_state: dict[str, set[str]] = {}
    corrupt: list[tuple[str, str]] = []
    docs: dict[str, dict[str, Any]] = {}
    for p in sorted(glob.glob(os.path.join(d, "*.yml"))):
        ticker = os.path.splitext(os.path.basename(p))[0].upper()
        try:
            with open(p, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            first = (str(exc).splitlines() or ["YAMLError"])[0]
            corrupt.append((p, first))
            continue
        if not isinstance(doc, dict):
            corrupt.append((p, f"top-level YAML is {type(doc).__name__}, expected mapping"))
            continue
        by_state.setdefault(_meta_state(doc), set()).add(ticker)
        docs[ticker] = doc
    return LedgerScan(by_state=by_state, corrupt=corrupt, docs=docs)


@dataclass
class OrphanReport:
    broker_holdings: dict[str, float]
    starter_tickers: list[str]
    protect_set: list[str]
    orphan_set: list[str]
    corrupt_ledgers: list[tuple[str, str]]

    @property
    def is_clean(self) -> bool:
        """True iff no orphans AND no corrupt ledgers (safe to proceed)."""
        return not self.orphan_set and not self.corrupt_ledgers

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker_holdings": self.broker_holdings,
            "starter_tickers": self.starter_tickers,
            "protect_set": self.protect_set,
            "orphan_set": self.orphan_set,
            "corrupt_ledgers": [list(c) for c in self.corrupt_ledgers],
        }


def _held_tickers(broker_holdings: dict[str, float]) -> set[str]:
    """Tickers with a non-trivial broker position (|qty| >= 1, longs OR shorts)."""
    return {t.upper() for t, q in broker_holdings.items() if q and abs(float(q)) >= 1}


def compute_orphans(
    broker_holdings: dict[str, float],
    scan: Optional[LedgerScan] = None,
) -> OrphanReport:
    """orphan_set = broker_holdings - starter_ledgers (dynamic PROTECT = starter).

    Args:
        broker_holdings: {ticker: signed_qty} live broker snapshot.
        scan: pre-computed LedgerScan (else scanned fresh). Test seam.
    """
    scan = scan or scan_ledgers()
    starter = scan.starter
    protect = set(starter)                      # dynamic PROTECT
    held = _held_tickers(broker_holdings)
    orphans = sorted(held - starter - protect)
    return OrphanReport(
        broker_holdings={k.upper(): v for k, v in broker_holdings.items()},
        starter_tickers=sorted(starter),
        protect_set=sorted(protect),
        orphan_set=orphans,
        corrupt_ledgers=list(scan.corrupt),
    )


def stuck_closing_candidates(
    broker_holdings: dict[str, float],
    scan: Optional[LedgerScan] = None,
) -> list[str]:
    """Tickers whose ledger says {closed, pending_close} but the broker still
    holds >= 1 share -- the Mode A 'stuck-closing' set (DAY order expired
    unfilled). Returned sorted.
    """
    scan = scan or scan_ledgers()
    done = scan.tickers_in(*STUCK_STATES)
    held = _held_tickers(broker_holdings)
    return sorted(done & held)
