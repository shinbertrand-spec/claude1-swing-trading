"""Health & Anomalies snapshot — the read-only data layer of the cockpit.

Reads the auto-paper run/trade artifacts the pipeline already writes and derives
a typed :class:`HealthSnapshot`. This is the single source of truth for both the
static HTML panel (:mod:`tools.observability.health_html`) and the Telegram
alert dispatcher (:mod:`tools.observability.health_alerts`); a future
interactive panel can serve the same JSON and add action endpoints.

OBSERVE-ONLY (load-bearing). This module only *reads* run outputs +
``positions.json`` and runs lightweight feed probes. It never places an order,
mutates a ledger, or writes anything outside ``journal/observability/``. It is
not imported by the placement path (``run_entry`` / ``pipeline`` / ``state``).
Every sub-check is wrapped so one failure degrades a single field rather than
raising.

The headline metric is the **silent-failure detector**. The 2026-06-05→15
outage ran the scheduled entry task every morning but placed nothing — stuck in
``--dry-run`` — and printed ``placed=0`` cleanly. The detector catches exactly
that: on a *scheduled entry session*, ``intended>0 AND (placed==0 OR dry>0)``.

CLI::

    uv run python -m tools.observability.health_snapshot [--run-dir DIR] [--no-feeds]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Artifact locations (relative to repo root; overridable for tests).
RUNS_DIR_DEFAULT = _REPO_ROOT / "ledgers" / "_auto_paper_runs"
POSITIONS_JSON_DEFAULT = _REPO_ROOT / "journal" / "paper-auto" / "positions.json"
PAPER_AUTO_LEDGERS_DEFAULT = _REPO_ROOT / "ledgers" / "paper-auto"
SNAPSHOT_OUT_DEFAULT = _REPO_ROOT / "journal" / "observability" / "health_snapshot.json"

PLACEMENT_FILE = "07_placement_results.yml"
STATUS_FILE = "_status.yml"

# Write-ahead intent journal (sibling of positions.json). Read-only here — this
# module never imports the placement path; it just inspects the breadcrumbs.
INTENTS_DIRNAME = "intents"
# Non-terminal intent statuses (mirror tools.auto_paper.intent_log without
# importing it, to keep the observe-only boundary).
UNRESOLVED_INTENT_STATES = frozenset({"intent", "placed"})

# Statuses that represent a candidate that REACHED placement (i.e. intent to
# trade). 'rejected' and 'defer' are legitimate gate decisions, not intent.
INTENT_STATUSES = frozenset({"placed", "dry_run", "error"})

# Scheduled entry cron fires 9:35 ET. A generous window distinguishes the
# scheduled run from ad-hoc manual --dry-run test runs (which must NOT alarm).
ENTRY_WINDOW_START = time(9, 25)
ENTRY_WINDOW_END = time(10, 15)
# Entry is "overdue" if it's a trading day and we're past this ET time with no
# run recorded today.
ENTRY_OVERDUE_AFTER = time(10, 20)
# Monitor is "stale" during RTH if there are open positions and no ledger
# update in this many minutes.
MONITOR_STALE_MINUTES = 95
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SilentFailure:
    run_id: Optional[str]
    run_dir: Optional[str]
    session_started_iso: Optional[str]
    is_scheduled_entry: bool
    intended: int
    placed: int
    dry_run: int
    errors: int
    rejected: int
    defer: int
    n_total: int
    alarm: bool
    reason: str
    placed_not_at_broker: list[dict[str, Any]] = field(default_factory=list)
    # Write-ahead intents (v2 path) that never reached a terminal state — a
    # placed-or-intended order the framework may have lost track of. ANY of
    # these is pageable regardless of the run being scheduled: it means an
    # order intent is dangling until the recovery sweep resolves it.
    orphan_intents: list[dict[str, Any]] = field(default_factory=list)
    # NAKED held positions (FOLD-IN #1): starter ledgers with no live stop and a
    # recorded stop-placement failure — believed-protected but actually unstopped.
    unprotected_starters: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CronTask:
    name: str
    last_run_iso: Optional[str]
    overdue: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeedStatus:
    name: str
    up: bool
    last_success_iso: Optional[str]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HealthSnapshot:
    generated_at: str
    silent_failure: Optional[SilentFailure]
    cron_tasks: list[CronTask]
    feeds: list[FeedStatus]
    error_count: int
    overall_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "silent_failure": self.silent_failure.to_dict() if self.silent_failure else None,
            "cron_tasks": [c.to_dict() for c in self.cron_tasks],
            "feeds": [f.to_dict() for f in self.feeds],
            "error_count": self.error_count,
            "overall_ok": self.overall_ok,
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — missing/corrupt file must not raise
        return None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_scheduled_entry_window(session_started_iso: Optional[str]) -> bool:
    """True if the run started in the scheduled morning-entry window (ET).

    This is what separates the scheduled entry run (which SHOULD place) from an
    ad-hoc manual ``--dry-run`` test (which legitimately places nothing). Only
    scheduled runs raise the silent-failure alarm.
    """
    dt = _parse_iso(session_started_iso)
    if dt is None:
        return False
    et = dt.astimezone(ET)
    if et.weekday() >= 5:
        return False
    return ENTRY_WINDOW_START <= et.timetz().replace(tzinfo=None) <= ENTRY_WINDOW_END


# ---------------------------------------------------------------------------
# silent-failure detector
# ---------------------------------------------------------------------------


def find_latest_entry_session(runs_dir: Path = RUNS_DIR_DEFAULT) -> Optional[Path]:
    """Newest run dir that wrote a placement-results file (i.e. an entry run).

    Monitor/reconcile runs don't write ``07_placement_results.yml``. Dir names
    are ISO timestamps, so reverse-lexicographic sort == newest-first.
    """
    try:
        dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    except (OSError, FileNotFoundError):
        return None
    for d in sorted(dirs, key=lambda p: p.name, reverse=True):
        if (d / PLACEMENT_FILE).is_file():
            return d
    return None


def scan_orphan_intents(positions_json: Path = POSITIONS_JSON_DEFAULT) -> list[dict[str, Any]]:
    """Read-only scan of the write-ahead intent journal for non-terminal intents.

    Returns one dict per dangling intent (status in {intent, placed}). Empty on
    a clean track or any read error — this must never raise. Derives the intents
    dir from the positions.json path so a test-redirected index is honoured.
    """
    out: list[dict[str, Any]] = []
    try:
        intents_dir = positions_json.parent / INTENTS_DIRNAME
        if not intents_dir.is_dir():
            return []
        for p in sorted(intents_dir.glob("*.yml")):
            doc = _load_yaml(p)
            if not isinstance(doc, dict):
                continue
            status = str(doc.get("status", "")).strip()
            if status in UNRESOLVED_INTENT_STATES:
                out.append({
                    "client_order_id": doc.get("client_order_id"),
                    "ticker": doc.get("ticker"),
                    "status": status,
                    "broker_order_id": doc.get("broker_order_id"),
                    "created_at": doc.get("created_at"),
                })
    except Exception:  # noqa: BLE001 — observe-only, never raise
        return out
    return out


def _held_qty(holdings: Any, ticker: str) -> Optional[float]:
    """Return the held share count for ``ticker`` from a holdings snapshot, or
    None if ``holdings`` is None (snapshot unavailable). Accepts a
    ``{ticker: qty}`` mapping or a set/list/tuple of held tickers (treated as
    qty 1). Symbol match is case-insensitive."""
    if holdings is None:
        return None
    key = ticker.upper()
    if isinstance(holdings, dict):
        for k, v in holdings.items():
            if str(k).upper() == key:
                try:
                    return abs(float(v))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0
    if isinstance(holdings, (set, list, tuple)):
        return 1.0 if any(str(k).upper() == key for k in holdings) else 0.0
    return None


def scan_unprotected_starters(
    paper_auto_ledgers: Path = PAPER_AUTO_LEDGERS_DEFAULT,
    *,
    holdings: Any = None,
) -> list[dict[str, Any]]:
    """Read-only scan for NAKED held positions (FOLD-IN #1): a paper-auto ledger
    in ``starter`` state with NO live ``stop_order_id`` AND a recorded
    ``stop_place_error`` — a position the system believes is protected but isn't
    (e.g. a held-orphan recovery whose stop placement failed and was never
    re-armed). Surfaced onto the pageable health surface. Never raises.

    FIX #5 (false-page guard): when a ``holdings`` snapshot is supplied
    (``{ticker: qty}`` mapping or set of held tickers), only a position the
    broker STILL HOLDS (>=1 share) is naked — a position closed externally before
    its stale flag was cleared is filtered out. When ``holdings`` is None (broker
    read unavailable / blackout), the scan is conservative and surfaces the flag
    regardless, so a blackout can never silence a genuine naked position."""
    out: list[dict[str, Any]] = []
    try:
        if not paper_auto_ledgers.is_dir():
            return []
        for p in sorted(paper_auto_ledgers.glob("*.yml")):
            doc = _load_yaml(p)
            if not isinstance(doc, dict):
                continue
            meta = doc.get("meta") or {}
            ps = doc.get("position_state") or {}
            if not isinstance(meta, dict) or not isinstance(ps, dict):
                continue
            state_v = str(meta.get("state", "")).strip().lower()
            has_stop = ps.get("stop_order_id") is not None
            err = ps.get("stop_place_error")
            if not (state_v == "starter" and not has_stop and err):
                continue
            ticker = meta.get("ticker") or p.stem
            held = _held_qty(holdings, str(ticker))
            # held is None → snapshot unavailable → surface (conservative).
            # held < 1 → broker no longer holds it → not naked, skip.
            if held is not None and held < 1:
                continue
            out.append({
                "ticker": ticker,
                "stop_place_error": str(err),
            })
    except Exception:  # noqa: BLE001 — observe-only, never raise
        return out
    return out


def _fetch_broker_holdings() -> Optional[dict[str, float]]:
    """Best-effort live holdings snapshot ``{ticker: qty}`` for the naked-starter
    guard (FIX #5). Returns None on any failure / blackout — callers treat None as
    "unavailable" and stay conservative (surface the flag). Never raises."""
    try:
        from ..broker.tiger import TigerClient
        client = TigerClient(allow_live=False)
        positions = client.positions().output["positions"]
        out: dict[str, float] = {}
        for p in positions:
            sym = str(p.get("symbol", "")).upper()
            if sym:
                try:
                    out[sym] = abs(float(p.get("quantity", 0)))
                except (TypeError, ValueError):
                    out[sym] = 0.0
        return out
    except Exception:  # noqa: BLE001 — observe-only, never raise
        return None


def compute_silent_failure(
    run_dir: Path,
    *,
    positions_json: Path = POSITIONS_JSON_DEFAULT,
    paper_auto_ledgers: Path = PAPER_AUTO_LEDGERS_DEFAULT,
    holdings: Any = None,
) -> SilentFailure:
    """Compute the silent-failure verdict for one entry-session run dir.

    ``holdings`` is an optional ``{ticker: qty}`` broker snapshot (or set of held
    tickers) used to gate the naked-starter scan — a starter no longer held is
    not naked (FIX #5). None → conservative (surface regardless)."""
    placement = _load_yaml(run_dir / PLACEMENT_FILE)
    status = _load_yaml(run_dir / STATUS_FILE)

    results = []
    if isinstance(placement, dict):
        results = placement.get("results") or []
    if not isinstance(results, list):
        results = []

    counts = {"placed": 0, "dry_run": 0, "error": 0, "rejected": 0, "defer": 0}
    placed_rows: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status", "")).strip()
        if st in counts:
            counts[st] += 1
        if st == "placed":
            placed_rows.append(row)

    intended = counts["placed"] + counts["dry_run"] + counts["error"]
    placed = counts["placed"]
    dry = counts["dry_run"]

    session_started = None
    run_id = run_dir.name
    if isinstance(status, dict):
        session_started = status.get("run_started_at")
        run_id = status.get("run_id") or run_id

    # Write-ahead intent orphans (v2 path). A dangling intent is ALWAYS
    # pageable — it means an order intent the framework hasn't reconciled to a
    # fill/cancel/abandon — independent of whether this run was scheduled.
    orphan_intents = scan_orphan_intents(positions_json)
    # NAKED held starters (FOLD-IN #1) — believed-protected but unstopped.
    # Gated on a live holdings snapshot (FIX #5): a starter no longer held is
    # not naked; None → conservative (surface regardless).
    unprotected_starters = scan_unprotected_starters(paper_auto_ledgers, holdings=holdings)

    is_scheduled = _is_scheduled_entry_window(session_started)
    raw_anomaly = intended > 0 and (placed == 0 or dry > 0)
    alarm = bool((is_scheduled and raw_anomaly) or orphan_intents or unprotected_starters)

    if unprotected_starters:
        reason = (
            f"{len(unprotected_starters)} NAKED held starter(s) "
            f"({', '.join(sorted({str(o.get('ticker')) for o in unprotected_starters}))}) "
            f"— position believed protected but has NO live stop (placement failed)"
        )
    elif orphan_intents:
        reason = (
            f"{len(orphan_intents)} unresolved write-ahead intent(s) "
            f"({', '.join(sorted({str(o.get('ticker')) for o in orphan_intents}))}) "
            f"— order intent dangling, not yet reconciled to fill/cancel"
        )
    elif is_scheduled and raw_anomaly:
        reason = (
            f"scheduled entry session placed {placed} of {intended} intended "
            f"({dry} dry-run) — silent failure"
        )
    elif raw_anomaly and not is_scheduled:
        reason = "anomaly present but session is not a scheduled entry run (ad-hoc/manual) — not paged"
    elif intended == 0:
        reason = "no candidates reached placement (all rejected/deferred or none) — nominal"
    else:
        reason = f"nominal — {placed} of {intended} intended placed"

    # Desync cross-check: each placed row should be in positions.json as starter.
    placed_not_at_broker = _placed_vs_broker(placed_rows, positions_json)

    return SilentFailure(
        run_id=run_id,
        run_dir=str(run_dir),
        session_started_iso=session_started,
        is_scheduled_entry=is_scheduled,
        intended=intended,
        placed=placed,
        dry_run=dry,
        errors=counts["error"],
        rejected=counts["rejected"],
        defer=counts["defer"],
        n_total=len(results),
        alarm=alarm,
        reason=reason,
        placed_not_at_broker=placed_not_at_broker,
        orphan_intents=orphan_intents,
        unprotected_starters=unprotected_starters,
    )


def _placed_vs_broker(
    placed_rows: list[dict[str, Any]],
    positions_json: Path,
) -> list[dict[str, Any]]:
    """Flag placed rows whose order id / ticker is absent from positions.json."""
    if not placed_rows:
        return []
    doc = _load_json(positions_json)
    positions = []
    if isinstance(doc, dict):
        positions = doc.get("positions") or []
    if not isinstance(positions, list):
        positions = []

    by_ticker: dict[str, set] = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        tkr = str(p.get("ticker", "")).upper()
        if not tkr:
            continue
        by_ticker.setdefault(tkr, set()).add(p.get("broker_order_id"))

    missing: list[dict[str, Any]] = []
    for row in placed_rows:
        tkr = str(row.get("ticker", "")).upper()
        oid = row.get("broker_order_id")
        ids = by_ticker.get(tkr)
        if ids is None or (oid is not None and oid not in ids):
            missing.append({"ticker": tkr, "broker_order_id": oid})
    return missing


# ---------------------------------------------------------------------------
# cron health
# ---------------------------------------------------------------------------


def _market_closed_on(d) -> bool:
    try:
        from ..market_calendar import compute
        return bool(compute(d).output["is_closed"])
    except Exception:  # noqa: BLE001
        # On any failure, assume open so we don't silently suppress overdue.
        return False


def cron_health(
    now_utc: datetime,
    *,
    runs_dir: Path = RUNS_DIR_DEFAULT,
    paper_auto_ledgers: Path = PAPER_AUTO_LEDGERS_DEFAULT,
) -> list[CronTask]:
    """Per scheduled task: last-run timestamp + overdue flag (best-effort)."""
    tasks: list[CronTask] = []
    now_et = now_utc.astimezone(ET)
    today_et = now_et.date()
    trading_day = not _market_closed_on(today_et)

    # --- Entry (the critical one) ---
    try:
        latest = find_latest_entry_session(runs_dir)
        last_iso = None
        ran_today = False
        if latest is not None:
            status = _load_yaml(latest / STATUS_FILE)
            if isinstance(status, dict):
                last_iso = status.get("run_started_at")
            dt = _parse_iso(last_iso)
            if dt is not None and dt.astimezone(ET).date() == today_et:
                ran_today = True
        overdue = bool(
            trading_day
            and not ran_today
            and now_et.timetz().replace(tzinfo=None) >= ENTRY_OVERDUE_AFTER
        )
        detail = "ran today" if ran_today else (
            "OVERDUE — no entry run recorded today" if overdue
            else ("not a trading day" if not trading_day else "before scheduled time")
        )
        tasks.append(CronTask("AutoPaperEntry", last_iso, overdue, detail))
    except Exception as exc:  # noqa: BLE001
        tasks.append(CronTask("AutoPaperEntry", None, False, f"check failed: {exc!r}"))

    # --- Monitor (best-effort: newest paper-auto ledger update, if positions open) ---
    try:
        last_iso, n_starter = _latest_ledger_update(paper_auto_ledgers)
        in_rth = RTH_OPEN <= now_et.timetz().replace(tzinfo=None) <= RTH_CLOSE
        stale = False
        if last_iso and n_starter > 0 and trading_day and in_rth:
            dt = _parse_iso(last_iso)
            if dt is not None:
                mins = (now_utc - dt).total_seconds() / 60.0
                stale = mins > MONITOR_STALE_MINUTES
        detail = (
            f"{n_starter} starter position(s); last ledger update {last_iso or 'never'}"
            + (" — STALE" if stale else "")
        )
        tasks.append(CronTask("AutoPaperMonitor", last_iso, stale, detail))
    except Exception as exc:  # noqa: BLE001
        tasks.append(CronTask("AutoPaperMonitor", None, False, f"check failed: {exc!r}"))

    return tasks


def _latest_ledger_update(paper_auto_ledgers: Path) -> tuple[Optional[str], int]:
    """Return (newest meta.updated_at across ledgers, count of starter states)."""
    newest: Optional[datetime] = None
    newest_iso: Optional[str] = None
    n_starter = 0
    try:
        files = list(paper_auto_ledgers.glob("*.yml"))
    except Exception:  # noqa: BLE001
        return None, 0
    for f in files:
        doc = _load_yaml(f)
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") or {}
        if isinstance(meta, dict):
            if str(meta.get("state", "")).strip() == "starter":
                n_starter += 1
            upd = meta.get("updated_at") or meta.get("asof")
            dt = _parse_iso(upd)
            if dt is not None and (newest is None or dt > newest):
                newest = dt
                newest_iso = upd if isinstance(upd, str) else _iso(dt)
    return newest_iso, n_starter


# ---------------------------------------------------------------------------
# feed health (lightweight probes; never raise)
# ---------------------------------------------------------------------------


def _probe_edgar() -> FeedStatus:
    import urllib.request
    import os
    name = "EDGAR"
    ident = os.environ.get("EDGAR_IDENTITY") or "Bertrand Shin shinbertrand@gmail.com"
    url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=1&output=atom"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ident})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            ok = resp.status == 200
        return FeedStatus(name, ok, _iso(_utc_now()) if ok else None,
                          "reachable" if ok else f"HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001
        return FeedStatus(name, False, None, f"unreachable: {exc!r}"[:200])


def _probe_price() -> FeedStatus:
    name = "PriceFeed"
    try:
        import yfinance as yf
        fi = yf.Ticker("SPY").fast_info
        px = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        ok = px is not None and float(px) > 0
        return FeedStatus(name, ok, _iso(_utc_now()) if ok else None,
                          f"SPY={float(px):.2f}" if ok else "no price returned")
    except Exception as exc:  # noqa: BLE001
        return FeedStatus(name, False, None, f"unreachable: {exc!r}"[:200])


def _probe_broker() -> FeedStatus:
    name = "BrokerAuth"
    try:
        from ..broker.tiger import TigerClient
        client = TigerClient(allow_live=False)
        entry = client.account_summary()
        out = entry.output if hasattr(entry, "output") else {}
        cash = out.get("cash")
        acct = out.get("account_masked", "****")
        ok = cash is not None
        # Detail is PII-safe: account is already last-4 masked by the client.
        return FeedStatus(name, ok, _iso(_utc_now()) if ok else None,
                          f"paper acct {acct} reachable" if ok else "no account summary")
    except Exception as exc:  # noqa: BLE001
        return FeedStatus(name, False, None, f"unreachable: {exc!r}"[:200])


def feed_health() -> list[FeedStatus]:
    """Probe EDGAR / price feed / broker auth. Each probe never raises."""
    return [_probe_edgar(), _probe_price(), _probe_broker()]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def error_count(run_dir: Optional[Path]) -> int:
    if run_dir is None:
        return 0
    status = _load_yaml(run_dir / STATUS_FILE)
    if not isinstance(status, dict):
        return 0
    errs = status.get("errors") or []
    return len(errs) if isinstance(errs, list) else 0


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    now_utc: Optional[datetime] = None,
    run_dir: Optional[Path] = None,
    runs_dir: Path = RUNS_DIR_DEFAULT,
    positions_json: Path = POSITIONS_JSON_DEFAULT,
    paper_auto_ledgers: Path = PAPER_AUTO_LEDGERS_DEFAULT,
    check_feeds: bool = True,
    write: bool = True,
    snapshot_out: Path = SNAPSHOT_OUT_DEFAULT,
    holdings: Any = None,
) -> HealthSnapshot:
    """Build the full health snapshot. Never raises; degrades per-field.

    ``holdings`` gates the naked-starter scan (FIX #5). When None and
    ``check_feeds`` is on (the live cron path), a best-effort live holdings
    snapshot is fetched from the paper broker; pass an explicit dict/set (or
    leave None with ``check_feeds=False``) in tests for determinism."""
    now = now_utc or _utc_now()

    target = run_dir or find_latest_entry_session(runs_dir)

    if holdings is None and check_feeds:
        holdings = _fetch_broker_holdings()

    sf: Optional[SilentFailure] = None
    if target is not None:
        try:
            sf = compute_silent_failure(target, positions_json=positions_json,
                                        paper_auto_ledgers=paper_auto_ledgers,
                                        holdings=holdings)
        except Exception as exc:  # noqa: BLE001
            sf = SilentFailure(
                run_id=getattr(target, "name", None), run_dir=str(target),
                session_started_iso=None, is_scheduled_entry=False,
                intended=0, placed=0, dry_run=0, errors=0, rejected=0, defer=0,
                n_total=0, alarm=False, reason=f"compute failed: {exc!r}",
            )

    try:
        crons = cron_health(now, runs_dir=runs_dir, paper_auto_ledgers=paper_auto_ledgers)
    except Exception as exc:  # noqa: BLE001
        crons = [CronTask("cron_health", None, False, f"check failed: {exc!r}")]

    feeds = feed_health() if check_feeds else []
    errs = error_count(target)

    overdue_any = any(c.overdue for c in crons)
    feeds_down = any(not f.up for f in feeds) if check_feeds else False
    alarm = bool(sf and sf.alarm)
    overall_ok = not alarm and not overdue_any and not feeds_down and errs == 0

    snap = HealthSnapshot(
        generated_at=_iso(now),
        silent_failure=sf,
        cron_tasks=crons,
        feeds=feeds,
        error_count=errs,
        overall_ok=overall_ok,
    )

    if write:
        try:
            snapshot_out.parent.mkdir(parents=True, exist_ok=True)
            snapshot_out.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 — failing to persist must not crash the run
            pass

    return snap


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.observability.health_snapshot",
        description="Build the auto-paper Health & Anomalies snapshot (read-only).",
    )
    p.add_argument("--run-dir", default=None, help="specific run dir to assess")
    p.add_argument("--no-feeds", action="store_true", help="skip network feed probes")
    p.add_argument("--no-write", action="store_true", help="don't persist the snapshot json")
    args = p.parse_args()

    rd = Path(args.run_dir) if args.run_dir else None
    snap = build_snapshot(run_dir=rd, check_feeds=not args.no_feeds, write=not args.no_write)
    print(json.dumps(snap.to_dict(), indent=2))


if __name__ == "__main__":
    main()
