"""Static HTML render of the Health & Anomalies snapshot (dark fintech skin).

Self-contained single file: inline CSS, no JavaScript, no CDN, no server. Opens
offline by double-clicking; a meta-refresh keeps an open tab current between
health-check runs. Renders a :class:`~tools.observability.health_snapshot.HealthSnapshot`
(or its ``to_dict()``) — it computes nothing and reaches no network.

The data layer is the single source of truth: a future interactive panel can
serve the same ``health_snapshot.json`` and add action endpoints; this renderer
is just one consumer.

CLI::  uv run python -m tools.observability.health_html [--out PATH]
"""
from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = _REPO_ROOT / "journal" / "observability" / "health.html"
META_REFRESH_SECONDS = 300

# palette (matches tools/auto_paper/dashboard_html.py)
_BG = "#0b0c0e"
_CARD = "#16181c"
_BORDER = "#26292f"
_GREEN = "#5ed98b"
_LIME = "#d4e84b"
_RED = "#f4707a"
_TEXT = "#f4f6f7"
_MUTE = "#878d96"


def _g(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _badge(text: str, color: str) -> str:
    return (
        f"<span style='display:inline-block;padding:2px 10px;border-radius:999px;"
        f"background:{color}22;color:{color};border:1px solid {color}55;"
        f"font-size:12px;font-weight:600'>{_esc(text)}</span>"
    )


def _row(cells: list[str]) -> str:
    tds = "".join(
        f"<td style='padding:8px 12px;border-bottom:1px solid {_BORDER};vertical-align:top'>{c}</td>"
        for c in cells
    )
    return f"<tr>{tds}</tr>"


def _section(title: str, inner: str) -> str:
    return (
        f"<div style='background:{_CARD};border:1px solid {_BORDER};border-radius:14px;"
        f"padding:18px 20px;margin:14px 0'>"
        f"<div style='font-size:13px;letter-spacing:.08em;text-transform:uppercase;"
        f"color:{_MUTE};margin-bottom:10px'>{_esc(title)}</div>{inner}</div>"
    )


def _silent_failure_hero(sf: Optional[Any]) -> str:
    if sf is None:
        return _section("Silent-Failure Detector", f"<div style='color:{_MUTE}'>No entry session found.</div>")
    alarm = bool(_g(sf, "alarm"))
    color = _RED if alarm else _GREEN
    headline = "🚨 SILENT FAILURE" if alarm else "✓ NOMINAL"
    intended, placed, dry = _g(sf, "intended", 0), _g(sf, "placed", 0), _g(sf, "dry_run", 0)
    defer, rejected = _g(sf, "defer", 0), _g(sf, "rejected", 0)
    stat = (
        f"<div style='display:flex;gap:26px;flex-wrap:wrap;margin:10px 0'>"
        f"<div><div style='font-size:30px;font-weight:700;color:{_TEXT}'>{intended}</div>"
        f"<div style='color:{_MUTE};font-size:12px'>intended</div></div>"
        f"<div><div style='font-size:30px;font-weight:700;color:{_GREEN if placed else _MUTE}'>{placed}</div>"
        f"<div style='color:{_MUTE};font-size:12px'>placed</div></div>"
        f"<div><div style='font-size:30px;font-weight:700;color:{_RED if dry else _MUTE}'>{dry}</div>"
        f"<div style='color:{_MUTE};font-size:12px'>dry-run</div></div>"
        f"<div><div style='font-size:30px;font-weight:700;color:{_MUTE}'>{rejected}</div>"
        f"<div style='color:{_MUTE};font-size:12px'>rejected</div></div>"
        f"<div><div style='font-size:30px;font-weight:700;color:{_MUTE}'>{defer}</div>"
        f"<div style='color:{_MUTE};font-size:12px'>defer</div></div>"
        f"</div>"
    )
    desync = _g(sf, "placed_not_at_broker", []) or []
    desync_html = ""
    if desync:
        names = ", ".join(_esc(_g(d, "ticker", "?")) for d in desync)
        desync_html = f"<div style='color:{_RED};margin-top:8px'>⚠ placed-but-not-at-broker: {names}</div>"
    inner = (
        f"<div style='font-size:20px;font-weight:700;color:{color};margin-bottom:4px'>{headline}</div>"
        f"<div style='color:{_MUTE};font-size:13px'>session <code>{_esc(_g(sf, 'run_id'))}</code> "
        f"· {_esc(_g(sf, 'reason'))}</div>{stat}{desync_html}"
    )
    return _section("Silent-Failure Detector", inner)


def _cron_section(tasks: list[Any]) -> str:
    rows = []
    for t in tasks:
        overdue = bool(_g(t, "overdue"))
        badge = _badge("OVERDUE", _RED) if overdue else _badge("ok", _GREEN)
        rows.append(_row([
            f"<b>{_esc(_g(t, 'name'))}</b>", badge,
            f"<span style='color:{_MUTE}'>{_esc(_g(t, 'last_run_iso'))}</span>",
            f"<span style='color:{_MUTE}'>{_esc(_g(t, 'detail'))}</span>",
        ]))
    table = f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{''.join(rows)}</table>"
    return _section("System Health — Scheduled Tasks", table)


def _feeds_section(feeds: list[Any]) -> str:
    if not feeds:
        return _section("Data Feeds", f"<div style='color:{_MUTE}'>Feed probes skipped this run.</div>")
    rows = []
    for f in feeds:
        up = bool(_g(f, "up"))
        badge = _badge("UP", _GREEN) if up else _badge("DOWN", _RED)
        rows.append(_row([
            f"<b>{_esc(_g(f, 'name'))}</b>", badge,
            f"<span style='color:{_MUTE}'>{_esc(_g(f, 'last_success_iso'))}</span>",
            f"<span style='color:{_MUTE}'>{_esc(_g(f, 'detail'))}</span>",
        ]))
    table = f"<table style='width:100%;border-collapse:collapse;font-size:14px'>{''.join(rows)}</table>"
    return _section("Data Feeds", table)


def render(snapshot: Any) -> str:
    """Render a HealthSnapshot (object or dict) to a self-contained HTML string."""
    snap = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    sf = _g(snap, "silent_failure")
    ok = bool(_g(snap, "overall_ok"))
    top_color = _GREEN if ok else _RED
    top_text = "ALL CLEAR" if ok else "ATTENTION NEEDED"
    err = _g(snap, "error_count", 0)
    err_html = ""
    if err:
        err_html = _section("Errors", f"<div style='color:{_RED}'>{err} error(s) in latest run — see _status.yml.</div>")

    body = (
        f"<div style='max-width:880px;margin:0 auto;padding:24px 16px'>"
        f"<div style='display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap'>"
        f"<div style='font-size:22px;font-weight:700'>Auto-Paper Health & Anomalies</div>"
        f"<div>{_badge(top_text, top_color)}</div></div>"
        f"<div style='color:{_MUTE};font-size:12px;margin-top:4px'>generated {_esc(_g(snap, 'generated_at'))}</div>"
        f"{_silent_failure_hero(sf)}"
        f"{_cron_section(_g(snap, 'cron_tasks', []) or [])}"
        f"{_feeds_section(_g(snap, 'feeds', []) or [])}"
        f"{err_html}"
        f"<div style='color:{_MUTE};font-size:11px;margin-top:18px'>Observe-only. This panel reads run "
        f"artifacts; it never places, sizes, or recommends a trade.</div>"
        f"</div>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='{META_REFRESH_SECONDS}'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Auto-Paper Health</title>"
        f"<style>body{{margin:0;background:{_BG};color:{_TEXT};"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}"
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}"
        "table td:first-child{white-space:nowrap}</style></head>"
        f"<body>{body}</body></html>"
    )


def write_html(snapshot: Any, out: Path = DEFAULT_OUT) -> Path:
    """Render + write the panel. Returns the path. Never raises on write error."""
    htmltext = render(snapshot)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(htmltext, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> None:
    from .health_snapshot import build_snapshot
    p = argparse.ArgumentParser(prog="tools.observability.health_html")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--no-feeds", action="store_true")
    args = p.parse_args()
    snap = build_snapshot(check_feeds=not args.no_feeds, write=False)
    path = write_html(snap, Path(args.out))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
