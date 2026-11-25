"""Credit-spread risk gate: entry caps and exit rules."""

from datetime import date, timedelta

import pytest

from tradingagents.risk_gate import (
    CreditSpread,
    RiskConfig,
    evaluate_entry,
    evaluate_exit,
    total_open_risk,
)

TODAY = date(2026, 1, 5)
CFG = RiskConfig()  # $1,000 account defaults


def _spread(**kw):
    base = dict(
        underlying="XLF", side="put", short_strike=50.0, long_strike=49.0,
        credit=0.20, expiration=TODAY + timedelta(days=38), sector="financials",
        iv_rank=60.0, bid_ask=0.03,
    )
    base.update(kw)
    return CreditSpread(**base)


@pytest.mark.unit
def test_max_loss_math():
    s = _spread()
    assert s.width == 1.0
    assert s.max_loss_per_contract == pytest.approx(80.0)
    assert s.max_loss == pytest.approx(80.0)


@pytest.mark.unit
@pytest.mark.parametrize("kw", [
    dict(credit=1.0), dict(credit=0), dict(contracts=0),
    dict(short_strike=48.0),                      # put: short below long
    dict(side="call", short_strike=50, long_strike=49),  # call: short above long
])
def test_invalid_spreads_rejected(kw):
    with pytest.raises(ValueError):
        _spread(**kw)


@pytest.mark.unit
def test_clean_entry_allowed():
    d = evaluate_entry(_spread(), [], CFG, TODAY)
    assert d.allowed and d.contracts == 1 and d.reasons == []


@pytest.mark.unit
def test_trade_risk_cap_rejects_wide_spread():
    # $5-wide spread risks ~$480 on a $1k account (cap is $100)
    d = evaluate_entry(_spread(long_strike=45.0, credit=0.20), [], CFG, TODAY)
    assert not d.allowed and d.contracts == 0
    assert any("risk caps" in r for r in d.reasons)


@pytest.mark.unit
def test_contracts_scale_down_to_fit_caps():
    big = RiskConfig(account_size=10_000)  # trade cap $1,000
    d = evaluate_entry(_spread(contracts=20), [], big, TODAY)
    assert d.allowed and d.contracts == 12  # floor(1000 / 80)


@pytest.mark.unit
def test_total_exposure_cap():
    open_ = [_spread(underlying="A", sector="a"), _spread(underlying="B", sector="b")]
    assert total_open_risk(open_) == pytest.approx(160.0)
    d = evaluate_entry(_spread(underlying="C", sector="c"), open_, CFG, TODAY)
    assert not d.allowed
    assert any("open positions" in r for r in d.reasons)


@pytest.mark.unit
def test_total_cap_without_position_limit():
    cfg = RiskConfig(max_open_positions=5)  # total cap $200
    open_ = [_spread(underlying="A", sector="a"), _spread(underlying="B", sector="b")]
    d = evaluate_entry(_spread(underlying="C", sector="c"), open_, cfg, TODAY)
    assert not d.allowed and any("risk caps" in r for r in d.reasons)


@pytest.mark.unit
def test_sector_limit():
    d = evaluate_entry(_spread(underlying="XLE"), [_spread()], CFG, TODAY)
    assert not d.allowed and any("sector" in r for r in d.reasons)


@pytest.mark.unit
@pytest.mark.parametrize("days", [29, 46])
def test_dte_window(days):
    d = evaluate_entry(_spread(expiration=TODAY + timedelta(days=days)), [], CFG, TODAY)
    assert not d.allowed and any("DTE" in r for r in d.reasons)


@pytest.mark.unit
def test_iv_rank_credit_and_liquidity_filters():
    d = evaluate_entry(_spread(iv_rank=40, credit=0.10, bid_ask=0.10), [], CFG, TODAY)
    assert not d.allowed
    assert len(d.reasons) == 3


@pytest.mark.unit
def test_unknown_iv_rank_rejected():
    d = evaluate_entry(_spread(iv_rank=None), [], CFG, TODAY)
    assert not d.allowed and any("unknown" in r for r in d.reasons)


@pytest.mark.unit
def test_earnings_before_expiration_blocks_entry():
    d = evaluate_entry(_spread(earnings_date=TODAY + timedelta(days=20)), [], CFG, TODAY)
    assert not d.allowed and any("earnings" in r for r in d.reasons)
    ok = evaluate_entry(_spread(earnings_date=TODAY + timedelta(days=60)), [], CFG, TODAY)
    assert ok.allowed


@pytest.mark.unit
def test_exit_take_profit_at_80_percent():
    s = _spread(credit=1.00, long_strike=48.0)
    assert evaluate_exit(s, 0.20, CFG, TODAY).action == "take_profit"
    assert evaluate_exit(s, 0.21, CFG, TODAY).action == "hold"


@pytest.mark.unit
def test_exit_stop_loss_at_2x_credit():
    s = _spread(credit=0.20)
    assert evaluate_exit(s, 0.40, CFG, TODAY).action == "stop_loss"
    assert evaluate_exit(s, 0.39, CFG, TODAY).action == "hold"


@pytest.mark.unit
def test_exit_before_earnings():
    s = _spread(earnings_date=TODAY + timedelta(days=2))
    assert evaluate_exit(s, 0.15, CFG, TODAY).action == "close_before_earnings"
    far = _spread(earnings_date=TODAY + timedelta(days=10))
    assert evaluate_exit(far, 0.15, CFG, TODAY).action == "hold"


@pytest.mark.unit
def test_exit_near_expiration():
    s = _spread(expiration=TODAY + timedelta(days=5))
    assert evaluate_exit(s, 0.15, CFG, TODAY).action == "close_expiring"


@pytest.mark.unit
def test_stop_loss_beats_other_exits():
    s = _spread(earnings_date=TODAY + timedelta(days=1), expiration=TODAY + timedelta(days=3))
    assert evaluate_exit(s, 0.50, CFG, TODAY).action == "stop_loss"
