"""Research lab harness and ledger: costs, splits, pass/fail rules and memory."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research" / "lab"))

import harness  # noqa: E402
import ledger  # noqa: E402


def _bars(returns):
    idx = pd.bdate_range("2000-01-03", periods=len(returns) + 1)
    close = 100 * np.cumprod(np.r_[1.0, 1 + np.asarray(returns, dtype=float)])
    return pd.DataFrame({"Close": close}, index=idx)


def _strategy(signal, cost_bps=0.0):
    return harness.Strategy(
        name="t", hypothesis="h", disproof="d", symbol="X", signal=signal, cost_bps=cost_bps
    )


@pytest.mark.unit
def test_position_is_held_from_the_next_bar_and_costs_are_charged_on_changes():
    bars = _bars([0.01, 0.02, -0.01, 0.03])
    target = pd.Series([1.0, 1.0, 0.0, 0.0, 0.0], index=bars.index)
    frame = harness.net_returns(bars["Close"], target, cost_bps=10)
    # Bought at the close of bar 0, so bar 0's own move is not earned.
    assert frame["held"].tolist() == [0.0, 1.0, 1.0, 0.0, 0.0]
    assert frame["net"].iloc[1] == pytest.approx(0.01 - 0.001)
    assert frame["net"].iloc[2] == pytest.approx(0.02)
    assert frame["net"].iloc[3] == pytest.approx(-0.001)  # flat, pays the exit


@pytest.mark.unit
def test_trades_are_runs_on_the_same_side():
    frame = pd.DataFrame(
        {
            "held": [0, 1, 1, 0, -1, -1, 1],
            "net": [0, 0.01, 0.01, 0, 0.02, -0.01, 0.05],
        },
        dtype=float,
    )
    trades = harness.trade_returns(frame)
    assert len(trades) == 3
    assert trades[0] == pytest.approx(1.01 * 1.01 - 1)
    assert trades[1] == pytest.approx(1.02 * 0.99 - 1)
    assert trades[2] == pytest.approx(0.05)


@pytest.mark.unit
def test_significance_bar_rises_with_every_strategy_tested():
    assert harness.required_t(1, 0.05) == pytest.approx(1.645, abs=0.01)
    assert harness.required_t(10, 0.05) == pytest.approx(2.576, abs=0.01)
    assert harness.required_t(50, 0.05) > harness.required_t(10, 0.05)


@pytest.mark.unit
def test_lookahead_is_caught():
    bars = _bars(np.random.default_rng(0).normal(0, 0.01, 300))

    def cheat(b):  # trades on tomorrow's move
        return np.sign(b["Close"].pct_change().shift(-1)).fillna(0.0)

    with pytest.raises(ValueError, match="future"):
        harness.check_causal(cheat, bars)
    harness.check_causal(lambda b: (b["Close"] > b["Close"].rolling(20).mean()).astype(float), bars)


@pytest.mark.unit
def test_a_real_edge_passes_and_the_holdout_stays_sealed():
    # Up on even bars, down on odd ones: holding only into even bars is a true, causal edge.
    n = 1500
    rets = np.where(np.arange(1, n + 1) % 2 == 0, 0.004, -0.003)
    bars = _bars(rets)

    def even_bars(b):
        return pd.Series(((np.arange(len(b)) + 1) % 2 == 0).astype(float), index=b.index)

    result = harness.evaluate(_strategy(even_bars, cost_bps=1), bars, n_tested=10)
    assert result["verdict"] == "pass", result["segments"]
    assert [s["segment"] for s in result["segments"]] == ["train", "test"]
    final = harness.evaluate(_strategy(even_bars, cost_bps=1), bars, n_tested=10, final=True)
    assert final["segments"][-1]["segment"] == "holdout"


@pytest.mark.unit
def test_noise_fails_with_reasons():
    bars = _bars(np.random.default_rng(1).normal(0.0003, 0.01, 1500))
    result = harness.evaluate(
        _strategy(lambda b: (b["Close"] > b["Close"].rolling(5).mean()).astype(float), cost_bps=5),
        bars,
        n_tested=10,
    )
    assert result["verdict"] == "fail"
    assert any(seg["reasons"] for seg in result["segments"])


@pytest.mark.unit
def test_too_few_trades_fails():
    bars = _bars(np.full(1500, 0.001))
    result = harness.evaluate(_strategy(lambda b: pd.Series(1.0, index=b.index)), bars, n_tested=1)
    test = result["segments"][1]
    assert any("trades" in r for r in test["reasons"])


@pytest.mark.unit
def test_ledger_remembers_failures_and_flags_repeats(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.append(
        {
            "name": "orb",
            "verdict": "fail",
            "tags": ["intraday", "breakout", "opening-range"],
            "hypothesis": "trade the first close beyond the opening range",
            "why": "noise",
        },
        path,
    )
    ledger.append(
        {
            "name": "rsi2",
            "verdict": "fail",
            "final": True,
            "tags": ["daily", "rsi"],
            "hypothesis": "buy short dips in uptrends",
            "why": "costs",
        },
        path,
    )
    records = ledger.load(path)
    assert ledger.strategies_tested(records) == 2
    assert ledger.holdout_used("rsi2", records)
    assert not ledger.holdout_used("orb", records)
    hits = ledger.similar(("intraday", "breakout", "opening-range"), "new words entirely", records)
    assert [r["name"] for r in hits] == ["orb"]
    # Generic tags alone do not make two ideas the same.
    assert ledger.similar(("daily", "etf"), "something unrelated", records) == []
    assert "do not repeat" not in ledger.brief(records)
    assert "orb [FAIL]" in ledger.brief(records)


@pytest.mark.unit
def test_seed_ledger_holds_every_past_test():
    records = ledger.load()
    names = {r["name"] for r in records}
    assert {"opening_range_breakout", "harry_sharp_bias", "tradingagents_free_model"} <= names
    assert all(r["verdict"] in {"pass", "fail"} for r in records)
