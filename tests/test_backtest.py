"""Backtest runner: schedule, forward-return scoring, resume, and the CLI command.

A fake graph and an in-memory price loader stand in for the LLM pipeline and
yfinance, so these tests make no network or model calls.
"""

import json

import pandas as pd
import pytest
from typer.testing import CliRunner

from tradingagents import backtest as bt
from tradingagents.agents.utils.memory import TradingMemoryLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _closes(start: str, values: list[float], tz: str | None = "America/New_York") -> pd.Series:
    """Business-day close series, tz-aware like yfinance by default."""
    idx = pd.bdate_range(start=start, periods=len(values), tz=tz)
    return pd.Series(values, index=idx, dtype=float)


class FakeGraph:
    """Returns a scripted rating per date; ``None`` in the script raises."""

    def __init__(self, ratings: dict[str, str | None], stats=None):
        self.ratings = ratings
        self.calls: list[tuple[str, str, str]] = []
        self.stats = stats

    def propagate(self, ticker, trade_date, asset_type="stock"):
        self.calls.append((ticker, trade_date, asset_type))
        if self.stats is not None:
            self.stats.llm_calls += 3
            self.stats.tokens_in += 100
            self.stats.tokens_out += 10
        rating = self.ratings[trade_date]
        if rating is None:
            raise RuntimeError("provider exploded")
        return {}, rating

    def _resolve_benchmark(self, ticker):
        return "SPY"


class Stats:
    llm_calls = 0
    tokens_in = 0
    tokens_out = 0


def _loader(series_by_symbol):
    def load(symbol, start, end):
        return series_by_symbol[symbol]
    return load


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def test_weekly_schedule_repeats_on_the_start_weekday():
    # 2025-01-01 is a Wednesday: every Wednesday, so 5-bar windows never overlap.
    assert bt.trade_dates("2025-01-01", "2025-01-22", "weekly") == [
        "2025-01-01", "2025-01-08", "2025-01-15", "2025-01-22",
    ]
    # A weekend start rolls to Monday.
    assert bt.trade_dates("2025-01-04", "2025-01-13", "weekly") == ["2025-01-06", "2025-01-13"]
    assert bt.trade_dates("2025-01-04", "2025-01-05", "weekly") == []


def test_default_weekly_schedule_does_not_overlap_default_holding(tmp_path):
    dates = bt.trade_dates("2025-01-01", "2025-03-31", "weekly")
    stock = _closes("2025-01-01", [100 + i for i in range(80)])
    bench = _closes("2025-01-01", [50.0] * 80)
    runner = bt.Backtester(
        FakeGraph(dict.fromkeys(dates, "Buy")), "NVDA", tmp_path, holding_days=5,
        price_loader=_loader({"NVDA": stock, "SPY": bench}),
    )
    assert runner.run(dates).summary["overlapping_windows"] is False


def test_monthly_and_daily_schedules():
    assert bt.trade_dates("2025-01-15", "2025-03-31", "monthly") == [
        "2025-01-15", "2025-02-03", "2025-03-03",
    ]
    # Weekends are skipped.
    assert bt.trade_dates("2025-01-03", "2025-01-07", "daily") == [
        "2025-01-03", "2025-01-06", "2025-01-07",
    ]


def test_unknown_frequency_rejected():
    with pytest.raises(ValueError, match="frequency"):
        bt.trade_dates("2025-01-01", "2025-02-01", "hourly")


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------

def test_forward_return_enters_on_trade_date_close():
    closes = _closes("2025-01-06", [100, 101, 102, 103, 104, 110, 120])
    ret, days = bt.forward_return(closes, "2025-01-06", 5)
    assert ret == pytest.approx(0.10)
    assert days == 5


def test_forward_return_rolls_to_next_bar_when_date_is_not_trading():
    # 2025-01-04 is a Saturday: enter at Monday's close.
    closes = _closes("2025-01-06", [100, 105, 110], tz=None)
    ret, _ = bt.forward_return(closes, "2025-01-04", 1)
    assert ret == pytest.approx(0.05)


def test_forward_return_none_when_window_incomplete():
    closes = _closes("2025-01-06", [100, 101, 102])
    assert bt.forward_return(closes, "2025-01-06", 5) == (None, None)
    assert bt.forward_return(pd.Series(dtype=float), "2025-01-06", 1) == (None, None)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_rating_exposure_and_long_only():
    assert bt.rating_exposure("Sell") == -1.0
    assert bt.rating_exposure("Underweight", long_only=True) == 0.0
    assert bt.rating_exposure("Buy", long_only=True) == 1.0
    assert bt.rating_exposure(None) is None


def _run(tmp_path, ratings, stock, bench, **kwargs):
    stats = Stats()
    graph = FakeGraph(ratings, stats=stats)
    runner = bt.Backtester(
        graph, "NVDA", tmp_path, holding_days=1,
        price_loader=_loader({"NVDA": stock, "SPY": bench}),
        stats=stats, **kwargs,
    )
    return graph, runner.run(list(ratings))


def test_backtest_scores_calls_against_forward_returns(tmp_path):
    # Stock: +10%, -10%, +5% over the three one-bar windows; benchmark flat.
    stock = _closes("2025-01-06", [100, 110, 99, 103.95])
    bench = _closes("2025-01-06", [50, 50, 50, 50])
    ratings = {"2025-01-06": "Buy", "2025-01-07": "Sell", "2025-01-08": "Hold"}

    graph, result = _run(tmp_path, ratings, stock, bench)
    s = result.summary

    assert [c[1] for c in graph.calls] == list(ratings)
    by_date = {r.trade_date: r for r in result.records}
    assert by_date["2025-01-06"].strategy_return == pytest.approx(0.10)
    assert by_date["2025-01-07"].strategy_return == pytest.approx(0.10)  # short a -10% move
    assert by_date["2025-01-08"].strategy_return == 0.0

    assert s["decisions"] == 3 and s["scored"] == 3 and s["errors"] == 0
    assert s["active_calls"] == 2
    assert s["hit_rate"] == 1.0
    assert s["cumulative_strategy_return"] == pytest.approx(1.1 * 1.1 - 1)
    assert s["cumulative_benchmark_return"] == pytest.approx(0.0)
    assert s["max_drawdown"] == pytest.approx(0.0)
    assert s["rating_counts"] == {"Buy": 1, "Sell": 1, "Hold": 1}
    assert s["llm_calls"] == 9 and s["tokens_in"] == 300 and s["tokens_out"] == 30
    assert s["overlapping_windows"] is False


def test_long_only_turns_sell_into_flat(tmp_path):
    stock = _closes("2025-01-06", [100, 90, 81])
    bench = _closes("2025-01-06", [50, 50, 50])
    _, result = _run(
        tmp_path, {"2025-01-06": "Sell", "2025-01-07": "Sell"}, stock, bench, long_only=True,
    )
    assert result.summary["active_calls"] == 0
    assert result.summary["cumulative_strategy_return"] == pytest.approx(0.0)


def test_drawdown_and_alpha(tmp_path):
    stock = _closes("2025-01-06", [100, 110, 88, 88])
    bench = _closes("2025-01-06", [100, 105, 105, 105])
    _, result = _run(
        tmp_path, {"2025-01-06": "Buy", "2025-01-07": "Buy", "2025-01-08": "Buy"}, stock, bench,
    )
    s = result.summary
    # Equity 1.10 -> 0.88 -> 0.88: a 20% drawdown from the peak.
    assert s["max_drawdown"] == pytest.approx(-0.20)
    first = result.records[0]
    assert first.alpha == pytest.approx(0.10 - 0.05)
    # Beat the benchmark only in the first window; a flat window is not a hit.
    assert s["alpha_hit_rate"] == pytest.approx(1 / 3)


def test_unscorable_recent_dates_are_counted_not_scored(tmp_path):
    stock = _closes("2025-01-06", [100, 101])
    bench = _closes("2025-01-06", [50, 50])
    _, result = _run(tmp_path, {"2025-01-06": "Buy", "2025-01-07": "Buy"}, stock, bench)
    assert result.summary["scored"] == 1
    assert result.summary["unscored"] == 1


def test_overlapping_windows_flagged(tmp_path):
    stock = _closes("2025-01-06", list(range(100, 120)))
    bench = _closes("2025-01-06", [50] * 20)
    runner = bt.Backtester(
        FakeGraph({"2025-01-06": "Buy", "2025-01-07": "Buy"}), "NVDA", tmp_path,
        holding_days=5, price_loader=_loader({"NVDA": stock, "SPY": bench}),
    )
    result = runner.run(["2025-01-06", "2025-01-07"])
    assert result.summary["overlapping_windows"] is True
    assert "overlap" in (tmp_path / "summary.md").read_text()


# ---------------------------------------------------------------------------
# Errors, resume, and output files
# ---------------------------------------------------------------------------

def test_failed_date_is_recorded_and_run_continues(tmp_path):
    stock = _closes("2025-01-06", [100, 101, 102])
    bench = _closes("2025-01-06", [50, 50, 50])
    _, result = _run(tmp_path, {"2025-01-06": None, "2025-01-07": "Buy"}, stock, bench)
    assert result.summary["errors"] == 1
    assert result.summary["decisions"] == 1
    assert "provider exploded" in result.records[0].error


def test_fail_fast_when_requested(tmp_path):
    runner = bt.Backtester(
        FakeGraph({"2025-01-06": None}), "NVDA", tmp_path,
        price_loader=_loader({}), continue_on_error=False,
    )
    with pytest.raises(RuntimeError):
        runner.run(["2025-01-06"])


def test_resume_skips_completed_dates_and_retries_errors(tmp_path):
    stock = _closes("2025-01-06", [100, 101, 102, 103])
    bench = _closes("2025-01-06", [50, 50, 50, 50])
    loader = _loader({"NVDA": stock, "SPY": bench})

    first = FakeGraph({"2025-01-06": "Buy", "2025-01-07": None})
    bt.Backtester(first, "NVDA", tmp_path, holding_days=1, price_loader=loader).run(
        ["2025-01-06", "2025-01-07"]
    )

    second = FakeGraph({"2025-01-06": "Sell", "2025-01-07": "Hold", "2025-01-08": "Buy"})
    result = bt.Backtester(second, "NVDA", tmp_path, holding_days=1, price_loader=loader).run(
        ["2025-01-06", "2025-01-07", "2025-01-08"]
    )

    # Completed date reused, errored date retried, new date run.
    assert [c[1] for c in second.calls] == ["2025-01-07", "2025-01-08"]
    assert [r.rating for r in result.records] == ["Buy", "Hold", "Buy"]
    assert result.summary["errors"] == 0


def test_output_files_written(tmp_path):
    stock = _closes("2025-01-06", [100, 110])
    bench = _closes("2025-01-06", [50, 51])
    _run(tmp_path, {"2025-01-06": "Buy"}, stock, bench)

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ticker"] == "NVDA" and summary["benchmark"] == "SPY"
    assert summary["cumulative_strategy_return"] == pytest.approx(0.10)
    rows = pd.read_csv(tmp_path / "results.csv")
    assert list(rows["rating"]) == ["Buy"]
    assert "# Backtest: NVDA vs SPY" in (tmp_path / "summary.md").read_text()


def test_price_download_failure_leaves_calls_unscored(tmp_path):
    def broken(symbol, start, end):
        raise ConnectionError("offline")

    runner = bt.Backtester(
        FakeGraph({"2025-01-06": "Buy"}), "NVDA", tmp_path, price_loader=broken,
    )
    result = runner.run(["2025-01-06"])
    assert result.summary["decisions"] == 1
    assert result.summary["scored"] == 0


def test_invalid_rating_is_an_error(tmp_path):
    runner = bt.Backtester(
        FakeGraph({"2025-01-06": "Moon"}), "NVDA", tmp_path, price_loader=_loader({}),
    )
    record = runner.run_date("2025-01-06")
    assert record.rating is None and "unrecognised rating" in record.error


# ---------------------------------------------------------------------------
# Config isolation
# ---------------------------------------------------------------------------

def test_memory_log_disabled_by_default():
    cfg = bt.backtest_config({"memory_log_path": "/real/log.md", "x": 1})
    assert cfg["memory_log_path"] is None and cfg["x"] == 1
    assert TradingMemoryLog(cfg).get_pending_entries() == []
    kept = bt.backtest_config({"memory_log_path": "/real/log.md"}, use_memory=True)
    assert kept["memory_log_path"] == "/real/log.md"


def test_run_backtest_wires_graph_and_isolates_memory(tmp_path, monkeypatch):
    built = {}

    class StubGraph(FakeGraph):
        def __init__(self, selected_analysts, config, callbacks):
            super().__init__(dict.fromkeys(bt.trade_dates("2025-01-06", "2025-01-17"), "Hold"))
            built.update(analysts=selected_analysts, config=config, callbacks=callbacks)

    monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", StubGraph)
    monkeypatch.setattr(bt, "fetch_closes", lambda *a: pd.Series(dtype=float))

    result = bt.run_backtest(
        "NVDA", "2025-01-06", "2025-01-17",
        config={"memory_log_path": "/real/log.md", "results_dir": str(tmp_path)},
        selected_analysts=["market"],
    )
    assert built["config"]["memory_log_path"] is None
    assert built["analysts"] == ["market"]
    assert isinstance(built["callbacks"][0], bt.UsageCounter)
    assert result.summary["decisions"] == 2
    assert result.output_dir == tmp_path / "backtests" / "NVDA_2025-01-06_2025-01-17"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_backtest_passes_options(tmp_path, monkeypatch):
    from cli import main as cli_main

    captured = {}

    def fake_run(ticker, start, end, **kwargs):
        captured.update(ticker=ticker, start=start, end=end, **kwargs)
        return bt.BacktestResult(
            ticker=ticker, benchmark="SPY", holding_days=kwargs["holding_days"],
            long_only=kwargs["long_only"], records=[],
            summary=bt.summarize([], kwargs["holding_days"]), output_dir=tmp_path,
        )

    monkeypatch.setattr(bt, "run_backtest", fake_run)
    result = CliRunner().invoke(cli_main.app, [
        "backtest", "btcusd", "--start", "2025-01-01", "--end", "2025-03-01",
        "--frequency", "monthly", "--holding-days", "10", "--long-only",
        "--output-dir", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert captured["ticker"] == "BTC-USD"
    assert captured["asset_type"] == "crypto"
    # Fundamentals is dropped for crypto.
    assert captured["selected_analysts"] == ["market", "social", "news"]
    assert captured["frequency"] == "monthly"
    assert captured["holding_days"] == 10
    assert captured["long_only"] is True
    assert captured["use_memory"] is False


@pytest.mark.parametrize("args, message", [
    (["--start", "2025-13-01", "--end", "2025-02-01"], "YYYY-MM-DD"),
    (["--start", "2025-03-01", "--end", "2025-02-01"], "on or before"),
    (["--start", "2025-01-01", "--end", "2025-02-01", "-f", "hourly"], "frequency"),
    (["--start", "2025-01-01", "--end", "2025-02-01", "--analysts", "astrology"], "analysts"),
])
def test_cli_backtest_rejects_bad_input(args, message):
    from cli import main as cli_main

    result = CliRunner().invoke(cli_main.app, ["backtest", "NVDA", *args])
    assert result.exit_code != 0
    assert message in result.output


@pytest.mark.parametrize("argv", [[], ["analyze"]])
@pytest.mark.parametrize("flag, expected", [([], None), (["--checkpoint"], True)])
def test_bare_command_still_runs_interactive_analysis(monkeypatch, argv, flag, expected):
    # Adding `backtest` turned the CLI into a multi-command app; a bare
    # `tradingagents` (with or without the checkpoint flag) must still start the
    # interactive analysis, and `tradingagents analyze` must keep working.
    from cli import main as cli_main

    calls = []
    monkeypatch.setattr(cli_main, "run_analysis", lambda checkpoint=None: calls.append(checkpoint))
    result = CliRunner().invoke(cli_main.app, [*argv, *flag] if argv else flag)
    assert result.exit_code == 0, result.output
    assert calls == [expected]


def test_backtest_command_does_not_start_interactive_analysis(tmp_path, monkeypatch):
    from cli import main as cli_main

    monkeypatch.setattr(cli_main, "run_analysis", lambda **k: pytest.fail("analysis ran"))
    monkeypatch.setattr(bt, "run_backtest", lambda *a, **k: bt.BacktestResult(
        ticker="NVDA", benchmark="SPY", holding_days=5, long_only=False, records=[],
        summary=bt.summarize([], 5), output_dir=tmp_path,
    ))
    result = CliRunner().invoke(
        cli_main.app, ["backtest", "NVDA", "--start", "2025-01-01", "--end", "2025-01-31"],
    )
    assert result.exit_code == 0, result.output


def test_resume_refuses_different_settings(tmp_path):
    loader = _loader({"NVDA": pd.Series(dtype=float), "SPY": pd.Series(dtype=float)})

    def runner(model):
        return bt.Backtester(
            FakeGraph({"2025-01-06": "Buy"}), "NVDA", tmp_path, price_loader=loader,
            run_settings={"deep_think_llm": model},
        )

    runner("model-a").run(["2025-01-06"])
    runner("model-a").run(["2025-01-06"])  # same settings resume fine
    with pytest.raises(ValueError, match="deep_think_llm"):
        runner("model-b").run(["2025-01-06"])


def test_resume_skips_a_line_cut_short_by_a_crash(tmp_path):
    good = json.dumps({"trade_date": "2025-01-06", "rating": "Buy"})
    (tmp_path / "decisions.jsonl").write_text(good + '\n{"trade_date": "2025-01-07", "rat')
    graph = FakeGraph({"2025-01-06": "Sell", "2025-01-07": "Hold"})
    result = bt.Backtester(graph, "NVDA", tmp_path, price_loader=_loader({})).run(
        ["2025-01-06", "2025-01-07"]
    )
    assert [c[1] for c in graph.calls] == ["2025-01-07"]
    assert [r.rating for r in result.records] == ["Buy", "Hold"]


def test_cli_backtest_reports_settings_mismatch(monkeypatch):
    from cli import main as cli_main

    def mismatch(*a, **k):
        raise ValueError("holds a backtest with different settings (deep_think_llm)")

    monkeypatch.setattr(bt, "run_backtest", mismatch)
    result = CliRunner().invoke(
        cli_main.app, ["backtest", "NVDA", "--start", "2025-01-01", "--end", "2025-01-31"],
    )
    assert result.exit_code == 1
    assert "different settings" in result.output
    assert "Traceback" not in result.output
