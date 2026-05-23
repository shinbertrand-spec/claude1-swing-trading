"""Phase 4 — re-run cited tools and detect output divergence.

Per ``swing-risk-compliance-doctrine.md`` Requirement 3:

    risk-and-compliance independently re-runs each numbered step's tool.
    Not the model; the script. If any step diverges, BLOCK.

Tools split into three replayability classes:

* **PURE** — pure-arithmetic tools whose inputs are JSON-serialisable
  primitives. We can fully re-run them from the recorded ``inputs``
  dict and compare ``output`` element-for-element.

* **OHLCV** — tools that consume OHLCV DataFrames. The recorded
  ``inputs`` only carries ticker / period / fetched_at — re-running
  would mean re-fetching, and the underlying bars will have advanced.
  Phase 4 baseline runs only a **shape check**: verify the recorded
  output has the expected keys and types. Value verification is a
  Phase 5 backtest concern.

* **MANUAL** — tools tagged ``manual:*`` (broker_api, sec_filing, etc.)
  have no Python counterpart; we just verify the trace step is
  well-formed.

The split is encoded in :data:`PURE_REGISTRY` and :data:`OHLCV_SHAPES`.

CLI: not provided directly — see :mod:`tools.trace_audit` for the
composite entry point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .contract import TraceEntry

TOOL = "tools/trace_rerun.py"

# Tolerance for numerical comparison. Pure-arithmetic tools should match
# to floating-point precision; ATR / position-sizing math can have minor
# drift from float ordering, so allow tiny relative tolerance.
DEFAULT_REL_TOL = 1e-6
DEFAULT_ABS_TOL = 1e-9


def _build_pure_registry() -> dict[str, Callable[..., TraceEntry]]:
    """Lazy import; returns name → callable for re-running.

    Each entry maps ``trace_step.tool`` literal strings to the function
    that produces a TraceEntry of identical shape.
    """
    from . import (
        combined_breakeven,
        compute_yoy,
        ep_grade,
        magna_score,
        pe_expansion_check,
        position_sizer,
        position_state,
        sell_decision,
        sell_into_strength,
        stop_sizer,
        add_on_evaluator,
    )

    return {
        "tools/compute_yoy.py": compute_yoy.compute,
        "tools/stop_sizer.py": stop_sizer.compute,
        "tools/position_sizer.py": position_sizer.compute,
        "tools/magna_score.py": magna_score.compute,
        "tools/ep_grade.py": ep_grade.compute,
        "tools/combined_breakeven.py": combined_breakeven.compute,
        "tools/position_state.py": position_state.compute,
        "tools/add_on_evaluator.py": add_on_evaluator.compute,
        "tools/sell_into_strength.py": sell_into_strength.compute,
        "tools/sell_decision.py": sell_decision.compute,
        "tools/pe_expansion_check.py": pe_expansion_check.compute,
    }


# Shape signatures for OHLCV-consuming tools. Each value is a set of
# output keys that MUST be present (subset of the canonical output).
OHLCV_SHAPES: dict[str, set[str]] = {
    "tools/atr_compute.py": {"atr", "period", "last_close", "last_close_date"},
    "tools/trend_template.py": {"trend_template_passes", "criteria", "stage", "stats"},
    "tools/regime_check.py": {
        "broad_market", "candidate", "regime_multiplier",
        "candidate_qualifies_for_entry",
    },
    "tools/vcp_detect.py": {
        "detected", "contractions_count", "contractions", "pivot",
        "last_close", "above_pivot",
    },
    "tools/ep_detect.py": {
        "gap_pct", "gap_band", "ep_eligible", "intraday_expansion_pct",
        "volume_today_vs_adv",
    },
    "tools/day7_milestone_check.py": {
        "survives_day7", "trading_days_since_entry", "broke_entry_low",
        "closed_below_10ma",
    },
    "tools/sltb_scan.py": {"sltb_triggered", "criteria", "stats"},
    "tools/momentum_burst_detect.py": {"triggered", "day_pct", "volume_ratio"},
    "tools/climax_top_detect.py": {"patterns_firing", "patterns", "stats"},
    "tools/violations_detect.py": {
        "violations_firing", "violations", "violation_5_alone_full_exit",
    },
    "tools/base_stage_detect.py": {"base_stage", "new_high_today"},
    "tools/pullback_detect.py": {"detected", "criteria", "stats"},
    "tools/rsi_divergence.py": {"detected"},
    "tools/resistance_break.py": {"detected"},
    "tools/earnings_calendar.py": {
        "next_earnings_date", "trading_days_to_earnings", "within_blackout_window",
    },
    "tools/prior_rally_pct.py": {
        "rally_3m_pct", "rally_6m_pct", "neglected", "neglected_threshold",
    },
}


class TraceRerunError(RuntimeError):
    """Raised when a pure-tool re-run diverges from the recorded output."""


@dataclass
class StepRerunResult:
    step_id: int
    tool: str
    klass: str                          # "pure" | "ohlcv" | "manual" | "unknown"
    status: str                          # "match" | "divergent" | "shape_ok" | "shape_fail" | "well_formed" | "unknown_tool"
    detail: str = ""
    diffs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TraceRerunReport:
    results: list[StepRerunResult]

    @property
    def divergent_count(self) -> int:
        return sum(1 for r in self.results if r.status in {"divergent", "shape_fail"})

    @property
    def has_divergence(self) -> bool:
        return self.divergent_count > 0


def _classify(tool: str) -> str:
    if tool.startswith("manual:"):
        return "manual"
    if tool in OHLCV_SHAPES:
        return "ohlcv"
    if tool in _PURE_REGISTRY:
        return "pure"
    return "unknown"


_PURE_REGISTRY: dict[str, Callable[..., TraceEntry]] = {}


def _ensure_registry_loaded() -> None:
    global _PURE_REGISTRY
    if not _PURE_REGISTRY:
        _PURE_REGISTRY = _build_pure_registry()


def _values_close(a: Any, b: Any, rel: float, abs_: float) -> bool:
    """Recursive value-equality with float tolerance."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_close(a[k], b[k], rel, abs_) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_close(x, y, rel, abs_) for x, y in zip(a, b))
    return a == b


def _diff(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    """Produce structured diff entries between two output values."""
    diffs: list[dict[str, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a.keys()) | set(b.keys()):
            if k not in a:
                diffs.append({"path": f"{path}.{k}", "recorded": "<missing>", "rerun": b[k]})
            elif k not in b:
                diffs.append({"path": f"{path}.{k}", "recorded": a[k], "rerun": "<missing>"})
            else:
                diffs.extend(_diff(a[k], b[k], f"{path}.{k}"))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append({"path": path, "recorded_len": len(a), "rerun_len": len(b)})
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs.extend(_diff(x, y, f"{path}[{i}]"))
    else:
        if not _values_close(a, b, DEFAULT_REL_TOL, DEFAULT_ABS_TOL):
            diffs.append({"path": path or ".", "recorded": a, "rerun": b})
    return diffs


def _check_pure(step: dict) -> StepRerunResult:
    sid = step["id"]
    tool = step["tool"]
    fn = _PURE_REGISTRY[tool]
    recorded_inputs = step.get("inputs") or {}
    recorded_output = step.get("output")
    # Strip ``v1_preliminary`` housekeeping key that some tools accept on input
    # but the compute() signature doesn't take.
    call_inputs = {
        k: v for k, v in recorded_inputs.items() if k not in {"v1_preliminary"}
    }
    try:
        rerun_entry = fn(**call_inputs)
    except TypeError as exc:
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="divergent",
            detail=f"inputs signature mismatch: {exc}",
        )
    except Exception as exc:
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="divergent",
            detail=f"rerun raised {type(exc).__name__}: {exc}",
        )
    if _values_close(recorded_output, rerun_entry.output, DEFAULT_REL_TOL, DEFAULT_ABS_TOL):
        return StepRerunResult(step_id=sid, tool=tool, klass="pure", status="match")
    diffs = _diff(recorded_output, rerun_entry.output)
    return StepRerunResult(
        step_id=sid,
        tool=tool,
        klass="pure",
        status="divergent",
        detail=f"{len(diffs)} mismatch(es) — first: {diffs[0] if diffs else 'n/a'}",
        diffs=diffs,
    )


def _check_ohlcv(step: dict) -> StepRerunResult:
    sid = step["id"]
    tool = step["tool"]
    expected_keys = OHLCV_SHAPES[tool]
    output = step.get("output")
    if not isinstance(output, dict):
        return StepRerunResult(
            step_id=sid, tool=tool, klass="ohlcv",
            status="shape_fail",
            detail=f"output is not a dict; got {type(output).__name__}",
        )
    missing = expected_keys - set(output.keys())
    if missing:
        return StepRerunResult(
            step_id=sid, tool=tool, klass="ohlcv",
            status="shape_fail",
            detail=f"missing expected output keys: {sorted(missing)}",
        )
    return StepRerunResult(
        step_id=sid, tool=tool, klass="ohlcv",
        status="shape_ok",
        detail=(
            "OHLCV-consuming tool: shape verified. Value re-run deferred to "
            "Phase 5 backtest harness (current bars will have advanced)."
        ),
    )


def _check_manual(step: dict) -> StepRerunResult:
    sid = step["id"]
    tool = step["tool"]
    # Just verify well-formedness: tool + output + fetched_at present, which
    # trace_validate already enforces, but we still echo the verdict.
    return StepRerunResult(
        step_id=sid, tool=tool, klass="manual",
        status="well_formed",
        detail="manual-source step; no programmatic re-run path",
    )


def rerun(ledger: dict) -> TraceRerunReport:
    """Walk ``ledger.reasoning_trace`` and re-run / shape-check each step."""
    _ensure_registry_loaded()
    results: list[StepRerunResult] = []
    for step in ledger.get("reasoning_trace") or []:
        if not isinstance(step, dict):
            continue
        if "id" not in step or "tool" not in step:
            continue
        tool = step["tool"]
        klass = _classify(tool)
        if klass == "pure":
            results.append(_check_pure(step))
        elif klass == "ohlcv":
            results.append(_check_ohlcv(step))
        elif klass == "manual":
            results.append(_check_manual(step))
        else:
            results.append(
                StepRerunResult(
                    step_id=step["id"], tool=tool, klass="unknown",
                    status="unknown_tool",
                    detail=(
                        f"tool {tool!r} not in PURE_REGISTRY or OHLCV_SHAPES and "
                        "not tagged manual:* — add to one of the registries"
                    ),
                )
            )
    return TraceRerunReport(results=results)


def compute(ledger: dict) -> TraceEntry:
    report = rerun(ledger)
    return TraceEntry(
        tool=TOOL,
        inputs={"trace_step_count": len(report.results)},
        output={
            "divergent_count": report.divergent_count,
            "has_divergence": report.has_divergence,
            "results": [
                {
                    "step_id": r.step_id,
                    "tool": r.tool,
                    "klass": r.klass,
                    "status": r.status,
                    "detail": r.detail,
                    "diffs": r.diffs,
                }
                for r in report.results
            ],
        },
    )


def assert_traces_consistent(ledger: dict) -> TraceEntry:
    """Run :func:`compute`; raise :class:`TraceRerunError` on any divergence."""
    entry = compute(ledger)
    if entry.output["has_divergence"]:
        first = next(
            r for r in entry.output["results"]
            if r["status"] in {"divergent", "shape_fail"}
        )
        raise TraceRerunError(
            f"trace re-run failed for step id={first['step_id']} tool={first['tool']}: "
            f"{first['status']} — {first['detail']}"
        )
    return entry
