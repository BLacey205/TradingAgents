# Options income strategy: defined-risk credit spreads ($1,000 account)

Educational framework derived from a YouTube video on generating income by
selling options (credit spreads screened with Barchart). Not financial advice.
The rules below are enforced in code by `tradingagents/risk_gate.py`
(tests: `tests/test_risk_gate.py`).

Two rules are additions, not from the video: the **2x-credit loss exit** and the
**total-exposure cap**. The video's screener example risks up to $1,000 on each
of seven trades, which would break its own 5% per-trade rule.

## Scaling the video to $1,000

| Item | Video ($10k) | This version ($1k) |
|---|---|---|
| Spread width | up to $10 | $1 wide (or $0.50) |
| Risk per trade | 5% (10% max) | 7-10% ($70-$100), a deliberate deviation |
| Total open risk | n/a | 20% ($200) |
| Open positions | n/a | 2 max, 1 per sector |
| Strategies | credit spreads, CSPs, covered calls | defined-risk credit spreads only |

Never sell naked options. Cash-secured puts and covered calls need more capital.

## Entry screen (Barchart)

| Filter | Setting |
|---|---|
| Days to expiration | 30-45, weekly expirations |
| Premium | >= $0.20 per share |
| Maximum loss | < $1.00 per share |
| IV rank | >= 50 |
| Underlying | large/mega-cap stocks and liquid ETFs, ideally under ~$60-80 |
| Sort | lowest probability of loss first |

Confirm before entering: trend agrees with direction (bull put = bullish,
bear call = bearish), short strike is outside the expected move, bid/ask on the
spread <= $0.05, and no earnings date before expiration.

## Exits

1. **Take profit** at 80% of the credit (a $0.20 credit closes at $0.04).
2. **Stop loss** when the spread's value reaches 2x the credit ($0.40).
3. **Earnings:** close before the announcement.
4. **Expiration:** close inside ~5 days.
5. **Roll** once, only for a net credit and only if the thesis is intact.

## Expected results (be realistic)

A $1-wide spread, $0.20 credit, ~85% win rate:
win +$16, stopped loss about -$20, commissions about $2.60 round trip.
Back-of-envelope expected value is about +$8 per trade after commissions,
before slippage, but the simulation below shows that estimate is optimistic.
With 4-8 trades a month, the best case is **$20-$60 (2-6%)**, and losing months
(or a negative edge after costs) are likely. $1,000/month is not achievable at this size. Aim to grow the
account and the process; reinvest everything until ~$2,500.

## What the simulation says (read this before trading it)

`tradingagents/spread_backtest.py` replays the rules on synthetic price paths
(Black-Scholes, 25% short-strike loss probability, $1-wide spreads, 700-bar
random walks, 40 seeds, a weekly bullish signal with no real edge):

| Case | Avg P&L per trade | Mean return per run |
|---|---|---|
| Rules, with costs ($0.02 slippage each way + $2.60 commissions) | -$3.09 | -10.1% |
| Rules, no stop | -$1.97 | -5.7% |
| Rules, no costs | +$3.82 | +15.3% |

- **Friction is the whole game at this size.** A $1-wide spread pays about
  $0.21, so $4 of slippage and $2.60 of commissions eat about a third of every
  credit. The rules have a small positive edge before costs and a negative one
  after.
- **The 85% win rate in the video is not what the rules produce.** With the 2x
  stop and 80% take-profit, simulated win rate was about 58%, because
  marked-to-market stops fire often.
- **A 15% loss-probability spread on a $1-wide width pays only about $0.05-$0.10**,
  below the $0.20 minimum credit, so the gate rejects it. $0.20 needs roughly a
  25% loss probability, which is a lower-probability trade than the video
  describes.
- These are synthetic results with an assumed volatility, so they show the
  rules' sensitivity to costs, not real-market returns.

Practical implications: use the cheapest commissions you can get (some brokers
charge $0), trade only liquid underlyings with tight spreads, favor a wider
spread (fewer, larger trades) once the account allows, and paper trade before
risking real money.

Run it on a real backtest with `tradingagents backtest NVDA --start ... --end ...
--spreads --account-size 1000`, which writes `spread_trades.csv` and
`spread_summary.md` next to the usual results.

## Growth path

| Account | Upgrade |
|---|---|
| $1,000 - $2,500 | $1-wide spreads, 2 positions, 7-10% risk each |
| $2,500 - $5,000 | $2-wide spreads, 3 positions, ~5% risk each |
| $5,000+ | the $10k version, then cash-secured puts and covered calls |

For other account sizes, change `account_size` and the percentage caps in
`RiskConfig`; the gate sizes contracts to fit.

## Using the risk gate

```python
from datetime import date
from tradingagents.risk_gate import CreditSpread, RiskConfig, evaluate_entry, evaluate_exit

cfg = RiskConfig(account_size=1000)
spread = CreditSpread(
    underlying="XLF", side="put", short_strike=50, long_strike=49, credit=0.20,
    expiration=date(2026, 2, 13), sector="financials", iv_rank=60, bid_ask=0.03,
    earnings_date=None,
)
decision = evaluate_entry(spread, open_positions=[], cfg=cfg, today=date(2026, 1, 5))
signal = evaluate_exit(spread, current_value=0.04, cfg=cfg, today=date(2026, 1, 20))
```

`evaluate_entry` returns `allowed`, the contract count that fits the caps, and
the reasons for any rejection. `evaluate_exit` returns one of `hold`,
`take_profit`, `stop_loss`, `close_before_earnings`, `close_expiring`.

## Monthly routine

- Week 1: run the screen; enter one spread, a second only in a different sector.
- Daily (5 min): check the 80% target, the 2x stop, and earnings dates.
- Month end: journal every trade (strategy, entry/exit, credit, max risk,
  thesis) and review sizing, profit-taking and loser management.
- Paper trade at least one month first.

## Risks

Fees and slippage consume a large share of small wins. Overnight gaps can skip
a stop (the spread caps the loss at max loss). Short options can be assigned
early, especially around dividends. Spread approval and margin requirements
vary by broker.
