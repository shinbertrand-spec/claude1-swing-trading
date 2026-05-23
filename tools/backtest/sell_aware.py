"""Per-bar sell-discipline composer for the backtest simulator.

Wraps the 4 OHLCV-derivable sell-discipline detectors into one decision:

* :mod:`tools.climax_top_detect`        (6 climax-top patterns)
* :mod:`tools.violations_detect`        (5 post-entry violations)
* :mod:`tools.base_stage_detect`        (base count 1-5 + new-high flag)
* :mod:`tools.sell_into_strength`       (10-15% in 2-3 days)

P/E expansion (``tools.pe_expansion_check``) needs fundamentals history
which OHLCV alone can't provide; omitted in Phase 5.c. M_analyst /
fundamentals-driven triggers are similarly deferred to Phase 5.d (real
fundamentals source).

Used by the simulator when ``sell_policy.enabled`` is True. If the
composer returns ``action != "hold"`` on any bar, the simulator exits
at that bar's close with ``exit_reason="sell_decision_<action>"``.

# v1-preliminary: revisit after Minervini book v2 ingestion (per
# swing-sell-discipline). Composer thresholds match Phase 2's
# tools.sell_decision; tag every test that depends on a specific
# threshold so v2 changes surface cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..base_stage_detect import compute_from_ohlcv as base_stage_compute
from ..climax_top_detect import compute_from_ohlcv as climax_compute
from ..sell_decision import compute as sell_decision_compute
from ..sell_into_strength import compute as sis_compute
from ..violations_detect import compute_from_ohlcv as violations_compute


@dataclass(frozen=True)
class SellPolicy:
    """Per-bar sell-discipline evaluation toggle + sub-detector switches.

    Defaults: all four OHLCV-derived detectors active. Disable any to
    isolate which trigger family is responsible for changes in metrics.

    ``regime_class`` is a per-trade ambient field (Phase 5.c baseline;
    per-bar regime evaluation is Phase 5.d).
    """

    enabled: bool = False
    use_climax_top: bool = True
    use_violations: bool = True
    use_base_stage: bool = True          # expensive — disable for speed if not testing late-stage exits
    use_sell_into_strength: bool = True
    regime_class: str = "stage_2_confirmed"
    # Skip the first N bars to give the position room. Climax-top patterns
    # over a 2-3 day window will trivially fire on the entry day itself.
    grace_period_bars: int = 3


@dataclass
class SellDecisionEvent:
    """One per-bar evaluation outcome (informational; the simulator only
    needs ``action``)."""

    bar_date: date
    action: str                              # "hold" | "tighten_stop" | "sell_1_3" | "sell_50" | "sell_75" | "sell_100"
    confidence: str                          # "HIGH" | "MEDIUM" | "LOW"
    climax_patterns_firing: int
    violations_firing: int
    base_stage: int
    sell_into_strength_triggered: bool
    contributing_triggers: list[str]


def evaluate_bar(
    *,
    df_through_today: pd.DataFrame,
    starter_entry: float,
    fill_date: date,
    setup_grade: str,
    policy: SellPolicy,
) -> SellDecisionEvent:
    """Run the 4 detectors + composer on the bar ending ``df_through_today``.

    Args:
        df_through_today: OHLCV slice from history through the current
            bar (inclusive).
        starter_entry: STARTER leg entry price (for sell-into-strength
            gain computation).
        fill_date: STARTER fill date (for violations_detect's entry-anchor).
        setup_grade: the trade's setup grade (per-grade modifier in
            sell_decision).
        policy: which sub-detectors to run.

    Returns:
        :class:`SellDecisionEvent` describing the composed action.
    """
    bar_date = pd.Timestamp(df_through_today.index[-1]).date()

    climax_count = 0
    if policy.use_climax_top:
        try:
            r = climax_compute(df_through_today)
            climax_count = int(r.output["patterns_firing"])
        except (ValueError, KeyError):
            climax_count = 0

    violations_count = 0
    violation_5_alone = False
    if policy.use_violations:
        try:
            r = violations_compute(df_through_today, entry_date=fill_date)
            violations_count = int(r.output["violations_firing"])
            violation_5_alone = bool(r.output["violation_5_alone_full_exit"])
        except (ValueError, KeyError):
            pass

    base_stage = 1
    new_high_today = False
    if policy.use_base_stage:
        try:
            r = base_stage_compute(df_through_today)
            base_stage = int(r.output["base_stage"])
            new_high_today = bool(r.output["new_high_today"])
        except (ValueError, KeyError):
            pass

    sis_triggered = False
    sis_fraction = 0.0
    if policy.use_sell_into_strength:
        bars_since_fill = sum(
            1 for ts in df_through_today.index
            if pd.Timestamp(ts).date() >= fill_date
        )
        bars_since_fill = max(1, bars_since_fill)
        # gain over the LAST 3 bars (per sell_into_strength 2-3 day window).
        if len(df_through_today) >= 4:
            recent_close = float(df_through_today["Close"].iloc[-1])
            base_close = float(df_through_today["Close"].iloc[-4])
            gain_pct = (recent_close / base_close) - 1.0 if base_close > 0 else 0.0
            try:
                r = sis_compute(
                    gain_pct=gain_pct,
                    days_in_move=min(3, bars_since_fill),
                    setup_grade=setup_grade,
                )
                sis_triggered = bool(r.output["threshold_met"])
                sis_fraction = float(r.output["recommended_fraction"])
            except ValueError:
                pass

    decision = sell_decision_compute(
        climax_patterns_firing=climax_count,
        violations_firing=violations_count,
        violation_5_alone_full_exit=violation_5_alone,
        base_stage=base_stage,
        new_high_today=new_high_today,
        sell_into_strength_triggered=sis_triggered,
        sell_into_strength_fraction=sis_fraction,
        setup_grade=setup_grade,
        pe_expansion_warning=False,    # OHLCV-only — pe needs fundamentals (Phase 5.d)
        regime_class=policy.regime_class,
    )

    return SellDecisionEvent(
        bar_date=bar_date,
        action=decision.output["action"],
        confidence=decision.output["confidence"],
        climax_patterns_firing=climax_count,
        violations_firing=violations_count,
        base_stage=base_stage,
        sell_into_strength_triggered=sis_triggered,
        contributing_triggers=list(decision.output.get("contributing_triggers", [])),
    )


def exit_action_to_reason(action: str) -> str:
    """Map a sell_decision action to a simulator exit_reason string."""
    return f"sell_decision_{action}"
