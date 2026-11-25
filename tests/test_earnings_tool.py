"""Earnings-calendar tool: date lookup, report, and registry wiring."""

from datetime import date

import pandas as pd
import pytest

from tradingagents.dataflows import yfinance_earnings as ye
from tradingagents.dataflows.interface import TOOLS_CATEGORIES, VENDOR_METHODS


class _FakeTicker:
    def __init__(self, frame):
        self._frame = frame

    def get_earnings_dates(self, limit=40):
        return self._frame


def _frame(*days):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in days])
    return pd.DataFrame({"EPS Estimate": [1.0] * len(days)}, index=idx)


@pytest.mark.unit
def test_next_earnings_date_picks_first_on_or_after():
    dates = [date(2024, 1, 25), date(2024, 4, 25), date(2024, 7, 25)]
    assert ye.next_earnings_date(dates, date(2024, 4, 25)) == date(2024, 4, 25)
    assert ye.next_earnings_date(dates, date(2024, 4, 26)) == date(2024, 7, 25)
    assert ye.next_earnings_date(dates, date(2025, 1, 1)) is None


@pytest.mark.unit
def test_fetch_earnings_dates_sorted_and_deduped(monkeypatch):
    frame = _frame("2024-07-25", "2024-01-25", "2024-01-25")
    monkeypatch.setattr(ye.yf, "Ticker", lambda _s: _FakeTicker(frame))
    assert ye.fetch_earnings_dates("AAPL") == [date(2024, 1, 25), date(2024, 7, 25)]


@pytest.mark.unit
def test_fetch_earnings_dates_handles_missing_and_errors(monkeypatch):
    monkeypatch.setattr(ye.yf, "Ticker", lambda _s: _FakeTicker(None))
    assert ye.fetch_earnings_dates("SPY") == []

    class _Boom:
        def get_earnings_dates(self, limit=40):
            raise RuntimeError("down")

    monkeypatch.setattr(ye.yf, "Ticker", lambda _s: _Boom())
    monkeypatch.setattr(ye, "yf_retry", lambda fn: fn())
    assert ye.fetch_earnings_dates("SPY") == []


@pytest.mark.unit
def test_get_next_earnings_report(monkeypatch):
    monkeypatch.setattr(ye, "fetch_earnings_dates", lambda _t: [date(2024, 1, 25), date(2024, 4, 25)])
    text = ye.get_next_earnings("aapl", "2024-04-10")
    assert "2024-04-25" in text and "15 days away" in text and "2024-01-25" in text
    assert "not yet announced" in ye.get_next_earnings("aapl", "2024-05-01")


@pytest.mark.unit
def test_get_next_earnings_without_calendar(monkeypatch):
    monkeypatch.setattr(ye, "fetch_earnings_dates", lambda _t: [])
    assert "No earnings calendar" in ye.get_next_earnings("SPY", "2024-04-10")


@pytest.mark.unit
def test_tool_is_registered_with_a_vendor():
    assert "get_next_earnings" in TOOLS_CATEGORIES["fundamental_data"]["tools"]
    assert "yfinance" in VENDOR_METHODS["get_next_earnings"]


@pytest.mark.unit
def test_agent_tool_routes_to_vendor(monkeypatch):
    from tradingagents.agents.utils import fundamental_data_tools as fdt

    seen = {}
    monkeypatch.setattr(fdt, "route_to_vendor", lambda *a: seen.setdefault("args", a) and "ok")
    out = fdt.get_next_earnings.invoke({"ticker": "AAPL", "curr_date": "2024-04-10"})
    assert out == "ok" and seen["args"] == ("get_next_earnings", "AAPL", "2024-04-10")
