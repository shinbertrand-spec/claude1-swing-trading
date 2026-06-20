"""Write-ahead intent journal for the paper-auto entry path.

The placement invariant (Knight-Capital class safety): **there is never a
broker order the framework doesn't know about, and never an order-intent that
isn't eventually reconciled to a fill, a cancel, or an abandonment.**

Before the v2 entry path placed an order *then* wrote the ledger. Any crash or
non-schema exception between place and ledger-write left a live, unledgered,
unstopped order — invisible to ``reconcile_today`` (no positions.json row) and
to the orphan sweep (a resting order is not yet a *holding*). This module is
the durable breadcrumb that closes that window:

    write intent (status=intent)  --durable, BEFORE the broker call
        -> place at broker
    update intent (status=placed, broker_order_id=...)  --durable
        -> write the canonical submitted ledger + positions.json
    update intent (status=ledgered)  --terminal for the happy path

A crash at *any* step leaves a recoverable on-disk intent. The recovery sweep
(:func:`tools.auto_paper.reconcile.reconcile_intents`) reads every non-terminal
intent, checks it against live broker orders, and drives it to a terminal
state: ``ledgered`` (reconstruct the ledger from the intent) or ``abandoned``
(the order never reached the broker).

Storage: one YAML per intent under ``<positions.json dir>/intents/`` (i.e.
``journal/paper-auto/intents/``), gitignored. The directory is derived from
:data:`tools.auto_paper.state.PAPER_AUTO_POSITIONS_JSON` at call time so test
fixtures that redirect the positions index automatically redirect intents too.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write never
leaves a half-written, unparseable intent.
"""
from __future__ import annotations

import datetime as _dt
import glob
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

from . import state

# --- status vocabulary -----------------------------------------------------
STATUS_INTENT = "intent"        # write-ahead written; broker NOT yet called
STATUS_PLACED = "placed"        # broker accepted the order; ledger NOT yet written
STATUS_LEDGERED = "ledgered"    # canonical submitted ledger + positions.json written
STATUS_RECONCILED = "reconciled"  # recovery sweep adopted/closed it out-of-band
STATUS_ABANDONED = "abandoned"  # order never reached the broker (clean terminal)

# Non-terminal states the recovery sweep must resolve.
UNRESOLVED_STATES = frozenset({STATUS_INTENT, STATUS_PLACED})
# Terminal states — no further action; safe to ignore / archive.
TERMINAL_STATES = frozenset({STATUS_LEDGERED, STATUS_RECONCILED, STATUS_ABANDONED})

_CLOID_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# paths (derived dynamically so monkeypatching state redirects intents too)
# ---------------------------------------------------------------------------


def intents_dir() -> str:
    """Directory holding intent journals — sibling ``intents/`` of positions.json."""
    return os.path.join(os.path.dirname(state.PAPER_AUTO_POSITIONS_JSON), "intents")


def intent_path(client_order_id: str) -> str:
    return os.path.join(intents_dir(), f"{client_order_id}.yml")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def make_cloid(ticker: str, run_tag: Optional[str] = None) -> str:
    """Deterministic client-order-id (idempotency key).

    Same (ticker, run_tag) → same id, so a re-run inside the same entry run
    collides with the existing intent rather than minting a second one. The
    ``run_tag`` is the run-dir basename when available (one entry run per
    cron fire), else the trade date — both stable within a placement attempt.
    """
    tag = run_tag or _today_iso()
    raw = f"ap-{ticker.upper()}-{tag}"
    return _CLOID_SAFE.sub("-", raw)


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@dataclass
class IntentRecord:
    """A single write-ahead order intent.

    Carries every field needed to reconstruct the canonical submitted ledger
    if the process dies after the broker accepts the order but before the
    ledger is written — the recovery sweep rebuilds the ledger from this.
    """
    client_order_id: str
    ticker: str
    setup_type: str
    setup_grade: Optional[str]
    pivot_price: float
    limit_price: float
    stop_price: float
    target_price: Optional[float]
    shares: int
    broker: str = "tiger_paper"
    sector_etf: Optional[str] = None
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)
    run_dir: Optional[str] = None
    status: str = STATUS_INTENT
    broker_order_id: Optional[int] = None
    note: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    # Number of CONSECUTIVE distinct-date sweeps that — with the broker PROVEN
    # alive (account non-zero) — confirmed no matching order for this still-
    # `intent` record. Only counts when the broker is provably honest AND
    # genuinely empty; RESET to 0 the moment the broker shows real order/position
    # data (proof it is not blacked out). A status=intent record is abandoned
    # only after this reaches the quorum (>= 2) across distinct dates — so a
    # blackout, a lying empty read, or two same-day calls can never torch a
    # genuinely-live order.
    empty_sweep_count: int = 0
    # UTC date (YYYY-MM-DD) of the last sweep counted toward the abandon quorum.
    # Gates the count to one-per-day so the morning + EOD reconcile passes can't
    # both increment in a single calendar day.
    last_empty_sweep_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IntentRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_unresolved(self) -> bool:
        return self.status in UNRESOLVED_STATES


# ---------------------------------------------------------------------------
# atomic I/O
# ---------------------------------------------------------------------------


def _atomic_write_yaml(path: str, doc: dict[str, Any]) -> None:
    """Write YAML atomically: temp file in the same dir + fsync + os.replace.

    os.replace is atomic on both POSIX and Windows (same filesystem), so a
    crash mid-write can only leave the *old* intact file or the *new* intact
    file — never a truncated one.
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_intent(record: IntentRecord) -> str:
    """Persist a fresh intent (write-ahead). Stamps timestamps. Returns path.

    Refuses to clobber an existing intent for the same client_order_id —
    that would silently lose the prior attempt's broker_order_id. Use
    :func:`update_intent` to advance an existing intent.
    """
    p = intent_path(record.client_order_id)
    if os.path.isfile(p):
        raise FileExistsError(
            f"intent {record.client_order_id} already exists at {p}; "
            f"use update_intent to advance it"
        )
    now = _now_iso()
    record.created_at = record.created_at or now
    record.updated_at = now
    _atomic_write_yaml(p, record.to_dict())
    return p


def load_intent(client_order_id: str) -> Optional[IntentRecord]:
    p = intent_path(client_order_id)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        return None
    return IntentRecord.from_dict(doc)


def update_intent(client_order_id: str, **fields: Any) -> IntentRecord:
    """Read-modify-atomic-write an existing intent. Returns the updated record.

    Raises FileNotFoundError if the intent does not exist.
    """
    rec = load_intent(client_order_id)
    if rec is None:
        raise FileNotFoundError(f"no intent for {client_order_id}")
    for k, v in fields.items():
        if not hasattr(rec, k):
            raise AttributeError(f"IntentRecord has no field {k!r}")
        setattr(rec, k, v)
    rec.updated_at = _now_iso()
    _atomic_write_yaml(intent_path(client_order_id), rec.to_dict())
    return rec


def mark(client_order_id: str, status: str, **extra: Any) -> IntentRecord:
    """Convenience: set status (+ optional extra fields) on an intent."""
    return update_intent(client_order_id, status=status, **extra)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def iter_intents() -> list[IntentRecord]:
    """All parseable intents on disk. Unparseable files are skipped silently
    (the recovery sweep surfaces a corrupt-intent finding separately)."""
    out: list[IntentRecord] = []
    for p in sorted(glob.glob(os.path.join(intents_dir(), "*.yml"))):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("client_order_id"):
            out.append(IntentRecord.from_dict(doc))
    return out


def corrupt_intents() -> list[str]:
    """Paths of intent files that exist but fail to parse (manual-fix surface)."""
    bad: list[str] = []
    for p in sorted(glob.glob(os.path.join(intents_dir(), "*.yml"))):
        try:
            with open(p, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            if not isinstance(doc, dict) or not doc.get("client_order_id"):
                bad.append(p)
        except yaml.YAMLError:
            bad.append(p)
    return bad


def unresolved_intents() -> list[IntentRecord]:
    """Intents in a non-terminal state — the recovery sweep's work list."""
    return [r for r in iter_intents() if r.is_unresolved]


def has_unresolved_intent(ticker: str) -> bool:
    """True iff any non-terminal intent exists for this ticker.

    Used by ``place_candidate`` to refuse a second placement while a prior
    intent for the same name is still dangling (re-fire double-place guard).
    """
    t = ticker.upper()
    return any(r.ticker.upper() == t and r.is_unresolved for r in iter_intents())
