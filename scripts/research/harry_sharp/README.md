# Harry Sharp strategy test

Backtests of the intraday futures strategy that Harry Sharp describes in an
IQ Capital interview ([youtu.be/Rk2G9E7axXw](https://youtu.be/Rk2G9E7axXw)).
He claims a 70%+ win rate at 1:1 risk to reward. These scripts are research,
not part of the `tradingagents` package.

| Script | What it tests | Data |
|---|---|---|
| `bias_test.py` | His "story/bias" rule alone: the previous daily candle and the pre-New York 4-hour candle closing the same way predict the New York session | Hourly NQ and ES futures, about 2.4 years |
| `entry_test.py` | His full setup turned into fixed rules: bias, a 6-candle range, a strong breakout candle, the "rubber band" dip, buy stop at the breakout high, stop below the breakout candle, 1:1 target | 5- and 15-minute NQ, ES, YM, RTY and GC futures, about 60 days |

The exact rules are in each script's docstring.

## Run

```sh
pip install yfinance pandas numpy
python scripts/research/harry_sharp/bias_test.py           # NQ and ES by default
python scripts/research/harry_sharp/bias_test.py NQ=F      # or name the symbols
python scripts/research/harry_sharp/entry_test.py          # also writes entry_trades.csv to the current folder
```

Yahoo Finance keeps only the last 730 days of hourly bars and the last 60 days of
5- and 15-minute bars, so each run covers a newer window and the numbers move.

## Results (run on Nov 22, 2026)

Bias rule, Jul 2024 to Nov 2026, 549 days. The bias was right 47.7% to 52.6% of
the time across eight versions, while picking at random would have been right
about 50%. None of the gaps is statistically meaningful.

Full setup, Sep 14 to Nov 20, 2026, exit A (all out at 1:1):

| Version | Trades | Win rate | Per trade after costs |
|---|---|---|---|
| 5-min, with bias | 54 | 57.4% | +0.08R |
| 5-min, no bias | 189 | 55.0% | +0.03R |
| 15-min, with bias | 17 | 47.1% | −0.09R |
| 15-min, no bias | 72 | 52.8% | +0.04R |

Break-even at 1:1 after costs is about 53%. The largest sample (189 trades)
puts the likely true win rate between 48% and 62%, below the 70% claim. As
fixed rules, the strategy shows no edge; any edge would have to come from
discretion these rules leave out.

Limits: free Yahoo Finance data, hourly bars that start the New York window at
9:00 rather than 9:30, a short minute-bar sample, contract-roll jumps in
continuous futures, and a guessed order of high and low inside each candle
(the stop is assumed to come first when both are touched).
