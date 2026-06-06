"""Self-contained HTML dashboard for the paper-auto track (dark fintech skin).

Renders the same data as ``/auto-paper-perf`` (open P&L, setup-vs-backtest,
swing-critic calibration flip-gate) into a single static HTML file with
inline CSS + inline SVG charts -- NO JavaScript, NO CDN, NO server. Opens
offline by double-clicking the file. Regenerated each ``/auto-paper-monitor``
run so the browser tab (meta-refresh) stays current.

Visual language modeled on a dark fintech dashboard: near-black canvas, lime
accent, rounded stat cards with deltas, a hero bar chart with rounded tops +
markers, and a radial tick-gauge for the calibration progress.

This is observation infrastructure (read-only) -- it never places, sizes, or
recommends a trade. Matches the deferred Phase 5.d "HTML reports" line.

CLI:   uv run python -m tools.auto_paper.dashboard_html [--out PATH]
Lib:   from tools.auto_paper.dashboard_html import write_dashboard
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import math
import sys
from pathlib import Path
from typing import Any, Optional

from .calibration_analysis import compute as compute_calibration
from .performance import compute_open_pnl, compute_performance

# Default artifact location -- gitignored, co-located with the track data.
DEFAULT_OUT = Path("journal/paper-auto/dashboard.html")

# Browser auto-reload cadence (seconds). The file itself is rewritten by the
# monitor cron every ~30 min; this keeps an open tab fresh between writes.
META_REFRESH_SECONDS = 300

# --- palette (dark fintech) ---
_BG = "#0b0c0e"
_CARD = "#16181c"
_CARD2 = "#1e2127"
_BORDER = "#26292f"
_LIME = "#d4e84b"
_LIME_DK = "#aab93a"
_GREEN = "#5ed98b"
_RED = "#f4707a"
_TEXT = "#f4f6f7"
_MUTE = "#878d96"
_STATUS_COLOR = {"ok": _GREEN, "warn": _LIME, "fail": _RED, "no_data": _MUTE}


def _g(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _money(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}"


def _pnl_color(v: float) -> str:
    return _GREEN if v > 0 else (_RED if v < 0 else _MUTE)


def _card(label: str, value: str, *, delta: str = "", delta_pos: bool = True,
          accent: bool = False) -> str:
    delta_html = ""
    if delta:
        col = _GREEN if delta_pos else _RED
        delta_html = f"<span class='delta' style='color:{col}'>{html.escape(delta)}</span>"
    cls = "card accent" if accent else "card"
    return (
        f"<div class='{cls}'>"
        f"<div class='card-label'>{html.escape(label)}</div>"
        f"<div class='card-value'>{html.escape(value)} {delta_html}</div>"
        f"</div>"
    )


def _hero_bars_svg(positions: list[Any]) -> str:
    """Diverging vertical bar chart of per-position unrealized P&L, rounded
    tops + end markers, dashed gridlines -- the 'Cash Flow' hero look."""
    rows = [(str(_g(p, "ticker", "?")), float(_g(p, "unrealized_pnl_usd", 0) or 0))
            for p in positions]
    W, H = 680, 240
    padL, padR, padB, padT = 40, 16, 28, 16
    plot_h = H - padB - padT
    if not rows:
        return (f"<svg width='{W}' height='{H}'><text x='{W/2}' y='{H/2}' "
                f"text-anchor='middle' fill='{_MUTE}' font-size='13'>"
                f"no open positions</text></svg>")
    vmax = max((abs(v) for _, v in rows), default=1.0) or 1.0
    n = len(rows)
    slot = (W - padL - padR) / n
    bw = min(64, slot * 0.5)
    zero_y = padT + plot_h / 2
    biggest = max(range(n), key=lambda i: abs(rows[i][1]))
    parts = [f"<svg width='{W}' height='{H}' role='img' aria-label='unrealized pnl'>"]
    parts.append("<defs><linearGradient id='limegrad' x1='0' y1='0' x2='0' y2='1'>"
                 f"<stop offset='0' stop-color='{_LIME}'/>"
                 f"<stop offset='1' stop-color='{_LIME}' stop-opacity='0.15'/>"
                 "</linearGradient></defs>")
    # gridlines + y labels (+vmax, 0, -vmax)
    for frac, lab in ((1.0, vmax), (0.5, vmax / 2), (0.0, 0.0),
                      (-0.5, -vmax / 2), (-1.0, -vmax)):
        gy = zero_y - frac * (plot_h / 2)
        parts.append(f"<line x1='{padL}' y1='{gy:.1f}' x2='{W - padR}' y2='{gy:.1f}' "
                     f"stroke='#23262c' stroke-dasharray='3 4'/>")
        parts.append(f"<text x='{padL - 6}' y='{gy + 3:.1f}' text-anchor='end' "
                     f"fill='{_MUTE}' font-size='9'>{_money(lab)}</text>")
    for i, (tk, v) in enumerate(rows):
        cx = padL + slot * i + slot / 2
        bh = (abs(v) / vmax) * (plot_h / 2)
        pos = v >= 0
        y = zero_y - bh if pos else zero_y
        fill = "url(#limegrad)" if (pos and i == biggest) else (_LIME if pos else _RED)
        parts.append(f"<rect x='{cx - bw/2:.1f}' y='{y:.1f}' width='{bw:.1f}' "
                     f"height='{max(bh,1):.1f}' rx='6' fill='{fill}'/>")
        my = (zero_y - bh) if pos else (zero_y + bh)
        parts.append(f"<circle cx='{cx:.1f}' cy='{my:.1f}' r='4' fill='{_LIME if pos else _RED}' "
                     f"stroke='{_BG}' stroke-width='2'/>")
        parts.append(f"<text x='{cx:.1f}' y='{H - 9}' text-anchor='middle' "
                     f"fill='{_TEXT}' font-size='11' font-weight='600'>{html.escape(tk)}</text>")
        lbly = (my - 9) if pos else (my + 15)
        parts.append(f"<text x='{cx:.1f}' y='{lbly:.1f}' text-anchor='middle' "
                     f"fill='{_pnl_color(v)}' font-size='10'>{html.escape(_money(v))}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _gauge_svg(value: int, target: int = 20) -> str:
    """Radial tick gauge (270-deg span); ticks lit lime up to value/target."""
    W = H = 190
    cx = cy = W / 2
    r_out, r_in = 82, 64
    n_ticks = 44
    span, start = 270.0, 135.0  # degrees, clockwise from lower-left
    frac = min(1.0, value / target) if target else 0.0
    lit = round(frac * n_ticks)
    parts = [f"<svg width='{W}' height='{H}' role='img' aria-label='calibration gauge'>"]
    for i in range(n_ticks):
        ang = math.radians(start + span * (i / (n_ticks - 1)))
        x1 = cx + r_in * math.cos(ang); y1 = cy + r_in * math.sin(ang)
        x2 = cx + r_out * math.cos(ang); y2 = cy + r_out * math.sin(ang)
        col = _LIME if i < lit else "#2b2f36"
        parts.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' "
                     f"stroke='{col}' stroke-width='3' stroke-linecap='round'/>")
    parts.append(f"<text x='{cx}' y='{cy - 2}' text-anchor='middle' fill='{_TEXT}' "
                 f"font-size='30' font-weight='700'>{value}</text>")
    parts.append(f"<text x='{cx}' y='{cy + 20}' text-anchor='middle' fill='{_MUTE}' "
                 f"font-size='12'>of {target} joined</text>")
    parts.append("</svg>")
    return "".join(parts)


def render_html(report: Any, open_pnl: Any, calib: Any) -> str:
    asof = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    total_pnl = float(_g(open_pnl, "total_unrealized_pnl_usd", 0) or 0)
    mkt_val = float(_g(open_pnl, "total_market_value_usd", 0) or 0)
    positions = _g(open_pnl, "by_position", []) or []
    err = _g(open_pnl, "error")
    missing = _g(open_pnl, "missing_quotes", []) or []
    ready = bool(_g(calib, "ready_to_flip", False))
    joined = int(_g(calib, "n_joined", 0) or 0)
    pnl_pct = (total_pnl / mkt_val * 100.0) if mkt_val else 0.0

    cards = [
        _card("Open P&L", _money(total_pnl),
              delta=f"{pnl_pct:+.2f}%", delta_pos=total_pnl >= 0, accent=True),
        _card("Market value", _money(mkt_val)),
        _card("Open positions", str(_g(report, "n_open", 0))),
        _card("Submitted", str(_g(report, "n_submitted", 0))),
        _card("Panel sizing", "LIVE" if ready else "SHADOW",
              delta=f"{joined}/20", delta_pos=ready),
    ]

    # positions table (Recent-Transactions style)
    pos_rows = []
    for p in positions:
        v = float(_g(p, "unrealized_pnl_usd", 0) or 0)
        tk = html.escape(str(_g(p, "ticker", "?")))
        pos_rows.append(
            "<tr>"
            f"<td><span class='dot'></span><b>{tk}</b></td>"
            f"<td class='num'>{_g(p, 'entry_price', '')}</td>"
            f"<td class='num'>{_g(p, 'current_price', '')}</td>"
            f"<td class='num' style='color:{_pnl_color(v)}'>{html.escape(_money(v))}</td>"
            "</tr>"
        )
    pos_table = (
        "<table><thead><tr><th>Ticker</th><th class='num'>Entry</th>"
        "<th class='num'>Current</th><th class='num'>Unreal. $</th></tr></thead><tbody>"
        + ("".join(pos_rows) or "<tr><td colspan='4' class='muted'>none</td></tr>")
        + "</tbody></table>"
    )

    cmp_rows = []
    for c in _g(report, "comparisons", []) or []:
        status = str(_g(c, "status", "no_data"))
        rs = _g(c, "realized_sharpe")
        rs_txt = f"{rs:.2f}" if isinstance(rs, (int, float)) else "NA"
        color = _STATUS_COLOR.get(status, _MUTE)
        cmp_rows.append(
            "<tr>"
            f"<td>{html.escape(str(_g(c, 'setup', '')))}</td>"
            f"<td class='num'>{_g(c, 'n_trades', 0)}</td>"
            f"<td class='num'>{rs_txt}</td>"
            f"<td class='num'>{_g(c, 'backtest_sharpe', '')}</td>"
            f"<td><span class='badge' style='color:{color};border-color:{color}'>"
            f"{html.escape(status)}</span></td>"
            "</tr>"
        )
    cmp_table = (
        "<table><thead><tr><th>Setup</th><th class='num'>n</th>"
        "<th class='num'>Real. Sharpe</th><th class='num'>BT Sharpe</th>"
        "<th>Status</th></tr></thead><tbody>" + "".join(cmp_rows) + "</tbody></table>"
    )

    notes = _g(report, "notes", []) or []
    notes_html = "".join(f"<li>{html.escape(str(n))}</li>" for n in notes[:10]) \
        or "<li class='muted'>none</li>"
    discrimination = html.escape(str(_g(calib, "discrimination", "")))

    banner = ""
    if err:
        banner += (f"<div class='banner'>Tiger unreachable: {html.escape(str(err))} "
                   f"-- P&amp;L may be stale.</div>")
    if missing:
        banner += (f"<div class='banner'>Missing broker quotes: "
                   f"{html.escape(', '.join(map(str, missing)))}</div>")

    flip_badge = (f"<span class='flip {'yes' if ready else 'no'}'>"
                  f"{'READY TO FLIP' if ready else 'SHADOW'}</span>")

    return f"""<!DOCTYPE html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta http-equiv='refresh' content='{META_REFRESH_SECONDS}'>
<title>Paper-Auto Dashboard</title>
<style>
 *{{box-sizing:border-box}}
 body{{margin:0;background:{_BG};color:{_TEXT};
   font-family:-apple-system,Segoe UI,Roboto,sans-serif;}}
 .wrap{{max-width:1040px;margin:0 auto;padding:26px 20px 70px;}}
 .top{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;}}
 h1{{font-size:19px;margin:0;}}
 .asof{{color:{_MUTE};font-size:11px;}}
 .cards{{display:flex;flex-wrap:wrap;gap:14px;margin:20px 0 22px;}}
 .card{{background:{_CARD};border:1px solid {_BORDER};border-radius:16px;
   padding:15px 18px;min-width:150px;flex:1;}}
 .card.accent{{background:linear-gradient(135deg,{_LIME} 0%,{_LIME_DK} 100%);
   border:none;color:#15170c;}}
 .card.accent .card-label,.card.accent .delta{{color:#3a3d18 !important;}}
 .card-label{{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:{_MUTE};}}
 .card-value{{font-size:25px;font-weight:700;margin-top:4px;
   font-variant-numeric:tabular-nums;}}
 .delta{{font-size:12px;font-weight:600;margin-left:4px;}}
 .grid{{display:grid;grid-template-columns:1.6fr 1fr;gap:16px;}}
 @media(max-width:760px){{.grid{{grid-template-columns:1fr;}}}}
 section{{background:{_CARD};border:1px solid {_BORDER};border-radius:16px;
   padding:18px 20px;margin-bottom:16px;}}
 h2{{font-size:13px;margin:0 0 14px;color:{_TEXT};font-weight:600;
   display:flex;align-items:center;gap:10px;}}
 table{{width:100%;border-collapse:collapse;font-size:13px;}}
 th,td{{text-align:left;padding:8px 8px;border-bottom:1px solid {_BORDER};}}
 th{{color:{_MUTE};font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.04em;}}
 tr:last-child td{{border-bottom:none;}}
 td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums;}}
 .dot{{display:inline-block;width:7px;height:7px;border-radius:50%;
   background:{_LIME};margin-right:8px;vertical-align:middle;}}
 .badge{{border:1px solid;padding:2px 9px;border-radius:11px;font-size:10px;
   text-transform:uppercase;letter-spacing:.03em;}}
 .flip{{padding:3px 11px;border-radius:11px;font-weight:700;font-size:11px;}}
 .flip.no{{background:#26292f;color:{_MUTE};}}
 .flip.yes{{background:{_LIME};color:#15170c;}}
 .muted{{color:{_MUTE};}}
 .gaugewrap{{display:flex;flex-direction:column;align-items:center;}}
 .banner{{background:#2a2410;border:1px solid #4a3f12;color:{_LIME};
   padding:9px 13px;border-radius:10px;margin-bottom:14px;font-size:13px;}}
 ul{{margin:0;padding-left:18px;font-size:12.5px;color:#c7ccd3;}}
 li{{margin:4px 0;}}
</style></head>
<body><div class='wrap'>
 <div class='top'>
   <h1>Paper-Auto Dashboard</h1>
   <div class='asof'>asof {html.escape(asof)} &middot; read-only &middot; refresh {META_REFRESH_SECONDS}s</div>
 </div>
 {banner}
 <div class='cards'>{''.join(cards)}</div>

 <div class='grid'>
   <section>
     <h2>Open positions &mdash; unrealized P&amp;L</h2>
     {_hero_bars_svg(positions)}
     {pos_table}
   </section>
   <section class='gaugewrap'>
     <h2>Calibration {flip_badge}</h2>
     {_gauge_svg(joined)}
     <p class='muted' style='font-size:11.5px;text-align:center;margin-top:12px'>{discrimination}</p>
   </section>
 </div>

 <section>
   <h2>Realized vs backtest expectation</h2>
   {cmp_table}
 </section>

 <section>
   <h2>Notes</h2>
   <ul>{notes_html}</ul>
 </section>
</div></body></html>
"""


def write_dashboard(
    out: Optional[Path | str] = None,
    *,
    report: Any = None,
    open_pnl: Any = None,
    calib: Any = None,
) -> Path:
    """Compute (unless injected) + render + write the HTML dashboard.

    ``report`` / ``open_pnl`` / ``calib`` are test seams; when None they are
    computed live. Returns the path written.
    """
    out_path = Path(out) if out is not None else DEFAULT_OUT
    if report is None:
        report = compute_performance()
    if open_pnl is None:
        open_pnl = compute_open_pnl()
    if calib is None:
        calib = compute_calibration()
    doc = render_html(report, open_pnl, calib)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.auto_paper.dashboard_html")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"output HTML path (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    try:
        path = write_dashboard(args.out)
    except Exception as exc:  # loud-fail for cron logs, never crash the monitor
        print(f"DASHBOARD_HTML_FAIL {exc!r}", flush=True)
        return 1
    print(f"DASHBOARD_HTML_OK {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
