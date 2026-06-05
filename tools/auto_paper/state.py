"""Paper-auto ledger I/O + positions.json append.

Per CLAUDE.md § Broker bridge → § Paper-auto carve-out: paper-auto positions
live in a parallel directory (``ledgers/paper-auto/<TICKER>.yml``) and a
parallel positions index (``journal/paper-auto/positions.json``), separate
from the human-discretionary track.

This module owns the file-system contract — read, validate, write, append.
The pipeline module composes these calls with the broker layer + sizing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

import yaml

PAPER_AUTO_LEDGER_DIR = "ledgers/paper-auto"
PAPER_AUTO_POSITIONS_JSON = "journal/paper-auto/positions.json"
SCHEMA_PATH = "ledgers/_schema/ledger.schema.json"


class PaperAutoStateError(RuntimeError):
    """Raised on validation failure or attempted overwrite."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return _dt.date.today().isoformat()


def ledger_path(ticker: str) -> str:
    """Return the canonical paper-auto ledger path for a ticker."""
    return os.path.join(PAPER_AUTO_LEDGER_DIR, f"{ticker.upper()}.yml")


def ledger_exists(ticker: str) -> bool:
    """True iff a paper-auto ledger file exists for this ticker."""
    return os.path.isfile(ledger_path(ticker))


def load_ledger(ticker: str) -> dict[str, Any]:
    """Load + return the paper-auto ledger YAML for a ticker."""
    p = ledger_path(ticker)
    if not os.path.isfile(p):
        raise PaperAutoStateError(f"no paper-auto ledger for {ticker} at {p}")
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _coerce_for_json(o: Any) -> Any:
    """Recursively coerce date / datetime to ISO strings for JSON-schema validation."""
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: _coerce_for_json(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_coerce_for_json(v) for v in o]
    return o


def _validate_against_schema(doc: dict[str, Any]) -> None:
    """Validate a ledger dict against the canonical schema.

    Raises:
        PaperAutoStateError: with the schema-validation error message.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise PaperAutoStateError("jsonschema not installed") from exc

    try:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            schema = json.load(fh)
    except FileNotFoundError as exc:
        raise PaperAutoStateError(f"schema not found at {SCHEMA_PATH}") from exc

    try:
        jsonschema.validate(
            _coerce_for_json(doc), schema, cls=jsonschema.Draft202012Validator,
        )
    except jsonschema.ValidationError as exc:
        raise PaperAutoStateError(f"ledger schema validation failed: {exc.message}") from exc


_KIND_REGISTRY_SETUPS = {
    "dual_ma_trend_following",
    "xs_short_term_reversal",
    "connors_rsi2",
    "clenow_momentum",
    # _liquid_us variants — same KIND_REGISTRY kinds, different (wide-US)
    # universe. setup_type values are spec FILENAMES so each variant
    # carries a distinct tag for ledger-level traceability, but the
    # trigger semantics (QuantSignal) are the same as the base kinds.
    "clenow_momentum_liquid_us",
    "residual_momentum_liquid_us",
    "xs_short_term_reversal_liquid_us",
    "ts_momentum_liquid_us",
}


def _assign_trace_ids(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp sequential ``id`` (1-indexed) onto trace entries that lack one.

    Per the TraceEntry contract: ``id: int | None`` is set when appended to
    a ledger's ``reasoning_trace``. This function is where the appending
    happens; without it, scanner-emitted traces fail the ledger schema's
    ``id: required`` rule. Idempotent — re-assigns IDs cleanly if all
    entries already have one (preserves existing ints, only fills None).
    """
    out: list[dict[str, Any]] = []
    next_id = 1
    used: set[int] = {
        t["id"] for t in traces
        if isinstance(t, dict) and isinstance(t.get("id"), int)
    }
    for t in traces:
        if not isinstance(t, dict):
            out.append(t)
            continue
        t2 = dict(t)
        if not isinstance(t2.get("id"), int):
            while next_id in used:
                next_id += 1
            t2["id"] = next_id
            used.add(next_id)
            next_id += 1
        out.append(t2)
    return out


def _trigger_for(setup_type: str) -> str:
    """Map a setup_type to the position_state.starter.trigger enum value.

    The trigger field is a leg-level tag (per swing-momentum-execution).
    KIND_REGISTRY family (quant scanner sources) use QuantSignal; the
    SETUP_REPLAY family preserves its original trigger names.
    """
    if setup_type == "EP":
        return "EPGap"
    if setup_type in _KIND_REGISTRY_SETUPS:
        return "QuantSignal"
    return "VCPBreakout"


def _build_submitted_doc(
    *,
    ticker: str,
    setup_type: str,
    setup_grade: str | None,
    pivot_price: float,
    limit_price: float,
    stop_price: float,
    shares: int,
    broker_order_id: int | None,
    broker: str,
    sector_etf: str | None = None,
    reasoning_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compose the ``submitted``-state ledger dict. Pure — no I/O, no schema
    validation. Shared by :func:`validate_submitted_ledger` (pre-place gate)
    and :func:`write_submitted_ledger` (post-place persist) so both operate on
    a byte-for-byte identical structure (modulo timestamps + broker_order_id).
    """
    now = _now_iso()
    doc: dict[str, Any] = {
        "meta": {
            "schema_version": "1.0",
            "ticker": ticker.upper(),
            "asof": now,
            "state": "submitted",
            "account_track": "paper-auto",
            "ledger_path": ledger_path(ticker).replace("\\", "/"),
            "created_by": "auto_paper/pipeline",
            "created_at": now,
        },
        "setup_classification": {
            "type": setup_type,
            "pivot_price": float(pivot_price),
            "stop_price": float(stop_price),
            "stop_distance_pct": (limit_price - stop_price) / limit_price if limit_price > 0 else 0.0,
            "trace_refs": [],
            "confluence_checklist": [],
        },
        "position_state": {
            "stage": "STARTER",
            "intended_full_shares": int(shares),
            "starter": {
                "trigger": _trigger_for(setup_type),
                "fill_date": _today(),
                # On `submitted`, no fill yet — set fill_price to the limit
                # so the schema's required-field check passes. EOD
                # reconciliation overwrites with the actual avg_fill_price.
                "shares": int(shares),
                "fill_price": float(limit_price),
                "limit_price_placed": float(limit_price),
                "initial_stop": float(stop_price),
                "trace_refs": [],
            },
            "current_stop": float(stop_price),
            "trail_state_legacy": "initial",
            "alerts_sent": [],
        },
        # 2026-05-28: assign sequential ids to reasoning_trace entries that
        # arrive id-less. TraceEntry's contract is `id: int | None — set when
        # appended to a ledger reasoning_trace`; this is where the appending
        # happens, so this is where ids are assigned. Without this, the ledger
        # schema's `id: required` rule rejects the doc.
        "reasoning_trace": _assign_trace_ids(reasoning_trace or []),
    }
    if setup_grade is not None:
        doc["setup_classification"]["grade"] = setup_grade
    if broker_order_id is not None:
        doc["position_state"]["starter"]["broker_order_id"] = int(broker_order_id)
    if broker:
        doc["position_state"]["starter"]["broker"] = broker
    if sector_etf:
        doc["regime"] = {
            "sector_etf": sector_etf,
            "computed_at": now,
        }
    return doc


def validate_submitted_ledger(
    *,
    ticker: str,
    setup_type: str,
    setup_grade: str | None,
    pivot_price: float,
    limit_price: float,
    stop_price: float,
    shares: int,
    broker: str,
    sector_etf: str | None = None,
    reasoning_trace: list[dict[str, Any]] | None = None,
    overwrite: bool = False,
) -> None:
    """Pre-flight gate: build + schema-validate a submitted ledger WITHOUT
    writing or any broker call.

    The pipeline calls this BEFORE ``place_limit_buy`` so a schema failure
    aborts the placement instead of orphaning a live broker order (the
    2026-06-04 v2-shadow failure mode). ``broker_order_id`` is intentionally
    omitted — it is unknown pre-place and is an *optional* schema field, so its
    later addition in :func:`write_submitted_ledger` cannot introduce a new
    validation error on an already-valid doc.

    Raises:
        PaperAutoStateError: on attempted overwrite or schema-validation failure.
    """
    p = ledger_path(ticker)
    if os.path.isfile(p) and not overwrite:
        raise PaperAutoStateError(
            f"paper-auto ledger for {ticker} already exists at {p}; pass overwrite=True"
        )
    doc = _build_submitted_doc(
        ticker=ticker,
        setup_type=setup_type,
        setup_grade=setup_grade,
        pivot_price=pivot_price,
        limit_price=limit_price,
        stop_price=stop_price,
        shares=shares,
        broker_order_id=None,
        broker=broker,
        sector_etf=sector_etf,
        reasoning_trace=reasoning_trace,
    )
    _validate_against_schema(doc)


def write_submitted_ledger(
    *,
    ticker: str,
    setup_type: str,
    setup_grade: str | None,
    pivot_price: float,
    limit_price: float,
    stop_price: float,
    shares: int,
    broker_order_id: int | None,
    broker: str,
    sector_etf: str | None = None,
    reasoning_trace: list[dict[str, Any]] | None = None,
    overwrite: bool = False,
) -> str:
    """Write a brand-new paper-auto ledger in the ``submitted`` state.

    Refuses to overwrite an existing ledger unless ``overwrite=True``.
    The target price is NOT stored on the ledger — targets live in
    ``journal/paper-auto/positions.json`` as ``target_1`` / ``target_2``.

    Returns:
        The path the ledger was written to.
    """
    p = ledger_path(ticker)
    if os.path.isfile(p) and not overwrite:
        raise PaperAutoStateError(
            f"paper-auto ledger for {ticker} already exists at {p}; pass overwrite=True"
        )

    os.makedirs(PAPER_AUTO_LEDGER_DIR, exist_ok=True)
    doc = _build_submitted_doc(
        ticker=ticker,
        setup_type=setup_type,
        setup_grade=setup_grade,
        pivot_price=pivot_price,
        limit_price=limit_price,
        stop_price=stop_price,
        shares=shares,
        broker_order_id=broker_order_id,
        broker=broker,
        sector_etf=sector_etf,
        reasoning_trace=reasoning_trace,
    )

    _validate_against_schema(doc)

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return p


def append_to_positions_json(entry: dict[str, Any]) -> None:
    """Append an entry to ``journal/paper-auto/positions.json``.

    Creates the file with a fresh v2-shape index if missing. Refuses to
    duplicate a ticker (raises :class:`PaperAutoStateError`).
    """
    os.makedirs(os.path.dirname(PAPER_AUTO_POSITIONS_JSON), exist_ok=True)
    if os.path.isfile(PAPER_AUTO_POSITIONS_JSON):
        with open(PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {
            "_account_track": "paper-auto",
            "_schema_version": "v2",
            "_description": "Autonomous paper-trade positions. Separate from human-discretionary journal/positions.json. See CLAUDE.md § Broker bridge → § Paper-auto carve-out.",
            "updated": _now_iso(),
            "positions": [],
        }

    existing = {p.get("ticker") for p in data.get("positions", [])}
    if entry.get("ticker") in existing:
        raise PaperAutoStateError(
            f"duplicate ticker {entry.get('ticker')} in {PAPER_AUTO_POSITIONS_JSON}"
        )

    data.setdefault("positions", []).append(entry)
    data["updated"] = _now_iso()

    with open(PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_positions_json() -> dict[str, Any]:
    """Return the parsed paper-auto positions index, or an empty index if missing."""
    if not os.path.isfile(PAPER_AUTO_POSITIONS_JSON):
        return {"positions": []}
    with open(PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Run-status helpers — per [auto-paper LLM/Python boundary refactor 2026-05-28]
# Each /auto-paper invocation creates one run directory under
# ``ledgers/_auto_paper_runs/<run_id>/`` (gitignored). ``_status.yml`` lives
# at the root of that directory and tracks phase progression so resume-from-
# failure is trivial.
# ---------------------------------------------------------------------------

_VALID_STATUS_PHASES = ("init", "post_skeptic", "post_panel")


def write_run_status(
    run_dir: str | os.PathLike,
    *,
    phase: str,
    started_at: str | None = None,
    completed_at: str | None = None,
    **extra_fields: Any,
) -> str:
    """Append-or-update the per-run ``_status.yml`` at ``<run_dir>/_status.yml``.

    Schema (§2 of the boundary spec):

        run_id: <basename of run_dir>
        run_started_at: <ISO timestamp; first write only>
        last_phase_completed: init | post_skeptic | post_panel | null
        phases_completed:
          - phase: init
            completed_at: <ISO timestamp>
            <plus any **extra_fields the caller passed at completion time>
          - ...
        errors: []

    Calling with ``started_at`` initialises the file. Calling with
    ``completed_at`` (and an existing file) appends the phase to
    ``phases_completed`` + advances ``last_phase_completed``.

    Args:
        run_dir: directory holding the run artifacts.
        phase: one of ``init`` / ``post_skeptic`` / ``post_panel``.
        started_at: when set, treats this as the FIRST status write
            (creates / overwrites the file).
        completed_at: when set, appends a phase-completion entry.
        **extra_fields: arbitrary key/value pairs included on the
            phase-completion entry (e.g. ``candidates_in=8``).

    Returns:
        Absolute path of the written ``_status.yml``.
    """
    if phase not in _VALID_STATUS_PHASES:
        raise ValueError(
            f"unknown phase {phase!r}; valid: {_VALID_STATUS_PHASES}"
        )
    if not (started_at or completed_at):
        raise ValueError("write_run_status requires started_at or completed_at")

    run_dir = os.fspath(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    status_path = os.path.join(run_dir, "_status.yml")

    if started_at:
        # First write — initialise the doc. Overwrite if exists (re-running
        # init creates a fresh dir anyway; this path is also used by tests).
        doc: dict[str, Any] = {
            "run_id": os.path.basename(os.path.normpath(run_dir)),
            "run_started_at": started_at,
            "last_phase_completed": None,
            "phases_completed": [],
            "errors": [],
        }
    else:
        # Append-to-existing — read, update, write back.
        if not os.path.isfile(status_path):
            raise PaperAutoStateError(
                f"write_run_status: cannot append phase {phase!r} — "
                f"no existing status at {status_path}; pass started_at to init"
            )
        with open(status_path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}

    if completed_at:
        entry: dict[str, Any] = {"phase": phase, "completed_at": completed_at}
        entry.update(extra_fields)
        doc.setdefault("phases_completed", []).append(entry)
        doc["last_phase_completed"] = phase

    with open(status_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return status_path


def read_run_status(run_dir: str | os.PathLike) -> dict[str, Any]:
    """Read and return ``<run_dir>/_status.yml`` as a dict.

    Raises :class:`PaperAutoStateError` if the file is missing or malformed.
    """
    run_dir = os.fspath(run_dir)
    status_path = os.path.join(run_dir, "_status.yml")
    if not os.path.isfile(status_path):
        raise PaperAutoStateError(
            f"read_run_status: no status file at {status_path}"
        )
    with open(status_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise PaperAutoStateError(
            f"read_run_status: malformed YAML at {status_path}"
        )
    return doc


def record_stop_order_id(ticker: str, stop_order_id: int) -> str:
    """Record the broker stop-loss order ID on the paper-auto ledger.

    Called after a successful ``TigerClient.place_stop_loss`` so that the
    next reconcile run (or monitor run) can match / cancel the stop without
    double-placing.

    Writes to ``position_state.stop_order_id`` — added to the schema in
    Session 3. Re-validates the doc before writing.

    Returns:
        The ledger path that was updated.

    Raises:
        PaperAutoStateError: if the ledger doesn't exist or fails revalidation.
    """
    p = ledger_path(ticker)
    if not os.path.isfile(p):
        raise PaperAutoStateError(f"no paper-auto ledger for {ticker}")

    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    ps = doc.setdefault("position_state", {})
    ps["stop_order_id"] = int(stop_order_id)

    doc.setdefault("meta", {})
    doc["meta"]["updated_by"] = "auto_paper/reconcile"
    doc["meta"]["updated_at"] = _now_iso()

    _validate_against_schema(doc)

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return p
