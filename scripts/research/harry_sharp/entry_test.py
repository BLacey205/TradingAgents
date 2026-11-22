"""Harry Sharp's full setup as fixed rules, on 5- and 15-minute futures bars (last ~60 days).

Fixed rules (long side; shorts mirror):
  bias     previous futures day (18:00->17:00 ET) and the 02:00-06:00 ET 4-hour candle both closed up
           (variant 'no bias': take setups in either direction)
  window   breakout candles may close 09:30-11:00 ET; open trades close at 16:00 at the latest
  range    the 6 candles before the breakout hold at least 2 bearish candles; range top = highest body
  breakout bullish candle, closes above the range top, body >= 50% of its high-low,
           high-low <= 50% of the range's height (not too big)
  trigger  within the next 3 candles: a candle that first dips at least 25% of the breakout candle's
           size below its own open (the "rubber band" wick), then trades through the breakout high.
           Buy stop at the breakout high. Cancelled if a candle closes back inside the range first.
  stop     1 tick below the breakout candle's low
  exit     A) all out at 1R (his usual 1:1)
           B) half at 1R, stop to breakeven, rest at 2R or 16:00
  one trade per instrument per day; costs in points per round trip
Intrabar: on the trigger candle the dip came first, so only the target can fill there; on later
candles that touch both stop and target, the stop is assumed to come first.
"""

import math

import numpy as np
import pandas as pd
import yfinance as yf

SPECS = {  # tick, round-trip cost in points
    "NQ=F": (0.25, 1.0),
    "ES=F": (0.25, 0.5),
    "YM=F": (1.0, 4.0),
    "RTY=F": (0.1, 0.4),
    "GC=F": (0.1, 0.6),
}


def hourly_bias(sym):
    h = (
        yf.Ticker(sym)
        .history(period="120d", interval="1h", prepost=True)
        .tz_convert("America/New_York")
    )
    h["tday"] = (h.index + pd.Timedelta(hours=6)).date
    days = sorted(set(h.tday))
    by = {k: g for k, g in h.groupby("tday")}  # noqa: C416 (dict(groupby) breaks: groupby.keys is a str)
    out = {}
    for prev, cur in zip(days, days[1:], strict=False):
        gp, gc = by[prev], by[cur]
        lon = gc[gc.index.hour.isin([2, 3, 4, 5])]
        if len(gp) < 18 or len(lon) != 4:
            continue
        d = np.sign(gp.Close.iloc[-1] - gp.Open.iloc[0])
        f = np.sign(lon.Close.iloc[-1] - lon.Open.iloc[0])
        out[cur] = int(d) if d == f and d != 0 else 0
    return out


def run(sym, interval, use_bias, exit_mode):
    tick, cost = SPECS[sym]
    bars = (
        yf.Ticker(sym)
        .history(period="60d", interval=interval, prepost=True)
        .tz_convert("America/New_York")
    )
    bars = bars[["Open", "High", "Low", "Close"]].dropna()
    bias = hourly_bias(sym)
    step = pd.Timedelta(interval)
    trades = []
    for day, g in bars.groupby(bars.index.date):
        if pd.Timestamp(day).weekday() > 4:
            continue
        b = bias.get(day)
        if use_bias and not b:
            continue
        g = g[
            (g.index.hour >= 4)
            & ((g.index + step) <= g.index[0].normalize() + pd.Timedelta(hours=16))
        ]
        o, h, low, c = (g[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
        close_t = g.index + step
        done = False
        for i in range(6, len(g) - 1):
            ct = close_t[i]
            if not (
                pd.Timestamp(day).tz_localize(ct.tz) + pd.Timedelta(hours=9, minutes=30)
                < ct
                <= pd.Timestamp(day).tz_localize(ct.tz) + pd.Timedelta(hours=11)
            ):
                continue
            for side in [b] if use_bias else [1, -1]:
                if done:
                    break
                lo, hi = i - 6, i
                rng_h = h[lo:hi].max() - low[lo:hi].min()
                bear = (c[lo:hi] < o[lo:hi]).sum() if side > 0 else (c[lo:hi] > o[lo:hi]).sum()
                top = (
                    np.maximum(o[lo:hi], c[lo:hi]).max()
                    if side > 0
                    else np.minimum(o[lo:hi], c[lo:hi]).min()
                )
                size = h[i] - low[i]
                strong = size > 0 and abs(c[i] - o[i]) >= 0.5 * size
                broke = (c[i] > top and c[i] > o[i]) if side > 0 else (c[i] < top and c[i] < o[i])
                if bear < 2 or not strong or not broke or rng_h <= 0 or size > 0.5 * rng_h:
                    continue
                entry_px = h[i] if side > 0 else low[i]
                stop = (low[i] - tick) if side > 0 else (h[i] + tick)
                risk = abs(entry_px - stop)
                for j in range(i + 1, min(i + 4, len(g))):
                    dip = (o[j] - low[j]) if side > 0 else (h[j] - o[j])
                    through = h[j] >= entry_px if side > 0 else low[j] <= entry_px
                    if dip >= 0.25 * size and through:
                        trades.append(
                            dict(
                                sym=sym,
                                day=day,
                                side=side,
                                **manage(
                                    o, h, low, c, j, side, entry_px, stop, risk, exit_mode, cost
                                ),
                            )
                        )
                        done = True
                        break
                    back_in = c[j] < top if side > 0 else c[j] > top
                    if back_in:
                        break
            if done:
                break
    return trades


def manage(o, h, low, c, j, side, entry, stop, risk, mode, cost):
    t1 = entry + side * risk
    t2 = entry + side * 2 * risk

    def fav(k):
        return h[k] if side > 0 else low[k]

    def adv(k):
        return low[k] if side > 0 else h[k]

    def hit(px, lvl):
        return (px >= lvl) if side > 0 else (px <= lvl)

    half_done, cur_stop, r = False, stop, 0.0
    for k in range(j, len(c)):
        stopped = (adv(k) <= cur_stop) if side > 0 else (adv(k) >= cur_stop)
        if k > j and stopped:
            r += (0.5 if half_done else 1.0) * side * (cur_stop - entry) / risk
            return {
                "R": r,
                "net_R": r - cost / risk,
                "outcome": "stop" if not half_done else "1R then BE",
            }
        if hit(fav(k), t1) and not half_done:
            if mode == "A":
                return {"R": 1.0, "net_R": 1.0 - cost / risk, "outcome": "1R"}
            half_done, cur_stop, r = True, entry, 0.5
        if half_done and hit(fav(k), t2):
            return {"R": r + 1.0, "net_R": r + 1.0 - cost / risk, "outcome": "2R"}
    r += (0.5 if half_done else 1.0) * side * (c[-1] - entry) / risk
    return {"R": r, "net_R": r - cost / risk, "outcome": "16:00"}


if __name__ == "__main__":
    rows, alltr = [], []
    for interval in ["5m", "15m"]:
        for use_bias in [True, False]:
            for mode in ["A", "B"]:
                tr = [t for s in SPECS for t in run(s, interval, use_bias, mode)]
                df = pd.DataFrame(tr)
                if df.empty:
                    rows.append({"tf": interval, "bias": use_bias, "exit": mode, "trades": 0})
                    continue
                n = len(df)
                wins = (df.R > 0).sum()
                rows.append(
                    {
                        "tf": interval,
                        "bias": use_bias,
                        "exit": mode,
                        "trades": n,
                        "win": round(wins / n, 3),
                        "avg_R": round(df.R.mean(), 3),
                        "avg_net_R": round(df.net_R.mean(), 3),
                        "t_net": round(df.net_R.mean() / (df.net_R.std(ddof=1) / math.sqrt(n)), 2)
                        if n > 1
                        else None,
                        "total_net_R": round(df.net_R.sum(), 1),
                        "per_market": dict(df.groupby("sym").net_R.sum().round(1)),
                    }
                )
                df["tf"], df["bias"], df["exit"] = interval, use_bias, mode
                alltr.append(df)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 120)
    print(pd.DataFrame(rows).to_string(index=False))
    pd.concat(alltr).to_csv("entry_trades.csv", index=False)
