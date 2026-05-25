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
    now = _now_iso()
    doc: dict[str, Any] = {
        "meta": {
            "schema_version": "1.0",
            "ticker": ticker.upper(),
            "asof": now,
            "state": "submitted",
            "account_track": "paper-auto",
            "ledger_path": p.replace("\\", "/"),
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
                "trigger": "EPGap" if setup_type == "EP" else "VCPBreakout",
                "fill_date": _today(),
                "shares": int(shares),
                # On `submitted`, no fill yet — set fill_price to the limit
                # so the schema's required-field check passes. EOD
                # reconciliation overwrites with the actual avg_fill_price.
                "fill_price": float(limit_price),
                "limit_price_placed": float(limit_price),
                "initial_stop": float(stop_price),
                "trace_refs": [],
            },
            "current_stop": float(stop_price),
            "trail_state_legacy": "initial",
            "alerts_sent": [],
        },
        "reasoning_trace": reasoning_trace or [],
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
