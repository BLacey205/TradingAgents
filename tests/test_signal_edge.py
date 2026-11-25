"""Signal-edge harness: statistics, directional test, permutation test, loading."""

import json
import random

import numpy as np
import pandas as pd
import pytest

from tradingagents.risk_gate import RiskConfig
from tradingagents.signal_edge import (
    binom_sf,
    decisions_needed,
    directional_edge,
    evaluate_signal_edge,
    load_decisions,
    render_edge_report,
    spread_permutation_test,
)
from tradingagents.spread_backtest import SpreadSimConfig

CFG = RiskConfig(min_iv_rank=0.0, min_credit=0.01)
SIM = SpreadSimConfig(min_rank_history=10)
HORIZON = 20


def _closes(n=900, seed=11):
    rng = np.random.default_rng(seed)
    px = 50 * np.exp(np.cumsum(rng.normal(0.0, 0.025, n)))
    return pd.Series(px, index=pd.bdate_range("2021-01-04", periods=n))


def _signals(closes, accuracy, seed=3, step=5):
    """Weekly ratings that call the next 20-bar direction right ``accuracy`` of the time."""
    rng = random.Random(seed)
    fwd = closes.shift(-HORIZON) / closes - 1
    out = []
    for ts in closes.index[300:-HORIZON - 1:step]:
        up = fwd.loc[ts] > 0
        right = rng.random() < accuracy
        out.append((ts.date().isoformat(), "Buy" if up == right else "Sell"))
    return out


@pytest.mark.unit
def test_binom_sf_known_values():
    assert binom_sf(8, 10, 0.5) == pytest.approx(0.0546875)
    assert binom_sf(0, 10, 0.5) == 1.0
    assert binom_sf(11, 10, 0.5) == 0.0
    assert binom_sf(5, 5, 1.0) == 1.0
    assert binom_sf(300, 500, 0.5) == pytest.approx(0.0, abs=1e-5)


@pytest.mark.unit
def test_decisions_needed_matches_textbook():
    assert 140 <= decisions_needed(0.60) <= 165  # ~152 for 60% vs 50%
    assert decisions_needed(0.50) is None
    assert decisions_needed(0.70) < decisions_needed(0.60)


@pytest.mark.unit
def test_informed_signals_show_directional_edge():
    closes = _closes()
    d = directional_edge(closes, _signals(closes, accuracy=0.85), HORIZON)
    bull, bear = d["bullish"], d["bearish"]
    assert bull["hit_rate"] > 0.7 and bull["p_value"] < 0.01
    assert bear["hit_rate"] > 0.7 and bear["p_value"] < 0.01
    assert d["overlap"] is True  # weekly decisions, 20-bar horizon


@pytest.mark.unit
def test_random_signals_show_no_directional_edge():
    closes = _closes()
    d = directional_edge(closes, _signals(closes, accuracy=0.5), HORIZON)
    assert d["bullish"]["p_value"] > 0.05 and d["bearish"]["p_value"] > 0.05


@pytest.mark.unit
def test_holds_and_unscorable_calls_are_ignored():
    closes = _closes(n=400)
    d = directional_edge(closes, [("2021-06-01", "Hold")], HORIZON)
    assert d["n_calls"] == 0 and d["bullish"] is None
    late = [(closes.index[-1].date().isoformat(), "Buy")]  # no forward window left
    assert directional_edge(closes, late, HORIZON)["bullish"] is None


@pytest.mark.unit
def test_permutation_test_separates_informed_from_random():
    closes = _closes()
    informed = spread_permutation_test("T", closes, _signals(closes, 0.9), [], CFG, SIM, n_perm=40, seed=1)
    random_ = spread_permutation_test("T", closes, _signals(closes, 0.5, seed=9), [], CFG, SIM, n_perm=40, seed=1)
    assert informed["actual_pnl"] > informed["null_mean"]
    assert informed["p_value"] < random_["p_value"]
    assert informed["p_value"] <= 0.1
    assert random_["p_value"] > 0.05


@pytest.mark.unit
def test_permutation_test_is_reproducible():
    closes = _closes(n=600)
    sigs = _signals(closes, 0.7)[:20]
    a = spread_permutation_test("T", closes, sigs, [], CFG, SIM, n_perm=10, seed=5)
    b = spread_permutation_test("T", closes, sigs, [], CFG, SIM, n_perm=10, seed=5)
    assert a == b


@pytest.mark.unit
def test_load_decisions_keeps_latest_and_drops_errors(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"ticker": "NVDA"}))
    rows = [
        {"trade_date": "2025-01-06", "rating": "Buy", "error": None},
        {"trade_date": "2025-01-07", "rating": "Sell", "error": "boom"},
        {"trade_date": "2025-01-08", "rating": "Hold", "error": None},
        {"trade_date": "2025-01-06", "rating": "Sell", "error": None},  # later line wins
    ]
    (tmp_path / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{cut")
    ticker, signals = load_decisions(tmp_path)
    assert ticker == "NVDA"
    assert signals == [("2025-01-06", "Sell"), ("2025-01-08", "Hold")]


@pytest.mark.unit
def test_evaluate_and_render_report_with_injected_data():
    closes = _closes()
    report = evaluate_signal_edge(
        "TEST", _signals(closes, 0.8), closes=closes, earnings=[], risk_cfg=CFG, sim_cfg=SIM,
        horizon=HORIZON, n_perm=10,
    )
    text = render_edge_report(report)
    assert "# Signal edge: TEST" in text and "Bullish" in text and "Permutation p" in text
    assert "training cutoff" in text
    assert any("overlap" in n for n in report.notes)
    no_spread = evaluate_signal_edge(
        "TEST", _signals(closes, 0.8), closes=closes, earnings=[], horizon=HORIZON, n_perm=0,
    )
    assert no_spread.spread is None and "Permutation" not in render_edge_report(no_spread)
