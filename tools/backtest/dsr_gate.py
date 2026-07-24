"""Deflated Sharpe Ratio 7th gate (cherry-pick Batch C, C1).

The 6-window gate cannot see selection bias ACROSS the setup family: after
enough setup x parameter variants, the all-window survivor may be the lucky
draw, not an edge. The DSR (Bailey & Lopez de Prado 2014 — math + provenance
in tools.backtest.sharpe_stats) deflates the selected candidate's Sharpe by
the expected maximum Sharpe of ``n_trials`` skill-less trials.

Gate convention (2026-07-24 cherry-pick spec): **DSR > 0.95 required IN
ADDITION to the existing two-clause gate** for roster promotion. Wiring:

* ``quant_strategies.runner.run_spec`` calls :func:`evaluate_grid` after
  ranking — REPORT-ONLY by default; a spec that sets ``gate.dsr_min``
  (e.g. 0.95) makes it a hard veto on the selected combo. Existing spec
  files are unchanged (no silent re-gating of history); NEW roster
  promotions must record a DSR verdict per CLAUDE.md.
* ``python -m tools.backtest.dsr_gate roster`` evaluates the current live
  roster properly (exact daily-return moments via a walk-forward re-run).

Trial counting (the paper's N): ``ledgers/trials.yml`` is the append-only
registry of every setup x variant ever evaluated, seeded by
:func:`derive_trial_components` (grid sizes from every strategy spec +
recorded sweep artifacts). Claude1 promotes the best candidate across the
WHOLE family, so N is the GLOBAL trial count — and where the true
correlation between trials is unmeasured we use raw N, which overstates
E[max SR] and therefore deflates MORE (conservative; Eq. 9's
``effective_trials`` is available once correlations are measured).

License note per the spec: the AGPL pypbo and all-rights-reserved mlfinlab
reference implementations were NOT read or ported — the implementation is
from the paper's own formulas, cross-checked against the paper's published
worked example (tests/test_sharpe_stats.py).
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from . import sharpe_stats

_ROOT = Path(__file__).resolve().parents[2]
TRIALS_REGISTRY_PATH = _ROOT / "ledgers" / "trials.yml"
SPEC_DIR = _ROOT / "tools" / "quant_strategies"
SWEEP_DIR = _ROOT / "ledgers" / "improvements"

DSR_MIN_DEFAULT = 0.95
TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Trial registry
# ---------------------------------------------------------------------------

def _grid_size(params: dict[str, Any]) -> int:
    n = 1
    for v in params.values():
        if isinstance(v, list) and v:
            n *= len(v)
    return n


def derive_trial_components(
    spec_dir: Optional[Path] = None, sweep_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Reproducible trial count: one component per evidence source.

    * Every strategy spec's param grid — each combo the runner evaluates is a
      trial (the runner runs the FULL grid every time a spec is judged).
    * Every recorded sweep JSON in ledgers/improvements — each element that
      carries a Sharpe is a trial from a param sweep outside the spec grids.

    This UNDERCOUNTS true history (ad-hoc runs that left no artifact are
    invisible) — which under-deflates. The registry is therefore a FLOOR;
    CLAUDE.md requires new sweeps to append to it.
    """
    components: list[dict[str, Any]] = []
    sd = Path(spec_dir) if spec_dir else SPEC_DIR
    for spec_path in sorted(sd.glob("*.yml")):
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if "kind" not in spec:
            continue
        n = _grid_size(spec.get("params") or {})
        components.append({
            "source": f"spec-grid:{spec_path.name}", "n_trials": n,
            "detail": f"param-grid product of {spec_path.name}",
        })
    wd = Path(sweep_dir) if sweep_dir else SWEEP_DIR
    for p in sorted(wd.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(doc, list):
            n = sum(1 for el in doc if isinstance(el, dict) and _mentions_sharpe(el))
            if n:
                components.append({
                    "source": f"sweep:{p.name}", "n_trials": n,
                    "detail": "recorded sweep artifact (one trial per element)",
                })
    return components


def _mentions_sharpe(el: dict[str, Any]) -> bool:
    def walk(x) -> bool:
        if isinstance(x, dict):
            return any(k == "sharpe" or walk(v) for k, v in x.items())
        return False
    return walk(el)


def write_registry(
    components: list[dict[str, Any]], path: Optional[Path] = None,
) -> Path:
    p = Path(path) if path else TRIALS_REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "as_of": date.today().isoformat(),
        "note": (
            "Append-only trial registry for the C1 Deflated-Sharpe gate. "
            "Every setup x variant evaluation counts as a trial. Re-derive "
            "with `python -m tools.backtest.dsr_gate derive --write` after "
            "new sweeps, or append a manual component. The total is a FLOOR "
            "(unrecorded ad-hoc runs are invisible) — a floor UNDER-deflates, "
            "so keep it current."
        ),
        "n_trials_total": sum(c["n_trials"] for c in components),
        "components": components,
    }
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p


def registry_total(path: Optional[Path] = None) -> int:
    p = Path(path) if path else TRIALS_REGISTRY_PATH
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return int(doc["n_trials_total"])
    except (FileNotFoundError, KeyError, ValueError, OSError):
        # No registry yet: derive live (slower, same floor semantics).
        return sum(c["n_trials"] for c in derive_trial_components())


# ---------------------------------------------------------------------------
# Grid evaluation (runner wiring)
# ---------------------------------------------------------------------------

def evaluate_grid(
    ranked: list[dict[str, Any]],
    *,
    registry_trials: Optional[int] = None,
    dsr_min: Optional[float] = None,
    periods_per_year: int = TRADING_DAYS,
) -> Optional[dict[str, Any]]:
    """DSR for the TOP-RANKED combo of a run_spec grid.

    Inputs taken from the ranked combo list: the grid's per-combo annualized
    Sharpes give the trial variance; the top combo's concatenated OOS
    outcomes give the return-shape moments (skew / raw kurtosis of the
    r-multiple series — scale-invariant, so R units are fine) and T.

    ``dsr_min=None`` -> report-only (``enforced``=False). Returns None when
    the grid has < 2 combos (no trial variance to estimate).
    """
    if len(ranked) < 2:
        return None
    sr_ann = [
        float(r["oos_report"].returns.sharpe_annualised) for r in ranked
        if r.get("oos_report") is not None
    ]
    if len(sr_ann) < 2:
        return None
    per_period = [s / math.sqrt(periods_per_year) for s in sr_ann]
    mean_pp = sum(per_period) / len(per_period)
    var_trials = sum((s - mean_pp) ** 2 for s in per_period) / (len(per_period) - 1)

    top = ranked[0]
    outcomes = top.get("concat_oos_outcomes") or []
    returns_seq = [
        float(o.r_multiple) for o in outcomes
        if getattr(o, "r_multiple", None) is not None
    ]
    top_sr_pp = float(top["oos_report"].returns.sharpe_annualised) / math.sqrt(periods_per_year)
    note = ""
    if len(returns_seq) >= 4:
        try:
            m = sharpe_stats.sharpe_moments(returns_seq)
            skew, kurt, t_obs = m["skew"], m["kurt_raw"], m["n"]
        except ValueError:
            skew, kurt, t_obs = 0.0, 3.0, len(returns_seq)
            note = "degenerate outcome series — Normal moments assumed"
    else:
        skew, kurt = 0.0, 3.0
        t_obs = max(len(returns_seq), int(top["oos_report"].trades.n_trades))
        note = "no outcome series available — Normal moments assumed (under-deflates fat tails)"

    n_trials = max(len(sr_ann), registry_trials if registry_trials is not None else registry_total())
    if var_trials <= 0:
        return {
            "dsr": None, "sr0_ann": None, "n_trials": n_trials,
            "var_trials_per_period": 0.0, "t_obs": t_obs,
            "enforced": dsr_min is not None, "passed": None,
            "note": "zero variance across trial Sharpes — DSR undefined",
        }
    out = sharpe_stats.deflated_sharpe(
        top_sr_pp, t_obs, skew, kurt,
        var_trials=var_trials, n_trials=int(n_trials),
    )
    dsr = out["dsr"]
    threshold = dsr_min if dsr_min is not None else DSR_MIN_DEFAULT
    return {
        "dsr": dsr,
        "sr0_ann": sharpe_stats.per_period_to_annualized(out["sr0"], periods_per_year),
        "n_trials": int(n_trials),
        "var_trials_per_period": var_trials,
        "t_obs": t_obs,
        "skew": skew,
        "kurt_raw": kurt,
        "threshold": threshold,
        "enforced": dsr_min is not None,
        "passed": bool(dsr > threshold),
        "note": note,
    }


def format_block(block: Optional[dict[str, Any]]) -> list[str]:
    """Markdown lines for the run_spec report."""
    if block is None:
        return ["", "## 7th gate — Deflated Sharpe (C1)", "",
                "- not computed (grid has < 2 combos — no trial variance)"]
    lines = ["", "## 7th gate — Deflated Sharpe (C1)", ""]
    if block["dsr"] is None:
        lines.append(f"- {block['note']}")
        return lines
    verdict = "PASS" if block["passed"] else "FAIL"
    mode = "ENFORCED" if block["enforced"] else "report-only"
    lines += [
        f"- **DSR = {block['dsr']:.4f}** vs threshold {block['threshold']} -> "
        f"**{verdict}** ({mode})",
        f"- E[max SR] under H0 across N={block['n_trials']} trials: "
        f"{block['sr0_ann']:.2f} annualized (SR0)",
        f"- inputs: T={block['t_obs']} obs · skew {block['skew']:.2f} · "
        f"raw kurtosis {block['kurt_raw']:.2f} · "
        f"var(trial SR, per-period) {block['var_trials_per_period']:.6f}",
        "- N = global trial-registry floor (ledgers/trials.yml) — raw count, "
        "no correlation shrink: conservative (deflates more)",
    ]
    if block["note"]:
        lines.append(f"- caveat: {block['note']}")
    if not block["passed"]:
        lines.append(
            "- **surfaced, not auto-retired** — operator reviews (per C1 spec)"
        )
    return lines


# ---------------------------------------------------------------------------
# Roster evaluation (exact moments via walk-forward re-run)
# ---------------------------------------------------------------------------

def evaluate_roster_setup(
    setup: str,
    *,
    sweep_json: Optional[Path] = None,
    registry_trials: Optional[int] = None,
) -> dict[str, Any]:
    """DSR for a deployed roster setup with EXACT daily-return moments:
    re-runs the deployed combo through the net-of-cost walk-forward
    (cached data), takes the aggregate OOS equity curve's daily returns.

    Trial variance comes from the recorded sweep JSON for the setup when
    given (per-combo net OOS Sharpes), else from the spec grid re-run is
    NOT attempted — the caller must supply sweep evidence.
    """
    import scripts.volume_share_slippage_rerun as rerun  # reuse the collector
    from ..auto_paper import config as ap_config

    data = ap_config.load()
    rows: dict[str, dict] = {}
    for section in ("deployable", "parked_by_concurrent_cap_reveals_weak_edge",
                    "parked_by_tightened_gate"):
        for r in data.get(section, []) or []:
            if isinstance(r, dict) and "setup" in r:
                rows.setdefault(r["setup"], r)
    signals, dfs, spec, start, end = rerun._collect(setup, rows.get(setup, {}))
    wf = rerun._wf(signals, dfs, spec, start, end)
    equity = wf.aggregate.equity_curve
    daily = equity.pct_change().dropna()
    m = sharpe_stats.sharpe_moments(list(daily))

    var_trials = None
    n_sweep = 0
    if sweep_json is not None and Path(sweep_json).exists():
        doc = json.loads(Path(sweep_json).read_text(encoding="utf-8"))
        srs = [
            el["oos_agg"]["sharpe"] / math.sqrt(TRADING_DAYS)
            for el in doc
            if isinstance(el, dict) and isinstance(el.get("oos_agg"), dict)
            and el["oos_agg"].get("sharpe") is not None
        ]
        n_sweep = len(srs)
        if len(srs) >= 2:
            mean = sum(srs) / len(srs)
            var_trials = sum((s - mean) ** 2 for s in srs) / (len(srs) - 1)
    if var_trials is None:
        raise ValueError(
            f"no per-trial Sharpe evidence for {setup} — pass --sweep-json "
            "with a recorded sweep artifact"
        )
    n_trials = max(
        n_sweep, registry_trials if registry_trials is not None else registry_total(),
    )
    out = sharpe_stats.deflated_sharpe(
        m["sr"], m["n"], m["skew"], m["kurt_raw"],
        var_trials=var_trials, n_trials=int(n_trials),
    )
    return {
        "setup": setup,
        "sr_ann": sharpe_stats.per_period_to_annualized(m["sr"]),
        "t_obs": m["n"], "skew": m["skew"], "kurt_raw": m["kurt_raw"],
        "n_trials": int(n_trials), "var_trials_per_period": var_trials,
        "sr0_ann": sharpe_stats.per_period_to_annualized(out["sr0"]),
        "dsr": out["dsr"],
        "passed": bool(out["dsr"] > DSR_MIN_DEFAULT),
        "threshold": DSR_MIN_DEFAULT,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Deflated Sharpe 7th gate (C1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dp = sub.add_parser("derive", help="derive the trial-count registry")
    dp.add_argument("--write", action="store_true", help="write ledgers/trials.yml")
    rp = sub.add_parser("roster", help="evaluate a deployed setup (exact moments)")
    rp.add_argument("--setup", required=True)
    rp.add_argument("--sweep-json", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "derive":
        components = derive_trial_components()
        total = sum(c["n_trials"] for c in components)
        for c in components:
            print(f"{c['n_trials']:>5}  {c['source']}")
        print(f"{total:>5}  TOTAL")
        if args.write:
            print(f"wrote {write_registry(components)}")
        return 0

    res = evaluate_roster_setup(
        args.setup,
        sweep_json=Path(args.sweep_json) if args.sweep_json else None,
    )
    print(json.dumps(res, indent=2))
    print(f"\nDSR {res['dsr']:.4f} vs {res['threshold']} -> "
          f"{'PASS' if res['passed'] else 'FAIL (surfaced, not auto-retired)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
