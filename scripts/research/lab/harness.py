"""Backtest harness for the research lab: one engine, fixed rules, no judgment calls.

A strategy is a causal function from price bars to a target position (-1..1) held
from the NEXT bar on. The harness charges costs on every change of position, splits
the history into train / test / holdout by time, compares each piece with buy-and-hold
and decides pass or fail with the fixed rules in ``Rules``. Nothing here is tuned per
strategy, and no AI is involved in the scoring.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rules:
    """Pass/fail rules, fixed before any result is seen. Change them only in a commit
    that says why, never to rescue a strategy."""

    train_frac: float = 0.6  # first 60% of bars: where the idea may be explored
    test_frac: float = 0.2  # next 20%: the out-of-sample verdict
    # the last 20% is the holdout: sealed, scored once per strategy with --final
    min_trades: int = 30  # fewer trades than this in a segment is too small a sample
    max_drawdown: float = -0.25  # worst peak-to-trough loss allowed in a segment
    alpha: float = 0.05  # one-sided error rate, split across every strategy ever tested
    # v2 (Nov 27, 2026): three stricter checks added, none loosened
    min_profitable_years: float = 0.6  # share of calendar years (train + test) that made money
    min_deflated_sharpe: float = (
        0.95  # probability the test Sharpe beats the best of N noise trials
    )
    regime_ma: int = 200  # moving average that splits bull / bear / sideways markets


RULES_VERSION = 2


@dataclass
class Strategy:
    name: str
    hypothesis: str
    disproof: str  # what result would prove the idea wrong, written before testing
    symbol: str
    signal: Callable[[pd.DataFrame], pd.Series]
    interval: str = "1d"
    start: str = "2000-01-01"
    cost_bps: float = 5.0  # commission + slippage per side, in basis points of the trade
    tags: tuple[str, ...] = ()


@dataclass
class Metrics:
    bars: int
    trades: int
    total_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    exposure: float
    trade_t_stat: float | None
    longest_underwater_days: int

    def as_dict(self) -> dict:
        return {
            k: v if v is None or isinstance(v, int) else round(float(v), 4)
            for k, v in vars(self).items()
        }


@dataclass
class Segment:
    name: str
    start: str
    end: str
    strategy: Metrics
    benchmark: Metrics
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "segment": self.name,
            "start": self.start,
            "end": self.end,
            "passed": self.passed,
            "reasons": self.reasons,
            "strategy": self.strategy.as_dict(),
            "buy_and_hold": self.benchmark.as_dict(),
        }


def required_t(n_tested: int, alpha: float) -> float:
    """t-stat a strategy's trades must reach on the test segment. Splitting ``alpha``
    across every strategy ever tested (Bonferroni) keeps lucky winners out as the
    ledger grows: 1 test needs 1.64, 10 need 2.58, 50 need 3.09."""
    return NormalDist().inv_cdf(1 - alpha / max(1, n_tested))


def bars_per_year(index: pd.DatetimeIndex) -> float:
    years = (index[-1] - index[0]).days / 365.25
    return len(index) / years if years > 0 else 252.0


def net_returns(close: pd.Series, target: pd.Series, cost_bps: float) -> pd.DataFrame:
    """Per-bar returns of holding yesterday's target, after costs on every change."""
    held = target.reindex(close.index).fillna(0.0).clip(-1, 1).shift(1).fillna(0.0)
    ret = close.pct_change().fillna(0.0)
    turnover = held.diff().abs()
    turnover.iloc[0] = abs(held.iloc[0])
    net = held * ret - turnover * cost_bps / 1e4
    return pd.DataFrame({"held": held, "ret": ret, "net": net})


def trade_returns(frame: pd.DataFrame) -> list[float]:
    """Compounded return of each trade: a run of bars held on the same side."""
    side = np.sign(frame["held"].to_numpy())
    net = frame["net"].to_numpy()
    out, cur, acc = [], 0.0, 1.0
    for s, r in zip(side, net, strict=True):
        if s != cur:
            if cur != 0:
                out.append(acc - 1)
            cur, acc = s, 1.0
        if s != 0:
            acc *= 1 + r
    if cur != 0:
        out.append(acc - 1)
    return out


def longest_underwater_days(equity: pd.Series) -> int:
    """Calendar days from a peak to the bar that first got back above it (or the end)."""
    if len(equity) < 2:
        return 0
    below = (equity < equity.cummax()).to_numpy()
    dates = equity.index
    longest, start = 0, None
    for i, under in enumerate(below):
        if under and start is None:
            start = i - 1 if i > 0 else 0
        if start is not None and (not under or i == len(below) - 1):
            longest = max(longest, (dates[i] - dates[start]).days)
            start = None
    return int(longest)


def metrics(frame: pd.DataFrame, per_year: float) -> Metrics:
    net = frame["net"]
    equity = (1 + net).cumprod()
    dd = float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0
    sd = net.std(ddof=1)
    sharpe = float(net.mean() / sd * math.sqrt(per_year)) if sd and sd > 0 else 0.0
    trades = trade_returns(frame)
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    t_stat = None
    if len(trades) > 1 and np.std(trades, ddof=1) > 0:
        t_stat = float(np.mean(trades) / (np.std(trades, ddof=1) / math.sqrt(len(trades))))
    return Metrics(
        bars=len(frame),
        trades=len(trades),
        total_return=float(equity.iloc[-1] - 1) if len(equity) else 0.0,
        sharpe=sharpe,
        max_drawdown=dd,
        win_rate=len(wins) / len(trades) if trades else None,
        avg_win=float(np.mean(wins)) if wins else None,
        avg_loss=float(np.mean(losses)) if losses else None,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        exposure=float((frame["held"] != 0).mean()),
        trade_t_stat=t_stat,
        longest_underwater_days=longest_underwater_days(equity),
    )


def deflated_sharpe(net: pd.Series, n_trials: int) -> dict:
    """Bailey & Lopez de Prado (2014) deflated Sharpe ratio, on per-bar (not annual)
    Sharpe. The bar is the Sharpe the best of ``n_trials`` pure-noise strategies would
    be expected to reach; with no record of the other trials' Sharpes, their spread is
    taken as the noise spread of a Sharpe estimate, 1/sqrt(T-1). Returns the
    probability that the true Sharpe is above that bar."""
    r = net.dropna().to_numpy(dtype=float)
    t = len(r)
    sd = r.std(ddof=1) if t > 1 else 0.0
    if t < 3 or sd == 0:
        return {"dsr": 0.0, "sharpe_per_bar": 0.0, "noise_bar_per_bar": 0.0}
    sr = r.mean() / sd
    euler = 0.5772156649
    nd = NormalDist()
    if n_trials > 1:
        expected_max_z = (1 - euler) * nd.inv_cdf(1 - 1 / n_trials) + euler * nd.inv_cdf(
            1 - 1 / (n_trials * math.e)
        )
    else:
        expected_max_z = 0.0
    sr0 = expected_max_z / math.sqrt(t - 1)
    z = (r - r.mean()) / sd
    skew, kurt = float((z**3).mean()), float((z**4).mean())
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2))
    dsr = nd.cdf((sr - sr0) * math.sqrt(t - 1) / denom)
    return {
        "dsr": round(dsr, 4),
        "sharpe_per_bar": round(sr, 5),
        "noise_bar_per_bar": round(sr0, 5),
    }


def yearly(frame: pd.DataFrame, bench: pd.DataFrame, min_bars: int = 50) -> list[dict]:
    """Walk-forward view: each calendar year scored on its own. The strategies have no
    fitted parameters, so every year is out of sample for the rule itself; judging by
    the spread of years catches an edge that came from one lucky stretch."""
    rows = []
    for year, part in frame.groupby(frame.index.year):
        if len(part) < min_bars:
            continue
        b = bench.loc[part.index]
        rows.append(
            {
                "year": int(year),
                "return": round(float((1 + part["net"]).prod() - 1), 4),
                "buy_and_hold": round(float((1 + b["net"]).prod() - 1), 4),
            }
        )
    return rows


def regimes(close: pd.Series, frame: pd.DataFrame, bench: pd.DataFrame, ma: int) -> dict:
    """Return by market regime, labelled from information known before each bar:
    bull = price above a rising average, bear = below a falling one, else sideways."""
    sma = close.rolling(ma).mean()
    rising = sma > sma.shift(20)
    label = pd.Series("sideways", index=close.index)
    label[(close > sma) & rising] = "bull"
    label[(close < sma) & ~rising & sma.notna()] = "bear"
    label = label.shift(1).reindex(frame.index).fillna("sideways")
    out = {}
    for name in ("bull", "bear", "sideways"):
        mask = label == name
        if mask.sum() == 0:
            continue
        out[name] = {
            "bars": int(mask.sum()),
            "return": round(float((1 + frame["net"][mask]).prod() - 1), 4),
            "buy_and_hold": round(float((1 + bench["net"][mask]).prod() - 1), 4),
        }
    return out


def check_causal(signal: Callable[[pd.DataFrame], pd.Series], bars: pd.DataFrame) -> None:
    """Fail loudly if a signal peeks at the future: its values on a shortened history
    must match the same bars computed on the full history."""
    full = signal(bars)
    for frac in (0.5, 0.8):
        k = int(len(bars) * frac)
        part = signal(bars.iloc[:k])
        a, b = full.iloc[:k].fillna(0.0), part.reindex(full.index[:k]).fillna(0.0)
        if not np.allclose(a.to_numpy(dtype=float), b.to_numpy(dtype=float)):
            raise ValueError(
                "signal uses future data: its past values change when later bars are added"
            )


def split_points(n: int, rules: Rules) -> tuple[int, int]:
    return int(n * rules.train_frac), int(n * (rules.train_frac + rules.test_frac))


def judge(m: Metrics, bench: Metrics, rules: Rules, t_needed: float | None) -> list[str]:
    reasons = []
    if m.trades < rules.min_trades:
        reasons.append(f"only {m.trades} trades (needs {rules.min_trades})")
    if m.total_return <= 0:
        reasons.append(f"lost money after costs ({m.total_return:+.1%})")
    if m.sharpe <= bench.sharpe:
        reasons.append(f"Sharpe {m.sharpe:.2f} does not beat buy-and-hold {bench.sharpe:.2f}")
    if m.max_drawdown < rules.max_drawdown:
        reasons.append(f"drawdown {m.max_drawdown:.0%} is worse than {rules.max_drawdown:.0%}")
    if t_needed is not None and (m.trade_t_stat is None or m.trade_t_stat < t_needed):
        got = "n/a" if m.trade_t_stat is None else f"{m.trade_t_stat:.2f}"
        reasons.append(f"trade t-stat {got} is below the {t_needed:.2f} required")
    return reasons


def evaluate(
    strategy: Strategy,
    bars: pd.DataFrame,
    n_tested: int,
    rules: Rules | None = None,
    final: bool = False,
) -> dict:
    """Score a strategy. ``n_tested`` counts every strategy in the ledger including
    this one, which sets how strong the test-segment evidence must be."""
    rules = rules or Rules()
    check_causal(strategy.signal, bars)
    close = bars["Close"]
    frame = net_returns(close, strategy.signal(bars), strategy.cost_bps)
    bench = net_returns(close, pd.Series(1.0, index=close.index), 0.0)
    per_year = bars_per_year(close.index)
    a, b = split_points(len(frame), rules)
    pieces = [("train", 0, a), ("test", a, b)] + ([("holdout", b, len(frame))] if final else [])
    t_needed = required_t(n_tested, rules.alpha)
    segments = []
    for name, lo, hi in pieces:
        m = metrics(frame.iloc[lo:hi], per_year)
        bm = metrics(bench.iloc[lo:hi], per_year)
        reasons = judge(m, bm, rules, t_needed if name != "train" else None)
        if name == "train" and m.total_return <= 0:
            reasons = [f"lost money even on the data it was designed on ({m.total_return:+.1%})"]
        elif name == "train":
            reasons = []
        idx = frame.index[lo:hi]
        segments.append(
            Segment(name, str(idx[0].date()), str(idx[-1].date()), m, bm, not reasons, reasons)
        )
    seg = {s.name: s for s in segments}

    # v2 checks: every calendar year so far, deflated Sharpe, regimes (report only).
    years = yearly(frame.iloc[:b], bench.iloc[:b])
    profitable = sum(1 for y in years if y["return"] > 0) / len(years) if years else 0.0
    if profitable < rules.min_profitable_years:
        seg["test"].reasons.append(
            f"made money in only {profitable:.0%} of years (needs {rules.min_profitable_years:.0%})"
        )
    dsr = deflated_sharpe(frame["net"].iloc[a:b], n_tested)
    if dsr["dsr"] < rules.min_deflated_sharpe:
        seg["test"].reasons.append(
            f"deflated Sharpe {dsr['dsr']:.2f} is below {rules.min_deflated_sharpe:.2f} "
            f"for {n_tested} strategies tested"
        )
    seg["test"].passed = not seg["test"].reasons
    test_regimes = regimes(close, frame.iloc[a:b], bench.iloc[a:b], rules.regime_ma)
    beats = [k for k, v in test_regimes.items() if v["return"] > v["buy_and_hold"]]

    passed = seg["train"].passed and seg["test"].passed and (not final or seg["holdout"].passed)
    return {
        "verdict": "pass" if passed else "fail",
        "rules_version": RULES_VERSION,
        "t_required": round(t_needed, 2),
        "strategies_counted": n_tested,
        "segments": [s.as_dict() for s in segments],
        "years": years,
        "profitable_years": round(profitable, 3),
        "worst_year": min(years, key=lambda y: y["return"]) if years else None,
        "deflated_sharpe": dsr,
        "test_regimes": test_regimes,
        "beats_buy_and_hold_in": beats,
        "frame": frame,
    }
