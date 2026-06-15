"""Auto-paper run entrypoint — 3-phase Python/LLM filesystem-protocol boundary.

Orchestrates the auto-paper pipeline across the LLM/Python boundary defined in
[auto-paper LLM/Python boundary spec 2026-05-28]. Python owns all state +
broker calls; LLM owns ONLY subagent invocations + JSON envelope saving.

Three phases, each idempotent + crash-safe:

  --phase init          Load candidates → screener → shell ledgers → emit
                        skeptic invocations.
  --phase post_skeptic  Read skeptic envelopes → emit panel invocations.
  --phase post_panel    Read panel envelopes → aggregate → place trades.

Failure-loud invariants (see :mod:`tools.auto_paper.errors`):
  - :exc:`MissingEnvelopeError`: phase expected envelope file that's missing.
  - :exc:`OutOfOrderPhaseError`: phase ran without prerequisite phase complete.
  - :exc:`RunDirCorruptError`: status file or expected artifacts inconsistent.

Re-running ``--phase init`` creates a NEW run dir (fresh timestamp). Re-running
``post_skeptic`` / ``post_panel`` against the SAME run_dir is safe (idempotent).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ..broker.tiger import TigerClient
from .. import atr_compute, regime_check, trend_template
from . import cron_gate, reconcile
from . import screener as screener_mod
from . import shell_ledger, state
from .critic_panel import (
    CriticVote,
    aggregate_panel,
    append_calibration_log,
    build_critic_envelope,
    build_panel_input_dict,
    save_critic_vote,
    save_panel_verdict,
    _derive_critics_list,
)
from .errors import (
    MissingEnvelopeError,
    OutOfOrderPhaseError,
    RunDirCorruptError,
)
from .pipeline import CandidateInput, place_candidate
from .quant_scanner import scan_today
from ..news_research.market_temperature import load_latest_market_temperature


# Run-directory root. Per §3.7 — gitignored, runtime artifacts.
RUN_ROOT = Path("ledgers/_auto_paper_runs")

# Skeptic envelope stub written when the LLM can't fire the agent — keeps
# the post_skeptic phase unblocking-able since Phase 2 is log-only.
SKEPTIC_STUB_VERDICT = "WEAK"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(msg: str) -> None:
    """Print one status line, Windows cp1252-safe."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_yaml(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(obj, fh, sort_keys=False)


def _read_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _new_run_dir(run_root: Path | None = None) -> Path:
    root = run_root or RUN_ROOT
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    p = root / run_id
    # If we somehow collided (sub-second), append a counter — won't happen in
    # practice (cron fires once / day) but trivial to guard.
    n = 1
    while p.exists():
        p = root / f"{run_id}-{n}"
        n += 1
    p.mkdir(parents=True)
    return p


def _latest_run_dir(run_root: Path | None = None) -> Path:
    root = run_root or RUN_ROOT
    if not root.exists():
        raise RunDirCorruptError(
            f"no run root at {root}; init must run before post_skeptic / post_panel"
        )
    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")),
        key=lambda p: p.name,
    )
    if not candidates:
        raise RunDirCorruptError(
            f"no run dirs under {root}; init must run before post_skeptic / post_panel"
        )
    return candidates[-1]


def _require_prerequisite_phase(run_dir: Path, *, expected: str) -> None:
    """Read run_dir's _status.yml; assert last_phase_completed == expected."""
    try:
        status = state.read_run_status(run_dir)
    except state.PaperAutoStateError as exc:
        raise RunDirCorruptError(str(exc)) from exc
    last = status.get("last_phase_completed")
    if last != expected:
        raise OutOfOrderPhaseError(
            f"expected last_phase_completed={expected!r}, got {last!r} "
            f"in {run_dir}/_status.yml"
        )


def _fetch_industry(ticker: str) -> str:
    """Best-effort yfinance industry lookup. Test seam — monkeypatch in tests."""
    try:
        import yfinance as yf
        info = (yf.Ticker(ticker).info or {})
        return info.get("industry", "") or ""
    except Exception:
        return ""


def _build_skeptic_prompt(
    *, ledger_path: str, bull_md_path: str
) -> str:
    """The single per-ticker prompt the LLM passes to trade-skeptic.

    Same wording as the v1 slash command Step 3b.2 used so the agent's
    output contract is unchanged. Schema-1.3 note: each invocation entry
    also carries a ``market_temperature`` field in
    ``03_skeptic_invocations.yml`` — overlay context only, never a gate.
    """
    return (
        f"Skeptic pass on `{ledger_path}`. This is a paper-auto quant pick — "
        f"the bull case is the statistical edge captured in the strategy's "
        f"walk-forward backtest (see `tools/deployable_setups.yml`), not a "
        f"discretionary narrative. The bull stub at `{bull_md_path}` "
        f"documents this. Your job is to surface the invalidation thesis — "
        f"strategy-blind failure modes the quant signal can't detect. Write "
        f"your bear thesis to `{ledger_path.replace('.yml', '-bear.md')}` "
        f"per the standard contract, and append bear-side trace_refs to the "
        f"ledger. The invocation entry in `03_skeptic_invocations.yml` "
        f"includes a `market_temperature` block (Put-Call / Fear & Greed / "
        f"AAII / VIX term) — read it as factual context for the regime "
        f"narrative, NOT as a gate (per spec § 3.4 and "
        f"[[ai-arbitrage-compression]] discipline)."
    )


# ---------------------------------------------------------------------------
# Phase init
# ---------------------------------------------------------------------------


def phase_init(
    run_dir: Path,
    *,
    today: date | None = None,
    scan_fn=None,
    screener_fn=None,
    industry_fn=None,
) -> int:
    """Phase init: scan → screen → shell ledgers → skeptic invocations.

    Args (test seams):
        scan_fn: override for :func:`tools.auto_paper.quant_scanner.scan_today`.
        screener_fn: override for :func:`tools.auto_paper.screener.screen`.
        industry_fn: override for the yfinance industry lookup.
    """
    today = today or date.today()
    scan_fn = scan_fn or scan_today
    screener_fn = screener_fn or screener_mod.screen
    industry_fn = industry_fn or _fetch_industry

    run_dir.mkdir(parents=True, exist_ok=True)
    state.write_run_status(run_dir, phase="init", started_at=_now())

    # Cron gate fast-path (Step 3, Mode B): if a PRIOR run (the post-RTH
    # reconciler) already discovered an unledgered broker orphan and set the
    # gate, refuse to scan / place until an operator reconciles + clears it.
    # Don't even touch the broker in this path.
    gated, gate_doc = cron_gate.is_gated()
    if gated:
        reason = (gate_doc or {}).get("reason", "unknown")
        state.write_run_status(
            run_dir, phase="init", completed_at=_now(),
            candidates_in=0, candidates_surviving_screener=0,
            skeptic_invocations_written=0,
            error=f"cron_gated: {reason}",
        )
        _emit(
            f"PHASE_INIT_GATED reason={reason} gate={cron_gate.GATE_PATH} "
            f"-- entry pipeline halted; operator must reconcile + clear the gate"
        )
        return 2

    # Account client (also used by the pre-session sweep below).
    c = TigerClient()

    # Pre-session orphan sweep (Priority 2 — Mode-B defense-in-depth): a FRESH
    # read-only orphan check even if the post-RTH reconciler never ran (machine
    # off / holiday / crash), so the bot self-protects before placing. Sets the
    # gate on a true orphan or a corrupt held ledger. Best-effort: a broker-fetch
    # failure here is non-fatal (the account_summary call below is the hard
    # broker dependency and will fail loud if the broker is truly down).
    try:
        sweep = reconcile.presession_sweep(client=c)
    except Exception as exc:  # never let the guard crash the run
        sweep = None
        _emit(f"[init] WARN presession sweep errored (continuing): {exc!r}")
    if sweep is not None:
        if sweep.stuck_closing:
            _emit(
                f"[init] NOTE {len(sweep.stuck_closing)} stuck-closing held "
                f"(post-RTH reconciler domain, not gated): {sweep.stuck_closing}"
            )
        if sweep.skipped:
            _emit(f"[init] WARN presession sweep skipped: {sweep.skip_reason}")
        if sweep.gated_now:
            state.write_run_status(
                run_dir, phase="init", completed_at=_now(),
                candidates_in=0, candidates_surviving_screener=0,
                skeptic_invocations_written=0,
                error="cron_gated: presession_orphan_sweep",
            )
            _emit(
                f"PHASE_INIT_GATED reason=presession_orphan_sweep "
                f"orphans={sweep.orphans} corrupt_held={sweep.corrupt_held} "
                f"gate={cron_gate.GATE_PATH} -- entry halted; operator must "
                f"reconcile + clear the gate"
            )
            return 2

    # Account + regime
    summary = c.account_summary().output
    net_liq = float(summary.get("net_liquidation") or 0.0)
    cash = float(summary.get("cash") or 0.0)
    spy = trend_template.compute_from_ticker("SPY", include_rs=False)
    regime_class, _ = regime_check.classify_broad(spy.output["trend_template_passes"])

    # Scan
    try:
        reports = scan_fn(
            account_net_liq=net_liq,
            regime_class=regime_class,
            cash_available=cash,
            today=today,
        )
    except Exception as exc:
        # Record failure in status + bail. _emit is loud-failure for cron logs.
        state.write_run_status(
            run_dir, phase="init", completed_at=_now(),
            candidates_in=0, candidates_surviving_screener=0,
            skeptic_invocations_written=0, error=f"scan_today: {exc!r}",
        )
        _emit(f"PHASE_INIT_FAIL scan_today: {exc!r}")
        return 1

    # De-dupe across reports
    seen: set[str] = set()
    scanner_cands = []
    for r in reports:
        for cand in r.candidates:
            if cand.ticker in seen:
                continue
            seen.add(cand.ticker)
            scanner_cands.append(cand)

    # Write 00_candidates.yml
    cands_payload = []
    for cand in scanner_cands:
        cands_payload.append({
            "ticker": cand.ticker,
            "setup_type": cand.setup_type,
            "setup_grade": cand.setup_grade,
            "pivot_price": float(cand.pivot_price),
            "limit_price": float(cand.limit_price),
            "stop_price": float(cand.stop_price),
            "target_price": float(cand.target_price) if cand.target_price else None,
            "shares": int(cand.shares),
            "sector_etf": cand.sector_etf,
        })
    _write_yaml(run_dir / "00_candidates.yml", {
        "candidates": cands_payload,
        "loaded_at": _now(),
        "account": {
            "net_liquidation": net_liq,
            "cash": cash,
            "regime_class": regime_class,
        },
    })

    # Phase 1 screener — record everything, surface only survivors
    screener_results: dict[str, dict[str, Any]] = {}
    surviving: list[tuple[Any, screener_mod.ScreenerResult]] = []
    for cand in scanner_cands:
        try:
            result = screener_fn(cand.ticker, claimed_sector_etf=cand.sector_etf)
        except Exception as exc:
            screener_results[cand.ticker] = {"crashed": True, "error": str(exc)}
            continue
        screener_results[cand.ticker] = result.to_dict()
        if not result.blocked:
            # Apply sector correction in-place on the cand (CandidateInput is mutable enough)
            if result.corrected_sector_etf:
                from dataclasses import replace
                cand = replace(cand, sector_etf=result.corrected_sector_etf)
            surviving.append((cand, result))
    _write_yaml(run_dir / "01_screener.yml", {
        "results": screener_results,
        "n_in": len(scanner_cands),
        "n_surviving": len(surviving),
    })

    # Shell ledgers + per-ticker industry lookup + skeptic invocations
    shell_dir = run_dir / "02_shell_ledgers"
    shell_dir.mkdir(exist_ok=True)
    skeptic_invocations: list[dict[str, Any]] = []
    envelope_dir = run_dir / "04_skeptic_envelopes"
    envelope_dir.mkdir(exist_ok=True)
    (run_dir / "06_panel_envelopes").mkdir(exist_ok=True)

    # Schema-1.3 overlay — load once for the run, threaded into per-ticker
    # skeptic invocations as factual context. ``None`` when the latest
    # snapshot is stale (>2h) or every fetcher in the block is in error,
    # so the LLM/critic sees the gap rather than rotted numbers.
    market_temperature_block = load_latest_market_temperature()

    for cand, screen_result in surviving:
        inp = shell_ledger.ShellLedgerInput(
            ticker=cand.ticker,
            setup_type=cand.setup_type,
            setup_grade=cand.setup_grade,
            pivot_price=float(cand.pivot_price),
            stop_price=float(cand.stop_price),
            sector_etf=cand.sector_etf,
            screener_output=screen_result.to_dict(),
            track=getattr(cand, "track", None),
        )
        try:
            ledger_dict, _ = shell_ledger.build_quant_shell_ledger(inp, today=today)
        except Exception as exc:
            _emit(f"[init] WARN {cand.ticker}: shell_ledger build failed: {exc!r}")
            continue

        # Canonical shell ledger lives in ledgers/candidates/YYYY-MM-DD/
        try:
            canonical_path = shell_ledger.write_shell_ledger(
                ledger_dict, ticker=cand.ticker, ledger_date=today,
            )
        except FileExistsError:
            canonical_path = (
                shell_ledger._CANDIDATES_DIR
                / today.isoformat()
                / f"{cand.ticker}.yml"
            )
            _emit(f"[init] {cand.ticker}: reusing existing canonical ledger at {canonical_path}")
        try:
            bull_md = shell_ledger.write_bull_stub_report(inp=inp, ledger_path=canonical_path)
        except Exception as exc:
            _emit(f"[init] WARN {cand.ticker}: bull-stub failed: {exc!r}")
            bull_md = Path(str(canonical_path).replace(".yml", ".md"))

        # Run-dir traceability copy
        _write_yaml(shell_dir / f"{cand.ticker}.yml", ledger_dict)

        # ATR (best-effort; tolerated to fail)
        try:
            atr14 = float(atr_compute.compute_from_ticker(cand.ticker, period=14).output["atr"])
        except Exception:
            atr14 = max(0.01, (float(cand.pivot_price) - float(cand.stop_price)) / 2.0)

        industry = industry_fn(cand.ticker)

        # Pull next-earnings out of screener evidence if available
        next_earnings: Optional[str] = None
        for chk in screen_result.checks:
            if chk.check == "earnings_blackout":
                evidence = getattr(chk, "evidence", None) or {}
                next_earnings = evidence.get("next_earnings_date")
                break

        envelope_path = envelope_dir / f"{cand.ticker}.json"
        invocation = {
            "ticker": cand.ticker,
            "subagent": "trade-skeptic",
            "prompt": _build_skeptic_prompt(
                ledger_path=str(canonical_path),
                bull_md_path=str(bull_md),
            ),
            "envelope_path": str(envelope_path),
            "ledger_path": str(canonical_path),
            "bull_md_path": str(bull_md),
            # extras the post_skeptic phase reads back when building panel input
            "atr_14": atr14,
            "next_earnings_date": next_earnings,
            "sector_industry": industry,
            "sector_etf": cand.sector_etf,
            "setup_type": cand.setup_type,
            "setup_grade": cand.setup_grade,
            "pivot_price": float(cand.pivot_price),
            "limit_price": float(cand.limit_price),
            "stop_price": float(cand.stop_price),
            "target_price": float(cand.target_price) if cand.target_price else None,
            "shares": int(cand.shares),
            "screener_summary": {
                "blocked": False,
                "corrected_sector_etf": screen_result.corrected_sector_etf,
                "blocking_checks": list(screen_result.blocking_checks),
            },
            # Schema-1.3 overlay. trade-skeptic reads this from its envelope
            # as factual context only — never as a gate (spec § 3.4).
            "market_temperature": market_temperature_block,
        }
        skeptic_invocations.append(invocation)

    _write_yaml(run_dir / "03_skeptic_invocations.yml", {
        "invocations": skeptic_invocations,
        "envelope_dir": str(envelope_dir),
    })

    state.write_run_status(
        run_dir, phase="init", completed_at=_now(),
        candidates_in=len(scanner_cands),
        candidates_surviving_screener=len(surviving),
        skeptic_invocations_written=len(skeptic_invocations),
    )
    _emit(f"PHASE_INIT_OK run_dir={run_dir} invocations={len(skeptic_invocations)}")
    return 0


# ---------------------------------------------------------------------------
# Phase post_skeptic
# ---------------------------------------------------------------------------


def phase_post_skeptic(run_dir: Path) -> int:
    """Read skeptic envelopes → emit panel invocations.

    Fails loud (MissingEnvelopeError) if ANY skeptic envelope is absent.
    """
    _require_prerequisite_phase(run_dir, expected="init")

    invocations_doc = _read_yaml(run_dir / "03_skeptic_invocations.yml") or {}
    invocations = invocations_doc.get("invocations") or []

    if not invocations:
        # Clean no-op when nothing surfaced from init
        _write_yaml(run_dir / "05_panel_invocations.yml", {"invocations": []})
        state.write_run_status(
            run_dir, phase="post_skeptic", completed_at=_now(),
            skeptic_envelopes_read=0, panel_invocations_written=0,
        )
        _emit("PHASE_POST_SKEPTIC_OK panel_invocations=0")
        return 0

    # Fail-loud on missing envelopes
    missing: list[tuple[str, str]] = []
    skeptic_envelopes: dict[str, dict[str, Any]] = {}
    for inv in invocations:
        env_path = Path(inv["envelope_path"])
        if not env_path.exists():
            missing.append((inv["ticker"], str(env_path)))
            continue
        try:
            with open(env_path, encoding="utf-8") as fh:
                skeptic_envelopes[inv["ticker"]] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise MissingEnvelopeError(
                f"skeptic envelope at {env_path} is not valid JSON: {exc}"
            ) from exc

    if missing:
        raise MissingEnvelopeError(
            f"Expected {len(invocations)} skeptic envelopes; missing "
            f"{len(missing)}: {missing}. The LLM did not fire trade-skeptic "
            f"for these tickers or did not save the envelope."
        )

    # Load portfolio + account for portfolio_context
    cands_doc = _read_yaml(run_dir / "00_candidates.yml") or {}
    account = cands_doc.get("account") or {}
    net_liq = float(account.get("net_liquidation") or 0.0)
    cash = float(account.get("cash") or 0.0)
    regime_class = account.get("regime_class") or ""
    positions = state.load_positions_json().get("positions", [])
    # Drop stale closed entries defensively (pre-fix ledgers may still have them)
    existing = [p for p in positions if p.get("stage") != "closed"]

    cash_buffer_pct = round(cash / net_liq, 4) if net_liq else 0.0

    panel_invocations: list[dict[str, Any]] = []
    panel_root = run_dir / "06_panel_envelopes"
    panel_root.mkdir(parents=True, exist_ok=True)
    for inv in invocations:
        ticker = inv["ticker"]
        panel_call_id = f"{run_dir.name}__{ticker}"
        panel_input = build_panel_input_dict(
            market_temperature=inv.get("market_temperature"),
            ticker=ticker,
            setup_type=inv["setup_type"],
            setup_grade=inv["setup_grade"],
            pivot_price=float(inv["pivot_price"]),
            stop_price=float(inv["stop_price"]),
            sector_etf=inv["sector_etf"],
            sector_industry=inv.get("sector_industry") or "",
            shares=int(inv["shares"]),
            source="quant_scanner",
            ledger_path=inv["ledger_path"],
            bull_report_path=inv["bull_md_path"],
            bear_report_path=inv["ledger_path"].replace(".yml", "-bear.md"),
            regime_class=regime_class,
            atr_14=inv.get("atr_14"),
            next_earnings_date=inv.get("next_earnings_date"),
            screener_summary=inv["screener_summary"],
            existing_positions=[
                {"ticker": p["ticker"], "sector_etf": p.get("sector")}
                for p in existing
            ],
            net_liquidation=net_liq,
            cash_buffer_pct=cash_buffer_pct,
            panel_call_id=panel_call_id,
            panel_firing_date=cands_doc.get("loaded_at", "")[:10] or _now()[:10],
            shadow_mode=True,
        )
        critics = _derive_critics_list(panel_input)
        env_dir = panel_root / ticker
        env_dir.mkdir(parents=True, exist_ok=True)
        # Per-critic envelope shaping. Spec § 3.4: market_temperature lands
        # in the macro-skeptic envelope ONLY; every other critic sees the
        # field stripped. build_critic_envelope is the single source of
        # truth for this routing rule.
        critic_envelopes = {
            critic: build_critic_envelope(critic, panel_input)
            for critic in critics
        }
        panel_invocations.append({
            "ticker": ticker,
            "panel_input": panel_input,
            "critic_envelopes": critic_envelopes,
            "critics_to_fire": critics,
            "envelope_dir": str(env_dir),
            "skeptic_envelope": skeptic_envelopes.get(ticker),
        })

    _write_yaml(run_dir / "05_panel_invocations.yml", {"invocations": panel_invocations})

    state.write_run_status(
        run_dir, phase="post_skeptic", completed_at=_now(),
        skeptic_envelopes_read=len(invocations),
        panel_invocations_written=len(panel_invocations),
    )
    _emit(f"PHASE_POST_SKEPTIC_OK panel_invocations={len(panel_invocations)}")
    return 0


# ---------------------------------------------------------------------------
# Phase post_panel
# ---------------------------------------------------------------------------


def phase_post_panel(
    run_dir: Path,
    *,
    shadow_mode: bool = True,
    dry_run: bool = False,
    place_fn=None,
    client_factory=None,
) -> int:
    """Read panel envelopes → aggregate → place trades.

    Args:
        shadow_mode: per Phase 3 v1 contract — verdicts logged but
            sizing_multiplier NOT applied. Flip to ``False`` for
            Phase 3 v2 (~2026-06-10).
        dry_run: when True, runs the full aggregate → size → candidate-build
            → validate path but passes ``dry_run=True`` to ``place_fn`` so no
            broker order is placed and no submitted ledger / positions.json is
            written (placement rows report ``status="dry_run"``). Used for the
            true-shadow validation window — exercise the open-session path
            without trading. Independent of ``shadow_mode`` (which is about
            sizing, not placement).
        place_fn: test seam — override for :func:`pipeline.place_candidate`.
        client_factory: test seam — override for :class:`TigerClient`.
    """
    _require_prerequisite_phase(run_dir, expected="post_skeptic")
    place_fn = place_fn or place_candidate
    client_factory = client_factory or TigerClient

    invocations_doc = _read_yaml(run_dir / "05_panel_invocations.yml") or {}
    invocations = invocations_doc.get("invocations") or []

    if not invocations:
        _write_yaml(run_dir / "07_placement_results.yml", {"results": [], "n_placed": 0})
        state.write_run_status(
            run_dir, phase="post_panel", completed_at=_now(),
            panel_envelopes_read=0, candidates_placed=0,
            placement_results_path=str(run_dir / "07_placement_results.yml"),
        )
        _emit("PHASE_POST_PANEL_OK placed=0")
        return 0

    invocations_doc_cands = _read_yaml(run_dir / "00_candidates.yml") or {}
    today = date.fromisoformat(
        invocations_doc_cands.get("loaded_at", _now())[:10]
    )

    client = client_factory()
    placement_results: list[dict[str, Any]] = []
    envelopes_read_total = 0

    for inv in invocations:
        ticker = inv["ticker"]
        ticker_dir = Path(inv["envelope_dir"])
        critics_to_fire = inv["critics_to_fire"]
        panel_input = inv["panel_input"]
        panel_call_id = panel_input["panel_metadata"]["panel_call_id"]

        # Load envelopes
        votes: list[CriticVote] = []
        missing_critics: list[str] = []
        for critic_name in critics_to_fire:
            env_path = ticker_dir / f"{critic_name}.json"
            if not env_path.exists():
                missing_critics.append(critic_name)
                continue
            try:
                with open(env_path, encoding="utf-8") as fh:
                    votes.append(CriticVote.from_dict(json.load(fh)))
                envelopes_read_total += 1
            except (json.JSONDecodeError, KeyError) as exc:
                _emit(f"WARN {ticker}: malformed vote at {env_path}: {exc!r}")

        if not votes:
            # ALL critics missing for this ticker — failure surface.
            raise MissingEnvelopeError(
                f"All {len(critics_to_fire)} critics missing for {ticker}: "
                f"{missing_critics}. LLM did not fire any panel critics."
            )
        if missing_critics:
            _emit(f"WARN ticker={ticker} missing_critics={missing_critics}")

        # Aggregate + persist
        verdict = aggregate_panel(
            votes,
            ticker=ticker,
            panel_call_id=panel_call_id,
            shadow_mode=shadow_mode,
        )
        for v in votes:
            save_critic_vote(v, ledger_date=today)
        save_panel_verdict(verdict, ledger_date=today)

        # Decide placement
        if verdict.action == "defer":
            append_calibration_log(
                verdict, placement_status="manual_defer",
                placement_shares=None, ledger_date=today,
            )
            placement_results.append({
                "ticker": ticker,
                "status": "defer",
                "placed": False,
                "verdict_action": verdict.action,
                "sizing_multiplier": verdict.sizing_multiplier,
                "shares": 0,
                "broker_order_id": None,
                "ledger_path": None,
                "reason": verdict.rationale[:200],
            })
            _emit(
                f"PLACE: {ticker} defer 0 "
                f"(structural_risk×{verdict.n_critics_structural_risk})"
            )
            continue

        # Compute shares
        base_shares = int(panel_input["candidate"]["shares"])
        if shadow_mode:
            shares = base_shares
        else:
            shares = max(1, int(base_shares * verdict.sizing_multiplier))

        cand_input = CandidateInput(
            ticker=ticker,
            setup_type=panel_input["candidate"]["setup_type"],
            setup_grade=panel_input["candidate"]["setup_grade"],
            pivot_price=float(panel_input["candidate"]["pivot_price"]),
            limit_price=round(float(panel_input["candidate"]["pivot_price"]) * 1.001, 2),
            stop_price=float(panel_input["candidate"]["stop_price"]),
            target_price=round(
                float(panel_input["candidate"]["pivot_price"])
                + 2 * (
                    float(panel_input["candidate"]["pivot_price"])
                    - float(panel_input["candidate"]["stop_price"])
                ),
                2,
            ),
            shares=shares,
            sector_etf=panel_input["candidate"]["sector_etf"],
            reasoning_trace=[],
            sizing_multiplier=verdict.sizing_multiplier,
            panel_verdict=verdict.to_dict(),
        )

        try:
            result = place_fn(
                cand_input,
                client=client,
                dry_run=dry_run,
                apply_panel_sizing=False,    # shares already finalized above
                auto_paper_run_dir=str(run_dir),
            )
        except Exception as exc:
            placement_results.append({
                "ticker": ticker,
                "status": "error",
                "placed": False,
                "verdict_action": verdict.action,
                "sizing_multiplier": verdict.sizing_multiplier,
                "shares": shares,
                "broker_order_id": None,
                "ledger_path": None,
                "reason": f"place_candidate raised: {exc!r}",
            })
            _emit(f"PLACE: {ticker} error {exc!r}")
            append_calibration_log(
                verdict, placement_status="error",
                placement_shares=None, ledger_date=today,
            )
            continue

        placement_results.append({
            "ticker": ticker,
            "status": result.status,
            "placed": result.status == "placed",
            "verdict_action": verdict.action,
            "sizing_multiplier": verdict.sizing_multiplier,
            "shares": shares,
            "broker_order_id": result.broker_order_id,
            "ledger_path": result.ledger_path,
            "reason": result.reason,
        })
        _emit(
            f"PLACE: {ticker} {result.status} {shares} "
            f"(verdict={verdict.action}, order={result.broker_order_id})"
        )
        append_calibration_log(
            verdict,
            placement_status=result.status or "unknown",
            placement_shares=shares if result.status == "placed" else None,
            ledger_date=today,
        )

    n_placed = sum(1 for r in placement_results if r["placed"])
    _write_yaml(run_dir / "07_placement_results.yml", {
        "results": placement_results,
        "n_placed": n_placed,
    })

    state.write_run_status(
        run_dir, phase="post_panel", completed_at=_now(),
        panel_envelopes_read=envelopes_read_total,
        candidates_placed=n_placed,
        placement_results_path=str(run_dir / "07_placement_results.yml"),
    )
    _emit(f"PHASE_POST_PANEL_OK placed={n_placed}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.auto_paper.run_entry",
        description="3-phase auto-paper orchestrator (Python/LLM boundary).",
    )
    parser.add_argument(
        "--phase", required=True,
        choices=["init", "post_skeptic", "post_panel"],
    )
    parser.add_argument(
        "--run-dir", type=Path, default=None,
        help="Override run directory. Defaults to latest for post_* phases, "
             "new dir for init.",
    )
    parser.add_argument(
        "--shadow-mode", action="store_true", default=True,
        help="Phase 3 v1 default. Verdicts logged, sizing not applied.",
    )
    parser.add_argument(
        "--apply-panel-sizing", action="store_true",
        help="Phase 3 v2 (post-2026-06-10). Sizing multiplier IS applied.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="post_panel only: run the full aggregate→size→validate path but "
             "do NOT place broker orders or write submitted ledgers "
             "(placement rows report status=dry_run). True-shadow validation.",
    )
    args = parser.parse_args(argv)
    shadow = not args.apply_panel_sizing

    if args.phase == "init":
        run_dir = args.run_dir or _new_run_dir()
        return phase_init(run_dir)
    elif args.phase == "post_skeptic":
        run_dir = args.run_dir or _latest_run_dir()
        return phase_post_skeptic(run_dir)
    elif args.phase == "post_panel":
        run_dir = args.run_dir or _latest_run_dir()
        return phase_post_panel(run_dir, shadow_mode=shadow, dry_run=args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
