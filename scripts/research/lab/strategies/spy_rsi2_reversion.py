"""Idea: buy short, sharp dips in SPY while its long-term trend is up; sell on the bounce.

Entry: 2-day RSI below 10 and close above the 200-day average.
Exit: the first close above the 5-day average.
"""

import pandas as pd
from harness import Strategy


def rsi(close: pd.Series, n: int) -> pd.Series:
    change = close.diff()
    up = change.clip(lower=0).rolling(n).mean()
    down = (-change.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / down.replace(0, float("nan")))


def signal(bars: pd.DataFrame) -> pd.Series:
    close = bars["Close"]
    r2 = rsi(close, 2).fillna(50).to_numpy()
    sma200 = close.rolling(200).mean().to_numpy()
    sma5 = close.rolling(5).mean().to_numpy()
    c = close.to_numpy()
    pos, held = [], 0.0
    for i in range(len(c)):
        if held == 0 and sma200[i] == sma200[i] and c[i] > sma200[i] and r2[i] < 10:
            held = 1.0
        elif held == 1 and c[i] > sma5[i]:
            held = 0.0
        pos.append(held)
    return pd.Series(pos, index=close.index)


STRATEGY = Strategy(
    name="spy_rsi2_reversion",
    hypothesis="short 2-day selloffs in SPY during long-term uptrends bounce within days, "
    "so buying them earns more per day held than always holding",
    disproof="test-years Sharpe not above buy-and-hold, or trade t-stat below the required bar",
    symbol="SPY",
    signal=signal,
    start="1995-01-01",
    tags=("daily", "mean-reversion", "rsi", "etf"),
)
