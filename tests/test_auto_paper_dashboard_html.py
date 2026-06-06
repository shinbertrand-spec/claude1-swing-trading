"""Tests for the paper-auto HTML dashboard renderer.

Pure render tests -- all inputs are injected stubs, so no Tiger / network /
filesystem dependency (beyond the tmp_path write). Mirrors the data shapes
returned by compute_performance / compute_open_pnl / calibration_analysis.
"""
from types import SimpleNamespace

from tools.auto_paper import dashboard_html as dh


def _report(*, n_open=2, n_submitted=0, n_realized=0, comparisons=None, notes=None):
    return SimpleNamespace(
        n_open=n_open, n_submitted=n_submitted, n_realized=n_realized,
        comparisons=comparisons if comparisons is not None else [
            SimpleNamespace(setup="connors_rsi2", n_trades=0, realized_sharpe=None,
                            backtest_sharpe=1.01, status="no_data"),
            SimpleNamespace(setup="xs_short_term_reversal", n_trades=5,
                            realized_sharpe=1.42, backtest_sharpe=1.6, status="warn"),
        ],
        notes=notes if notes is not None else ["a note", "another note"],
    )


def _open_pnl(*, total=2742.0, mv=121462.0, positions=None, error=None, missing=None):
    if positions is None:
        positions = [
            {"ticker": "GO", "entry_price": 8.03, "current_price": 8.48,
             "unrealized_pnl_usd": 1348.0},
            {"ticker": "VAL", "entry_price": 92.31, "current_price": 90.29,
             "unrealized_pnl_usd": -408.0},
        ]
    return {
        "total_unrealized_pnl_usd": total, "total_market_value_usd": mv,
        "by_position": positions, "missing_quotes": missing or [], "error": error,
    }


def _calib(*, joined=1, ready=False, discrimination="INSUFFICIENT DATA"):
    return SimpleNamespace(
        n_joined=joined, ready_to_flip=ready, discrimination=discrimination,
        n_verdict_records=62, n_outcome_records=1,
    )


def test_render_contains_core_sections():
    out = dh.render_html(_report(), _open_pnl(), _calib())
    assert out.startswith("<!DOCTYPE html>")
    assert "Paper-Auto Dashboard" in out
    # cards
    assert "Open P&amp;L" in out or "Open P&L" in out
    # positions present
    assert "GO" in out and "VAL" in out
    # setup table
    assert "connors_rsi2" in out and "xs_short_term_reversal" in out
    # calibration gauge + shadow badge
    assert "of 20 joined" in out
    assert "SHADOW" in out
    # well-formed-ish: balanced html/body close
    assert out.rstrip().endswith("</html>")


def test_ready_to_flip_renders_live_badge():
    out = dh.render_html(_report(), _open_pnl(), _calib(joined=22, ready=True))
    assert "READY TO FLIP" in out
    assert "LIVE" in out


def test_empty_positions_no_crash():
    out = dh.render_html(_report(n_open=0), _open_pnl(positions=[], total=0.0, mv=0.0),
                         _calib())
    assert "no open positions" in out
    # zero market value must not divide-by-zero
    assert "<!DOCTYPE html>" in out


def test_error_state_shows_banner():
    out = dh.render_html(_report(), _open_pnl(error="HTTP 503"), _calib())
    assert "Tiger unreachable" in out
    assert "HTTP 503" in out


def test_missing_quotes_banner():
    out = dh.render_html(_report(), _open_pnl(missing=["ABC", "XYZ"]), _calib())
    assert "Missing broker quotes" in out
    assert "ABC" in out and "XYZ" in out


def test_negative_pnl_color_and_sign():
    out = dh.render_html(_report(), _open_pnl(total=-500.0), _calib())
    assert "-$500" in out
    assert dh._RED in out  # loser styling present


def test_write_dashboard_writes_file(tmp_path):
    out_path = tmp_path / "dash.html"
    written = dh.write_dashboard(out_path, report=_report(), open_pnl=_open_pnl(),
                                 calib=_calib())
    assert written == out_path
    assert out_path.exists()
    txt = out_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in txt and "Paper-Auto Dashboard" in txt


def test_html_escapes_injected_strings():
    rep = _report(notes=["<script>evil()</script>"])
    out = dh.render_html(rep, _open_pnl(), _calib())
    assert "<script>evil()</script>" not in out
    assert "&lt;script&gt;" in out
