"""Process B monitor loop — the kill-switch ``while True`` runner.

**Session 1 scope: dry-run ONLY.** This module composes the single-cycle
pipeline (read Tiger positions -> identify thematic subset -> update peak ->
read kill-event flag -> run ladder compute -> append event log -> write
heartbeat) but does NOT place orders. Session 2 wires the order-placement
arm.

The pipeline:

1. :func:`cycle` performs one monitoring cycle:
   a. Read Tiger account_summary (for total_account_value).
   b. Read Tiger positions().
   c. Intersect with thematic index (positions.py).
   d. Update rolling peak (state.update_peak).
   e. Load Aschenbrenner-kill-event flag (state.load_kill_event).
   f. Run ladder.compute() -> KillSwitchDecision.
   g. Append CycleEvent to events.jsonl.
   h. Update heartbeat.json.
   i. If decision.action != "hold" AND not dry_run -> place orders
      (Session 2 — currently raises NotImplementedError).

2. :func:`run_forever` calls :func:`cycle` in a loop, sleeping per the
   clock module's RTH-aware cadence. Catches and logs cycle errors but
   does NOT exit the loop (the kill-switch must keep running).

CLI::

    uv run python -m tools.thematic_portfolio.kill_switch.monitor [--dry-run]
        [--once] [--state-dir <path>] [--index-path <path>]

``--dry-run`` is the **default** in this session. ``--no-dry-run`` is
explicitly rejected with a NotImplementedError reminder pointing at
Session 2.

``--once`` runs a single cycle and exits (used by tests + ad-hoc
inspection).
"""
from __future__ import annotations

import argparse
import time as _time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from . import ladder, positions, state
from .clock import session_state


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_cycle_id() -> str:
    return f"cycle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


@dataclass
class CycleResult:
    """In-memory summary of one cycle, returned for CLI + test inspection."""

    cycle_id: str
    cycle_number: int
    ok: bool
    error: Optional[str]
    decision: Optional[ladder.KillSwitchDecision]
    thematic_market_value: float
    peak_thematic_value: float
    total_account_value: float
    aschenbrenner_kill_event: bool
    thematic_symbols: list[str]
    dry_run: bool
    placed_orders: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "cycle_number": self.cycle_number,
            "ok": self.ok,
            "error": self.error,
            "decision": self.decision.to_dict() if self.decision else None,
            "thematic_market_value": self.thematic_market_value,
            "peak_thematic_value": self.peak_thematic_value,
            "total_account_value": self.total_account_value,
            "aschenbrenner_kill_event": self.aschenbrenner_kill_event,
            "thematic_symbols": self.thematic_symbols,
            "dry_run": self.dry_run,
            "placed_orders": self.placed_orders,
        }


def cycle(
    *,
    tiger: TigerClient,
    state_dir: Optional[Path] = None,
    index_path: Optional[Path] = None,
    cycle_number: int = 0,
    dry_run: bool = True,
) -> CycleResult:
    """Run one monitoring cycle.

    Args:
        tiger: an already-constructed (paper-routed) TigerClient.
        state_dir: override the state directory (used in tests).
        index_path: override the thematic-index path (used in tests).
        cycle_number: monotonically-increasing cycle number for the
            heartbeat. Caller manages.
        dry_run: when True (DEFAULT in Session 1), the cycle runs the
            full pipeline EXCEPT order placement. When False, currently
            raises NotImplementedError pointing at Session 2.
    """
    cycle_id = _new_cycle_id()

    # 1-2. Tiger reads.
    try:
        summary = tiger.account_summary().output
        tiger_positions_te = tiger.positions().output
    except (BrokerConfigError, BrokerOrderError) as exc:
        return CycleResult(
            cycle_id=cycle_id,
            cycle_number=cycle_number,
            ok=False,
            error=f"tiger_read_failed: {exc}",
            decision=None,
            thematic_market_value=0.0,
            peak_thematic_value=0.0,
            total_account_value=0.0,
            aschenbrenner_kill_event=False,
            thematic_symbols=[],
            dry_run=dry_run,
            placed_orders=[],
        )

    total_account_value = float(summary.get("net_liquidation", 0.0) or 0.0)
    tiger_positions_list = list(tiger_positions_te.get("positions", []))

    # 3. Intersect with thematic index.
    thematic = positions.identify_thematic_positions(
        tiger_positions_list, index_path=index_path
    )

    # 4. Update rolling peak.
    peak = state.update_peak(thematic.thematic_market_value, state_dir=state_dir)

    # 5. Load kill-event flag.
    kill_flag = state.load_kill_event(state_dir=state_dir)

    # 6. Run ladder.
    previous_tier = state.most_recent_fired_tier(state_dir=state_dir)
    decision = ladder.compute(
        ladder.KillSwitchInputs(
            thematic_market_value=thematic.thematic_market_value,
            peak_thematic_value=peak.peak_value,
            total_account_value=total_account_value,
            aschenbrenner_kill_event=kill_flag.fired,
            previous_fired_tier=previous_tier,
        )
    )

    placed_orders: list[dict[str, Any]] = []
    cycle_warnings = list(thematic.warnings) + list(decision.warnings)

    # 7. Order placement gate (Session 2 territory).
    if decision.action != "hold" and not dry_run:
        raise NotImplementedError(
            f"Cycle {cycle_id} decision.action={decision.action!r} but "
            "live order placement is Session 2. Pass --dry-run for Session 1."
        )
    if decision.action != "hold" and dry_run:
        cycle_warnings.append(
            f"DRY-RUN: would have placed sell orders for "
            f"{len(thematic.thematic_symbols)} thematic position(s) at "
            f"sell_fraction={decision.sell_fraction:.2%} (tier {decision.tier})."
        )

    # 8. Append event log.
    event = state.CycleEvent(
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        fired_at=_utc_now_iso(),
        dry_run=dry_run,
        action=decision.action,
        tier=decision.tier,
        drawdown_pct=decision.drawdown_pct,
        current_allocation_pct=decision.current_allocation_pct,
        target_allocation_pct=decision.target_allocation_pct,
        sell_fraction=decision.sell_fraction,
        thematic_market_value=thematic.thematic_market_value,
        peak_thematic_value=peak.peak_value,
        total_account_value=total_account_value,
        aschenbrenner_kill_event=kill_flag.fired,
        aschenbrenner_override=decision.aschenbrenner_override,
        rationale=decision.rationale,
        thematic_symbols=thematic.thematic_symbols,
        warnings=cycle_warnings,
        orders_placed=placed_orders,
    )
    state.append_event(event, state_dir=state_dir)

    # 9. Write heartbeat.
    state.save_heartbeat(
        state.HeartbeatState(
            last_cycle_at=event.fired_at,
            cycle_number=cycle_number,
            dry_run=dry_run,
            last_action=decision.action,
            last_tier=decision.tier,
        ),
        state_dir=state_dir,
    )

    return CycleResult(
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        ok=True,
        error=None,
        decision=decision,
        thematic_market_value=thematic.thematic_market_value,
        peak_thematic_value=peak.peak_value,
        total_account_value=total_account_value,
        aschenbrenner_kill_event=kill_flag.fired,
        thematic_symbols=thematic.thematic_symbols,
        dry_run=dry_run,
        placed_orders=placed_orders,
    )


def run_forever(
    *,
    tiger: TigerClient,
    state_dir: Optional[Path] = None,
    index_path: Optional[Path] = None,
    dry_run: bool = True,
    max_cycles: Optional[int] = None,
    sleep_fn: Any = _time.sleep,
    clock_fn: Any = session_state,
) -> int:
    """Run cycles forever. Returns the cycle count when ``max_cycles`` is hit.

    The loop catches all exceptions from :func:`cycle` and logs them, then
    sleeps the off-hours cadence (longer cadence is the safer default
    after an error). The kill-switch never exits its loop on error — only
    on explicit ``max_cycles``.

    Args:
        max_cycles: cap the loop for tests / ad-hoc invocation. None =
            run forever.
        sleep_fn: injected for tests.
        clock_fn: injected for tests.
    """
    cycle_number = 0
    while True:
        if max_cycles is not None and cycle_number >= max_cycles:
            return cycle_number
        cycle_number += 1
        try:
            result = cycle(
                tiger=tiger,
                state_dir=state_dir,
                index_path=index_path,
                cycle_number=cycle_number,
                dry_run=dry_run,
            )
            if result.ok:
                clock = clock_fn()
                sleep_seconds = clock.suggested_sleep_seconds
            else:
                sleep_seconds = 300  # off-hours cadence after error
        except Exception:  # noqa: BLE001 — kill-switch never exits on exception
            traceback.print_exc()
            sleep_seconds = 300
        sleep_fn(sleep_seconds)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.kill_switch.monitor",
        description=(
            "Process B kill-switch monitor (Session 1 — dry-run only). "
            "Reads Tiger paper positions, intersects with the thematic "
            "index, computes the 3-tier CPPI ladder, appends to the "
            "event log + heartbeat. Does NOT place orders in Session 1."
        ),
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Skip order placement (Session 1 default).",
    )
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help=(
            "Session 2 only: enable live order placement. Currently raises "
            "NotImplementedError on any non-hold decision."
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit (default: run forever).",
    )
    p.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="When running forever, exit after this many cycles (for tests).",
    )
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--index-path", type=Path, default=None)
    p.add_argument(
        "--props-dir",
        type=Path,
        default=None,
        help="Tiger credentials dir; defaults to $TIGER_PROPS_DIR.",
    )
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()
    tiger = TigerClient(props_dir=str(args.props_dir) if args.props_dir else None)
    if args.once:
        result = cycle(
            tiger=tiger,
            state_dir=args.state_dir,
            index_path=args.index_path,
            cycle_number=1,
            dry_run=args.dry_run,
        )
        import json
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return
    run_forever(
        tiger=tiger,
        state_dir=args.state_dir,
        index_path=args.index_path,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
    )


if __name__ == "__main__":
    main()
