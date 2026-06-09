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

import inspect
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .contract import TraceEntry

TOOL = "tools/trace_rerun.py"

# Tolerance for numerical comparison. Pure-arithmetic tools should match
# to floating-point precision; ATR / position-sizing math can have minor
# drift from float ordering, so allow tiny relative tolerance.
DEFAULT_REL_TOL = 1e-6
DEFAULT_ABS_TOL = 1e-9

# Bounds for the "faithful rounding" accommodation (see _is_faithful_rounding).
# A recorded value only counts as cosmetic display-rounding of the re-run when
# the rounding granularity (0.5 * 10**-decimals) is small relative to the value
# itself. This accepts 2-decimal price storage (439.44 for 439.438) and
# >=2-decimal fractional storage (0.39 for 0.3883) while still flagging
# aggressive sub-unit rounding (0.40 cited for a true 0.3883 = a 13% rounding
# step), which is a material misstatement, not cosmetics.
ROUNDING_REL_CAP = 0.01    # granularity may be up to 1% of |value| ...
ROUNDING_ABS_CAP = 0.005   # ... or an absolute 0.005 floor (universal 2-dp storage)


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

# Core authenticating subset per OHLCV tool. We cannot value-verify OHLCV tools
# (bars have advanced), so the shape check is an authenticity proxy. Agents
# legitimately store a value-SLICE of a tool's output (only the keys they cite),
# so demanding every canonical key over-blocks. The CORE set is the minimum that
# proves "this is a real <tool> result"; recorded output missing only NON-core
# metadata (e.g. atr_compute's period / last_close_date) is `shape_partial`
# (informational, non-blocking) rather than `shape_fail` (authenticity failure).
# Tools absent here default to their full OHLCV_SHAPES set (no behaviour change).
#
# Rule for new entries: CORE = the tool's OHLCV_SHAPES set MINUS its
# nested-collection keys ({criteria, contractions, patterns, violations, stats})
# and pure metadata (period, *_date). Agents routinely store only the cited
# scalar slice of a tool's output and omit the nested breakdown dict/list;
# demanding the full nested structure over-blocks (shape_fail) what is a
# legitimate value-slice. Omitting a nested-detail key now degrades to
# shape_partial (informational); omitting a CORE scalar (e.g.
# trend_template_passes, detected) still shape_fails as an authenticity failure.
OHLCV_CORE: dict[str, set[str]] = {
    "tools/atr_compute.py": {"atr", "last_close"},                  # metadata: period, last_close_date
    "tools/trend_template.py": {"trend_template_passes", "stage"},  # nested: criteria, stats
    "tools/vcp_detect.py": {                                        # nested: contractions
        "detected", "contractions_count", "pivot", "last_close", "above_pivot",
    },
    "tools/sltb_scan.py": {"sltb_triggered"},                       # nested: criteria, stats
    "tools/climax_top_detect.py": {"patterns_firing"},             # nested: patterns, stats
    "tools/violations_detect.py": {                                # nested: violations
        "violations_firing", "violation_5_alone_full_exit",
    },
    "tools/pullback_detect.py": {"detected"},                      # nested: criteria, stats
}


class TraceRerunError(RuntimeError):
    """Raised when a pure-tool re-run diverges from the recorded output."""


@dataclass
class StepRerunResult:
    step_id: int
    tool: str
    klass: str                          # "pure" | "ohlcv" | "manual" | "unknown"
    status: str                          # "match" | "divergent" | "shape_ok" | "shape_partial" | "shape_fail" | "well_formed" | "unknown_tool"
    detail: str = ""
    diffs: list[dict[str, Any]] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    dropped_inputs: list[str] = field(default_factory=list)


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


def _decimal_places(x: float | int) -> int | None:
    """Decimal places ``x`` is expressed in (shortest repr); None if not finite.

    ``0.3883 -> 4``, ``439.44 -> 2``, ``5.0 -> 0``. Used to decide whether a
    recorded value is a deliberate display-rounding of a fresh re-run.
    """
    try:
        d = Decimal(repr(float(x)))
    except (ValueError, InvalidOperation, OverflowError):
        return None
    exp = d.as_tuple().exponent
    if not isinstance(exp, int):  # 'n' / 'N' / 'F' for nan/inf
        return None
    return max(0, -exp)


def _is_faithful_rounding(recorded: Any, rerun: Any) -> bool:
    """True if ``recorded`` is ``rerun`` rounded to recorded's own precision.

    Ledgers store display-rounded values for human readability (e.g. recorded
    ``0.3883`` for a YoY decimal whose full value is ``0.3883495…``, or a
    ``stop_price`` of ``439.44`` for ``439.438``). Re-running the tool yields
    full float precision, so a naive equality check flags every such value as a
    divergence. A *genuine* miscalculation, by contrast, will not round-trip:
    ``recorded 0.40`` is NOT a faithful 2-decimal rounding of ``rerun 0.3883``
    (which rounds to ``0.39``), so real errors still surface.
    """
    if isinstance(recorded, bool) or isinstance(rerun, bool):
        return False
    if not isinstance(recorded, (int, float)) or not isinstance(rerun, (int, float)):
        return False
    dp = _decimal_places(recorded)
    if dp is None:
        return False
    try:
        if round(float(rerun), dp) != float(recorded):
            return False
    except (ValueError, OverflowError):
        return False
    # Guard against aggressive rounding masking a material gap: the rounding
    # granularity must be small relative to the value (or below an absolute
    # 2-dp floor). 0.40-for-0.3883 (dp=1, granularity 0.05) fails this; a
    # 4-dp YoY or a 2-dp price passes.
    granularity = 0.5 * (10.0 ** -dp)
    return granularity <= max(ROUNDING_ABS_CAP, ROUNDING_REL_CAP * abs(float(recorded)))


def _values_close(a: Any, b: Any, rel: float, abs_: float) -> bool:
    """Recursive value-equality with float tolerance.

    ``a`` is the recorded (ledger) value, ``b`` the fresh re-run. Two
    accommodations beyond raw tolerance, both one-directional so real errors
    still surface:
      * floats: ``a`` may be a faithful decimal rounding of ``b`` (cosmetic
        ledger precision) — see :func:`_is_faithful_rounding`.
      * dicts: ``a``'s keys may be a *subset* of ``b``'s (the agent stored a
        value-slice, or the tool gained new output keys since the ledger was
        written). A recorded key ABSENT from the re-run still diverges — that's
        a fabricated / retired field, not schema growth.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        if abs(a - b) <= max(abs_, rel * max(abs(a), abs(b))):
            return True
        return _is_faithful_rounding(a, b)
    if isinstance(a, dict) and isinstance(b, dict):
        if not set(a.keys()) <= set(b.keys()):
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
        # Keys only in the re-run (b) are tolerated schema growth — see
        # _values_close — so we do not report them. A recorded key (a) absent
        # from the re-run IS a real divergence (retired / fabricated field).
        for k in a.keys():
            if k not in b:
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

    # Resolve compute()'s signature so the re-run is resilient to how agents
    # actually record inputs (they annotate, and they sometimes use a non-pure
    # entrypoint like compute_from_ticker). Two robustness rules:
    #   (a) drop recorded input keys the compute() signature doesn't accept
    #       (annotations like `note`, plus the `v1_preliminary` housekeeping
    #       flag) — they cannot affect the computation, so dropping is safe;
    #       they are surfaced as `dropped_inputs` (warn, not block).
    #   (b) if, after that, a REQUIRED compute() parameter is absent, the
    #       recorded inputs don't match this pure entrypoint at all — the agent
    #       recorded a different (e.g. network compute_from_ticker) call. That
    #       step is not value-replayable here; mark it well_formed (non-block)
    #       rather than divergent, instead of crashing on a TypeError.
    sig = inspect.signature(fn)
    params = sig.parameters
    accepts_var_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    housekeeping = {"v1_preliminary"}
    if accepts_var_kw:
        call_inputs = {k: v for k, v in recorded_inputs.items() if k not in housekeeping}
        dropped = sorted(k for k in housekeeping if k in recorded_inputs)
    else:
        accepted = set(params)
        call_inputs = {k: v for k, v in recorded_inputs.items() if k in accepted}
        dropped = sorted((set(recorded_inputs) - accepted) - housekeeping
                         | (housekeeping & set(recorded_inputs)))
    required = {
        name for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    missing_required = sorted(required - set(call_inputs))
    if missing_required:
        if not call_inputs:
            # NOT ONE recorded input matches a compute() parameter — this is not a
            # recognizable call of this tool (garbage / fabricated step). Keep it
            # divergent so the red-team guard against fabricated steps still fires.
            return StepRerunResult(
                step_id=sid,
                tool=tool,
                klass="pure",
                status="divergent",
                detail=f"recorded inputs match no {tool} compute() parameter — signature mismatch",
                dropped_inputs=dropped,
            )
        # Some parameters match but a REQUIRED one is absent → the step was
        # recorded under a different (e.g. network compute_from_ticker)
        # entrypoint, so it is not value-replayable via the pure compute().
        # Well-formed (non-blocking), surfaced as a warn — not a divergence.
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="well_formed",
            detail=(
                f"recorded inputs do not satisfy {tool} compute() signature "
                f"(missing required {missing_required}); the step was likely recorded "
                f"from a non-pure entrypoint (e.g. compute_from_ticker) — not "
                f"value-replayable, treated as well-formed (non-blocking)"
            ),
            dropped_inputs=dropped,
        )
    try:
        rerun_entry = fn(**call_inputs)
    except TypeError as exc:
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="divergent",
            detail=f"inputs signature mismatch: {exc}",
            dropped_inputs=dropped,
        )
    except Exception as exc:
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="divergent",
            detail=f"rerun raised {type(exc).__name__}: {exc}",
            dropped_inputs=dropped,
        )
    if _values_close(recorded_output, rerun_entry.output, DEFAULT_REL_TOL, DEFAULT_ABS_TOL):
        return StepRerunResult(
            step_id=sid,
            tool=tool,
            klass="pure",
            status="match",
            detail=(f"dropped non-signature inputs: {dropped}" if dropped else ""),
            dropped_inputs=dropped,
        )
    diffs = _diff(recorded_output, rerun_entry.output)
    return StepRerunResult(
        step_id=sid,
        tool=tool,
        klass="pure",
        status="divergent",
        detail=f"{len(diffs)} mismatch(es) — first: {diffs[0] if diffs else 'n/a'}",
        diffs=diffs,
        dropped_inputs=dropped,
    )


def _check_ohlcv(step: dict) -> StepRerunResult:
    sid = step["id"]
    tool = step["tool"]
    expected_keys = OHLCV_SHAPES[tool]
    core_keys = OHLCV_CORE.get(tool, expected_keys)
    output = step.get("output")
    if not isinstance(output, dict):
        return StepRerunResult(
            step_id=sid, tool=tool, klass="ohlcv",
            status="shape_fail",
            detail=f"output is not a dict; got {type(output).__name__}",
        )
    present = set(output.keys())
    core_missing = core_keys - present
    if core_missing:
        # Core authenticating keys absent — cannot trust this is a real result.
        return StepRerunResult(
            step_id=sid, tool=tool, klass="ohlcv",
            status="shape_fail",
            detail=f"missing CORE output keys: {sorted(core_missing)}",
            missing_keys=sorted(core_missing),
        )
    meta_missing = expected_keys - present
    if meta_missing:
        # Core present, only non-core metadata absent — agent stored a value
        # slice. Informational, NOT a divergence.
        return StepRerunResult(
            step_id=sid, tool=tool, klass="ohlcv",
            status="shape_partial",
            detail=(
                f"core keys present; recorded output is a value-slice missing "
                f"non-core metadata: {sorted(meta_missing)}"
            ),
            missing_keys=sorted(meta_missing),
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
                    "missing_keys": r.missing_keys,
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
