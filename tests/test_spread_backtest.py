"""Credit-spread income backtest: pricing helpers, simulation, integration."""

import math
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradingagents.risk_gate import RiskConfig
from tradingagents.spread_backtest import (
    SpreadSimConfig,
    bs_price,
    iv_rank_proxy,
    pick_short_strike,
    realized_vol_series,
    render_spread_summary,
    run_spread_scoring,
    simulate_spreads,
    write_spread_results,
)

# Gate relaxed on the synthetic IV-rank / credit filters so tests exercise the
# simulation mechanics; those filters have their own tests in test_risk_gate.py.
CFG = RiskConfig(min_iv_rank=0.0, min_credit=0.01)
SIM = SpreadSimConfig()


def _history(n=320, seed=7, start=50.0, daily_vol=0.028):
    rng = np.random.default_rng(seed)
    px = start * np.exp(np.cumsum(rng.normal(0.0, daily_vol, n)))
    return pd.Series(px, index=pd.bdate_range("2023-01-02", periods=n))


def _extend(closes, path):
    idx = pd.bdate_range(closes.index[-1] + pd.offsets.BDay(1), periods=len(path))
    return pd.concat([closes, pd.Series(path, index=idx)])


def _signal_date(closes):
    return closes.index[-1].date().isoformat()


@pytest.mark.unit
def test_black_scholes_put_call_parity():
    s, k, t, sig, r = 50.0, 48.0, 0.1, 0.3, 0.04
    call = bs_price(s, k, t, sig, r, "call")
    put = bs_price(s, k, t, sig, r, "put")
    assert call - put == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)
    assert bs_price(s, k, 0.0, sig, r, "put") == 0.0  # expired OTM


@pytest.mark.unit
@pytest.mark.parametrize("side", ["put", "call"])
def test_short_strike_hits_target_probability(side):
    s, t, sig, r, p = 100.0, 38 / 365, 0.3, 0.04, 0.15
    k = pick_short_strike(s, t, sig, r, p, side, step=0.01)
    drift = (r - 0.5 * sig**2) * t
    z = (math.log(k / s) - drift) / (sig * math.sqrt(t))
    prob = (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))) if side == "call" else 0.5 * (1 + math.erf(z / math.sqrt(2)))
    assert prob == pytest.approx(p, abs=0.01)
    assert (k < s) if side == "put" else (k > s)


@pytest.mark.unit
def test_iv_rank_proxy_and_insufficient_history():
    vol = pd.Series([0.2] * 100 + [0.4] * 5 + [0.3])
    assert iv_rank_proxy(vol, len(vol) - 1, 252, 90) == pytest.approx(50.0)
    assert iv_rank_proxy(vol, 50, 252, 90) is None  # fewer than 90 observations
    assert iv_rank_proxy(pd.Series([0.2] * 120), 119, 252, 90) is None  # flat: no range


@pytest.mark.unit
def test_calm_uptrend_bull_put_wins_and_respects_risk_cap():
    closes = _history()
    sig = _signal_date(closes)
    path = closes.iloc[-1] * (1 + 0.0004 * np.arange(1, 70))
    result = simulate_spreads("TEST", _extend(closes, path), [(sig, "Buy")], [], CFG, SIM)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "put" and trade.pnl > 0
    assert trade.exit_reason in {"take_profit", "close_expiring", "expired"}
    assert trade.max_loss <= CFG.account_size * CFG.max_trade_risk_pct + 1e-6
    assert result.summary["win_rate"] == 1.0
    assert result.summary["ending_equity"] == pytest.approx(CFG.account_size + trade.pnl)


@pytest.mark.unit
def test_crash_triggers_stop_loss_with_bounded_loss():
    closes = _history()
    sig = _signal_date(closes)
    spot = closes.iloc[-1]
    path = np.concatenate([[spot * 0.99], np.full(68, spot * 0.70)])
    result = simulate_spreads("TEST", _extend(closes, path), [(sig, "Buy")], [], CFG, SIM)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    commissions = SIM.commission_per_leg * 4
    assert -(trade.max_loss + commissions + 0.01) <= trade.pnl < 0


@pytest.mark.unit
def test_bearish_rating_opens_bear_call():
    closes = _history()
    path = closes.iloc[-1] * (1 - 0.0004 * np.arange(1, 70))
    result = simulate_spreads("TEST", _extend(closes, path), [(_signal_date(closes), "Sell")], [], CFG, SIM)
    assert result.trades[0].side == "call" and result.trades[0].pnl > 0


@pytest.mark.unit
def test_hold_and_missing_rating_are_skipped():
    closes = _history()
    sig = _signal_date(closes)
    result = simulate_spreads("TEST", closes, [(sig, "Hold"), (sig, None)], [], CFG, SIM)
    assert not result.trades
    assert len(result.skipped) == 2
    assert "no directional rating" in result.skipped[0].reasons


@pytest.mark.unit
def test_earnings_inside_window_blocks_entry():
    closes = _history()
    sig = _signal_date(closes)
    last = closes.index[-1].date()
    earnings = [date.fromordinal(last.toordinal() + 20)]
    path = closes.iloc[-1] * (1 + 0.0004 * np.arange(1, 70))
    result = simulate_spreads("TEST", _extend(closes, path), [(sig, "Buy")], earnings, CFG, SIM)
    assert not result.trades
    assert any("earnings" in r for s in result.skipped for r in s.reasons)
    assert any("earnings" in k for k in result.summary["skip_reasons"])


@pytest.mark.unit
def test_short_history_is_skipped_for_missing_volatility():
    closes = _history(n=10)
    result = simulate_spreads("TEST", closes, [(_signal_date(closes), "Buy")], [], CFG, SIM)
    assert not result.trades
    assert "volatility" in result.skipped[0].reasons[0]


@pytest.mark.unit
def test_open_position_marked_to_market_at_end_of_data():
    closes = _history()
    path = closes.iloc[-1] * np.ones(3)  # too short to reach any exit rule
    result = simulate_spreads("TEST", _extend(closes, path), [(_signal_date(closes), "Buy")], [], CFG, SIM)
    assert result.trades[0].exit_reason == "end_of_data"


@pytest.mark.unit
def test_realized_vol_series_scale():
    closes = _history(n=400)
    vol = realized_vol_series(closes.reset_index(drop=True), 30).dropna()
    assert 0.3 < float(vol.mean()) < 0.6  # 2.8% daily ~ 44% annualised


@pytest.mark.unit
def test_run_spread_scoring_uses_injected_loaders_and_writes_files(tmp_path):
    closes = _history()
    path = closes.iloc[-1] * (1 + 0.0004 * np.arange(1, 70))
    full = _extend(closes, path)
    backtest_result = SimpleNamespace(
        ticker="TEST",
        records=[SimpleNamespace(trade_date=_signal_date(closes), rating="Buy"),
                 SimpleNamespace(trade_date=_signal_date(closes), rating=None)],
    )
    result = run_spread_scoring(
        backtest_result, risk_cfg=CFG, sim_cfg=SIM,
        price_loader=lambda *_: full, earnings_loader=lambda _t: [],
    )
    assert len(result.trades) == 1
    text = render_spread_summary(result)
    assert "Synthetic pricing" in text and "TEST" in text
    write_spread_results(result, tmp_path)
    assert (tmp_path / "spread_trades.csv").exists()
    assert (tmp_path / "spread_summary.md").exists()
    assert (tmp_path / "spread_summary.json").exists()


@pytest.mark.unit
def test_run_spread_scoring_with_no_ratings():
    backtest_result = SimpleNamespace(ticker="TEST", records=[SimpleNamespace(trade_date="2024-01-02", rating=None)])
    result = run_spread_scoring(backtest_result, risk_cfg=CFG, price_loader=lambda *_: pd.Series(dtype=float))
    assert result.trades == [] and result.summary == {}
