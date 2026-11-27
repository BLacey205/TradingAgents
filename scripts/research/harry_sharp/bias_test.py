"""Harry Sharp's 'story/bias' rule, tested on hourly NQ and ES futures.

Bias for a New York session on day D:
  - previous daily candle: the full futures day (18:00 ET -> 17:00 ET) before D
  - pre-session 4-hour candle: one of
      'london'  = 02:00-06:00 ET (the 4h candle on an ET 4-hour grid that closes before NY)
      'pre-ny'  = 05:00-09:00 ET (the last four full hours before the 9:00 bar)
  - both up -> bullish, both down -> bearish, otherwise mixed (no trade)
Outcome windows (hourly bars, so the 9:30 open is approximated by the 9:00 bar):
  - 'open2h' = 09:00 open -> 11:00 (covers the NY open and his ~90-minute window)
  - 'day'    = 09:00 open -> 16:00 close
A 'trade' goes long on bullish days and short on bearish days over the window.
"""

import math
import sys

import numpy as np
import pandas as pd
import yfinance as yf

COST_PTS = {"NQ=F": 1.0, "ES=F": 0.5}  # rough round trip: slippage + commission, in index points


def load(sym):
    d = (
        yf.Ticker(sym)
        .history(period="730d", interval="1h", prepost=True)
        .tz_convert("America/New_York")
    )
    d = d[["Open", "High", "Low", "Close"]].dropna()
    d["tday"] = (d.index + pd.Timedelta(hours=6)).date  # 18:00 belongs to the next trading day
    d["hour"] = d.index.hour
    return d


def bar(g, hours):
    s = g[g.hour.isin(hours)]
    if len(s) != len(hours):
        return None
    return s.Open.iloc[0], s.Close.iloc[-1]


def build(d):
    days = sorted(set(d.tday))
    by = {k: g for k, g in d.groupby("tday")}  # noqa: C416 (dict(groupby) breaks: groupby.keys is a str)
    rows = []
    for prev, cur in zip(days, days[1:], strict=False):
        gp, gc = by[prev], by[cur]
        if len(gp) < 18 or pd.Timestamp(cur).weekday() > 4:
            continue
        dly = (gp.Open.iloc[0], gp.Close.iloc[-1])
        lon, pre = bar(gc, [2, 3, 4, 5]), bar(gc, [5, 6, 7, 8])
        o2, dy = bar(gc, [9, 10]), bar(gc, list(range(9, 16)))
        if None in (lon, pre, o2, dy):
            continue

        def sgn(b):
            return np.sign(b[1] - b[0])

        rows.append(
            {
                "day": cur,
                "daily": sgn(dly),
                "london": sgn(lon),
                "pre_ny": sgn(pre),
                "open_px": o2[0],
                "open2h": o2[1] - o2[0],
                "day_move": dy[1] - dy[0],
            }
        )
    return pd.DataFrame(rows)


def evaluate(df, four, window, cost):
    bias = np.where((df.daily == df[four]) & (df.daily != 0), df.daily, 0)
    t = df[bias != 0].copy()
    t["bias"] = bias[bias != 0]
    pnl = t.bias * t[window]
    n = len(t)
    hits = int((pnl > 0).sum())
    up_base = (df[window] > 0).mean()  # how often the window goes up on ANY day
    # chance of matching the window's direction by following an unrelated coin with the same long/short mix
    p_long = (t.bias > 0).mean()
    expected = p_long * up_base + (1 - p_long) * (1 - up_base)
    z = (hits - n * expected) / math.sqrt(n * expected * (1 - expected))
    net = pnl - cost
    half = n // 2
    return {
        "days": len(df),
        "signals": n,
        "long": int((t.bias > 0).sum()),
        "short": int((t.bias < 0).sum()),
        "hit": hits / n,
        "expected": expected,
        "z": z,
        "avg_pts": pnl.mean(),
        "net_pts": net.mean(),
        "t_net": net.mean() / (net.std(ddof=1) / math.sqrt(n)),
        "first_half": pnl.iloc[:half].sum(),
        "second_half": pnl.iloc[half:].sum(),
        "bull_hit": (t[t.bias > 0][window] > 0).mean(),
        "bear_hit": (t[t.bias < 0][window] < 0).mean(),
    }


if __name__ == "__main__":
    out = []
    for sym in sys.argv[1:] or ["NQ=F", "ES=F"]:
        df = build(load(sym))
        print(sym, "days", len(df), df.day.iloc[0], "->", df.day.iloc[-1])
        for four in ["london", "pre_ny"]:
            for window in ["open2h", "day_move"]:
                out.append(
                    dict(
                        sym=sym,
                        four_hour=four,
                        window=window,
                        **evaluate(df, four, window, COST_PTS[sym]),
                    )
                )
    pd.set_option("display.width", 250)
    print(pd.DataFrame(out).round(3).to_string(index=False))
