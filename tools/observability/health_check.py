"""Health-check orchestrator — the cron entry point for the cockpit slice 1.

Composes: build snapshot → write snapshot JSON → render static HTML panel →
dispatch edge-triggered Telegram alerts. Designed to be fired by a scheduled
task ~post-entry (10:20 ET) then hourly through RTH.

OBSERVE-ONLY and crash-isolated: this **always exits 0** so a health-check
failure can never look like (or cause) a trading failure. It reads run
artifacts and writes only under ``journal/observability/`` +
``ledgers/observability/``.

CLI::

    uv run python -m tools.observability.health_check
    uv run python -m tools.observability.health_check --run-dir <DIR> --no-alert --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .health_alerts import dispatch_alerts
from .health_html import write_html
from .health_snapshot import build_snapshot


def run(*, run_dir: Path | None = None, alert: bool = True,
        check_feeds: bool = True, print_json: bool = False) -> int:
    """Run the full health check. Returns 0 always (best-effort)."""
    try:
        snap = build_snapshot(run_dir=run_dir, check_feeds=check_feeds, write=True)
    except Exception as exc:  # noqa: BLE001
        # Even snapshot construction is guarded inside build_snapshot, but be
        # doubly safe: a checker crash must never propagate.
        print(f"health_check: snapshot failed: {exc!r}", file=sys.stderr)
        return 0

    try:
        write_html(snap)
    except Exception as exc:  # noqa: BLE001
        print(f"health_check: html render failed: {exc!r}", file=sys.stderr)

    dispatch = None
    if alert:
        try:
            dispatch = dispatch_alerts(snap)
        except Exception as exc:  # noqa: BLE001
            print(f"health_check: alert dispatch failed: {exc!r}", file=sys.stderr)

    sf = snap.silent_failure
    summary = {
        "overall_ok": snap.overall_ok,
        "alarm": bool(sf and sf.alarm),
        "intended": sf.intended if sf else None,
        "placed": sf.placed if sf else None,
        "dry_run": sf.dry_run if sf else None,
        "error_count": snap.error_count,
        "feeds_down": [f.name for f in snap.feeds if not f.up],
        "alerts": dispatch.to_dict() if dispatch else None,
    }
    if print_json:
        print(json.dumps(snap.to_dict(), indent=2))
    else:
        print("HEALTH " + json.dumps(summary))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.observability.health_check")
    p.add_argument("--run-dir", default=None, help="assess a specific run dir (validation)")
    p.add_argument("--no-alert", action="store_true", help="compute + render but don't push Telegram")
    p.add_argument("--no-feeds", action="store_true", help="skip network feed probes")
    p.add_argument("--json", action="store_true", dest="as_json", help="print the full snapshot JSON")
    args = p.parse_args()
    rd = Path(args.run_dir) if args.run_dir else None
    code = run(run_dir=rd, alert=not args.no_alert,
               check_feeds=not args.no_feeds, print_json=args.as_json)
    sys.exit(code)


if __name__ == "__main__":
    main()
