# Research lab

A paper-only research desk: ideas are written down before testing, tested by one
fixed harness, and every result (mostly failures) is kept in a ledger so the same
idea is not tried twice. It follows the "self-improving trading research team"
loop, with one change: the builder and backtester are plain code, not AI roles,
so results can't be made up.

| Role | Here |
|---|---|
| Idea agent | An AI (Claude Code or any chat model) that reads `run.py brief` first and proposes one idea, with what would prove it wrong |
| Builder | A file in `strategies/` defining `STRATEGY`: a causal signal plus the hypothesis and disproof |
| Backtester | `harness.py`: costs, train/test/holdout split, buy-and-hold comparison, fixed pass rules |
| Paper trader | Not set up: needs a broker demo account (for example Alpaca paper trading) |
| Memory | `ledger.jsonl`: every strategy tested, appended, never deleted |

## Run

```sh
python scripts/research/lab/run.py brief                    # read before proposing an idea
python scripts/research/lab/run.py test spy_rsi2_reversion  # train + test verdict, logged
python scripts/research/lab/run.py list                     # the whole ledger
```

Run outputs (per-bar returns, the full result) go to `results/lab/<strategy>/`,
which git ignores. The ledger is the record.

## The rules (`harness.Rules`, fixed before any result)

- History is split by time: the first 60% is **train**, the next 20% is **test**,
  the last 20% is the **holdout**.
- The holdout is sealed. `--final` opens it, and only once per strategy.
- Positions are taken at a bar's close and held from the next bar. Costs (default
  5 bps per side) are charged on every change.
- Every run checks the signal for lookahead and fails loudly if its past values
  change when later bars are added.
- A strategy passes only if it **made money on train**, and on test it:
  - made at least 30 trades;
  - made money after costs;
  - beat buy-and-hold's Sharpe ratio;
  - kept the worst drawdown above -25%;
  - reached a trade t-stat above a bar that rises with every strategy ever tested
    (Bonferroni: 1.64 for the first, 2.58 by the tenth, 3.09 by the fiftieth), so
    luck can't win by sheer number of tries;
  - has a **deflated Sharpe** of at least 0.95 (Bailey & López de Prado): the
    probability its test-period Sharpe beats the best Sharpe that as many
    pure-noise strategies would be expected to reach (added in rules v2);
- and across every calendar year of train + test it **made money in at least 60%
  of years** (walk-forward view, rules v2). The report names the worst year.

Every report also shows, without pass/fail:

- **Longest time underwater**: calendar days from a peak until the strategy got
  back above it.
- **Regimes** in the test period: return in bull (price above a rising 200-day
  average), bear (below a falling one) and sideways markets, against
  buy-and-hold. A note flags a strategy that beats buy-and-hold in only one.

Strategies here have no fitted parameters, so every year is already out of sample
for the rule; a strategy that tunes parameters would need a `fit` step re-run on
each past window before its next year, which the harness doesn't do yet.

Change a rule only in its own commit that says why, never to rescue a strategy.

## Ledger so far (Nov 27, 2026)

Seven earlier tests were added by hand (`source: manual`). Two new ideas ran
through the harness under rules v1, again under v2, and again after a fix that charges each trade its own exit cost (all records kept):

| Strategy | Verdict | Why |
|---|---|---|
| `spy_sma200_trend`: hold SPY only above its 200-day average | Fail | Only 18 trades in the 2014-2020 test; Sharpe 0.63 vs buy-and-hold 0.68; made money in 58% of years; deflated Sharpe 0.52 |
| `spy_rsi2_reversion`: buy 2-day dips in uptrends, sell the bounce | Fail | Won 69% of 152 test trades but lost 2.3% after costs; deflated Sharpe 0.07; even with zero costs its Sharpe (0.27) trailed buy-and-hold (0.68) |

Both beat buy-and-hold only in sideways markets during the test years.

Both holdouts are still sealed.
