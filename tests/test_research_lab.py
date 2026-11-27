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


@pytest.mark.unit
def test_deflated_sharpe_raises_the_bar_with_more_trials():
    rng = np.random.default_rng(3)
    modest = pd.Series(rng.normal(0.0006, 0.01, 1250))  # about Sharpe 0.95 a year
    one = harness.deflated_sharpe(modest, 1)
    many = harness.deflated_sharpe(modest, 200)
    assert one["noise_bar_per_bar"] == 0.0
    assert many["noise_bar_per_bar"] > 0
    assert many["dsr"] < one["dsr"]
    noise = harness.deflated_sharpe(pd.Series(rng.normal(0, 0.01, 1250)), 20)
    assert noise["dsr"] < 0.95
    strong = harness.deflated_sharpe(pd.Series(rng.normal(0.003, 0.01, 1250)), 20)
    assert strong["dsr"] > 0.95


@pytest.mark.unit
def test_longest_underwater_counts_calendar_days_from_the_peak():
    idx = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-10", "2020-02-01", "2020-03-01"])
    equity = pd.Series([1.0, 1.2, 1.1, 1.15, 1.3], index=idx)
    # Peak on Jan 2, back above it on Mar 1.
    assert harness.longest_underwater_days(equity) == (idx[4] - idx[1]).days
    still_under = pd.Series([1.0, 1.2, 1.1, 1.0, 0.9], index=idx)
    assert harness.longest_underwater_days(still_under) == (idx[4] - idx[1]).days


@pytest.mark.unit
def test_years_and_regimes_are_reported_and_a_one_year_wonder_fails():
    # Strategy earns everything in its first year and bleeds afterwards.
    n = 1500
    bars = _bars(np.full(n, 0.0005))

    def first_year_only(b):
        return pd.Series(1.0, index=b.index)

    result = harness.evaluate(_strategy(first_year_only, cost_bps=0), bars, n_tested=1)
    assert result["years"] and result["years"][0]["year"] == 2000
    assert set(result["test_regimes"]) <= {"bull", "bear", "sideways"}

    rets = np.where(np.arange(n) < 260, 0.01, -0.0004)
    lucky = harness.evaluate(_strategy(first_year_only, cost_bps=0), _bars(rets), n_tested=1)
    assert any("years" in r for r in lucky["segments"][1]["reasons"])


@pytest.mark.unit
def test_each_trade_pays_its_own_exit_cost():
    bars = _bars([0.01, 0.02, 0.0, 0.0])
    target = pd.Series([1.0, 1.0, 0.0, 0.0, 0.0], index=bars.index)
    frame = harness.net_returns(bars["Close"], target, cost_bps=10)
    (trade,) = harness.trade_returns(frame)
    # entry cost, +1%, +2%, exit cost: all inside the one trade
    assert trade == pytest.approx((1 + 0.01 - 0.001) * 1.02 * (1 - 0.001) - 1)
    # a flip pays exit on the old side and entry on the new one, once each
    flip = harness.net_returns(
        bars["Close"], pd.Series([1.0, -1.0, -1.0, 0.0, 0.0], index=bars.index), cost_bps=10
    )
    first, second = harness.trade_returns(flip)
    assert first == pytest.approx((1 + 0.01 - 0.001) * (1 - 0.001) - 1)
    assert second == pytest.approx((1 - 0.02 - 0.001) * 1.0 * (1 - 0.001) - 1)


@pytest.mark.unit
def test_sealed_holdout_bars_never_leave_a_normal_run():
    bars = _bars(np.random.default_rng(2).normal(0, 0.01, 1000))
    strat = _strategy(lambda b: (b["Close"] > b["Close"].rolling(10).mean()).astype(float))
    normal = harness.evaluate(strat, bars, n_tested=1)
    _, holdout_start = harness.split_points(len(bars), harness.Rules())
    assert len(normal["frame"]) == holdout_start
    assert len(harness.evaluate(strat, bars, n_tested=1, final=True)["frame"]) == len(bars)


@pytest.mark.unit
def test_final_cannot_skip_the_ledger():
    import run

    with pytest.raises(SystemExit, match="must be logged"):
        run.main(["test", "spy_sma200_trend", "--final", "--no-log"])
