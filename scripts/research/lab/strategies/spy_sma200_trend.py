"""Idea: hold SPY only while it closes above its 200-day average; sit in cash otherwise."""

import pandas as pd
from harness import Strategy


def signal(bars: pd.DataFrame) -> pd.Series:
    close = bars["Close"]
    sma = close.rolling(200).mean()
    return (close > sma).astype(float).where(sma.notna(), 0.0)


STRATEGY = Strategy(
    name="spy_sma200_trend",
    hypothesis="holding SPY only above its 200-day moving average avoids big bear markets "
    "and gives a better return for the risk than always holding",
    disproof="Sharpe on the test years not above buy-and-hold, or too few trades to judge",
    symbol="SPY",
    signal=signal,
    start="1995-01-01",
    tags=("daily", "trend-following", "moving-average", "etf"),
)
