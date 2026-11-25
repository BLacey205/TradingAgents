"""Credit-spread income backtest driven by the agents' ratings and the risk gate.

Turns each directional rating from a walk-forward backtest into a defined-risk
credit spread (Buy/Overweight -> bull put, Sell/Underweight -> bear call, Hold
-> no trade), then walks the underlying's daily closes, applying
``tradingagents.risk_gate`` for entries (risk caps, DTE, IV rank, earnings
blackout) and exits (80% profit take, 2x-credit stop, earnings, expiration).

IMPORTANT: this is a *synthetic* simulation. yfinance has no historical option
chains, so spreads are priced with Black-Scholes using an implied volatility
proxied from trailing realized volatility (``iv_markup`` x), and "IV rank" is the
percentile of that realized volatility over the prior year. Real fills, skew,
early assignment and gap-through-stop losses are not modelled beyond a flat
per-share slippage and per-leg commission. Treat results as a sanity check of
the rules, not a forecast of returns.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd

from tradingagents.risk_gate import (
    CONTRACT_MULTIPLIER,
    CreditSpread,
    RiskConfig,
    evaluate_entry,
    evaluate_exit,
)

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Sell", "Underweight"}
_N = NormalDist()
TRADING_DAYS = 252


@dataclass(frozen=True)
class SpreadSimConfig:
    target_dte: int = 38  # calendar days, inside the gate's 30-45 window
    short_loss_prob: float = 0.15  # chance the short strike finishes in the money
    width: float = 1.0
    strike_step: float = 0.5
    iv_markup: float = 1.15  # assumed implied vol = realized vol x this
    risk_free: float = 0.04
    slippage: float = 0.02  # per share, paid on entry and on non-expiry exits
    commission_per_leg: float = 0.65
    assumed_bid_ask: float = 0.03  # synthetic quote width handed to the gate
    vol_window: int = 30
    rank_window: int = TRADING_DAYS
    min_rank_history: int = 90
    contracts: int = 1


@dataclass
class SpreadTrade:
    underlying: str
    side: str
    rating: str
    entry_date: str
    exit_date: str
    short_strike: float
    long_strike: float
    contracts: int
    credit: float
    exit_value: float
    exit_reason: str
    pnl: float
    max_loss: float
    equity_after: float


@dataclass
class SkippedSignal:
    date: str
    rating: str | None
    reasons: list[str]


@dataclass
class SpreadBacktest:
    ticker: str
    initial_equity: float
    trades: list[SpreadTrade]
    skipped: list[SkippedSignal]
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

def bs_price(spot: float, strike: float, t: float, sigma: float, r: float, kind: str) -> float:
    """Black-Scholes price of a European put/call (per share)."""
    if t <= 0 or sigma <= 0:
        intrinsic = max(strike - spot, 0.0) if kind == "put" else max(spot - strike, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if kind == "call":
        return spot * _N.cdf(d1) - strike * math.exp(-r * t) * _N.cdf(d2)
    return strike * math.exp(-r * t) * _N.cdf(-d2) - spot * _N.cdf(-d1)


def spread_value(spread: CreditSpread, spot: float, t: float, sigma: float, r: float) -> float:
    """Cost per share to buy back the spread (short leg minus long leg), capped at width."""
    short = bs_price(spot, spread.short_strike, t, sigma, r, spread.side)
    long_ = bs_price(spot, spread.long_strike, t, sigma, r, spread.side)
    return min(max(short - long_, 0.0), spread.width)


def pick_short_strike(spot: float, t: float, sigma: float, r: float, prob: float, side: str, step: float) -> float:
    """Strike whose lognormal probability of finishing in the money is ~``prob``."""
    drift = (r - 0.5 * sigma**2) * t
    z = _N.inv_cdf(prob if side == "put" else 1 - prob)
    strike = spot * math.exp(drift + sigma * math.sqrt(t) * z)
    return round(strike / step) * step


def realized_vol_series(closes: pd.Series, window: int) -> pd.Series:
    returns = closes.pct_change().apply(lambda x: math.log1p(x) if pd.notna(x) else x)
    return returns.rolling(window).std() * math.sqrt(TRADING_DAYS)


def iv_rank_proxy(vol: pd.Series, i: int, rank_window: int, min_history: int) -> float | None:
    """Percentile-style rank (0-100) of today's vol within the prior ``rank_window`` bars."""
    hist = vol.iloc[max(0, i - rank_window + 1): i + 1].dropna()
    if len(hist) < min_history:
        return None
    lo, hi = float(hist.min()), float(hist.max())
    if hi - lo < 1e-12:
        return None
    return (float(hist.iloc[-1]) - lo) / (hi - lo) * 100.0


def _bar_dates(closes: pd.Series) -> list[date]:
    idx = pd.DatetimeIndex(closes.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return [ts.date() for ts in idx.normalize()]


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else datetime.strptime(value[:10], "%Y-%m-%d").date()


def _category(reason: str) -> str:
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", reason)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class _Open:
    spread: CreditSpread
    rating: str
    entry: date
    sigma: float


def simulate_spreads(
    ticker: str,
    closes: pd.Series,
    signals: Sequence[tuple[str, str | None]],
    earnings: Sequence[date],
    risk_cfg: RiskConfig,
    sim_cfg: SpreadSimConfig | None = None,
) -> SpreadBacktest:
    """Replay ``signals`` ((date, rating) pairs) as credit spreads on ``closes``."""
    sim = sim_cfg or SpreadSimConfig()
    dates = _bar_dates(closes)
    prices = [float(p) for p in closes.to_numpy()]
    vol = realized_vol_series(closes.reset_index(drop=True), sim.vol_window)
    earnings = sorted(earnings)

    pending = sorted(((_as_date(d), r) for d, r in signals), key=lambda x: x[0])
    next_signal = 0
    open_: list[_Open] = []
    trades: list[SpreadTrade] = []
    skipped: list[SkippedSignal] = []
    equity = risk_cfg.account_size

    def close(pos: _Open, d: date, spot: float, reason: str, final_bar: bool = False) -> None:
        nonlocal equity
        expired = d >= pos.spread.expiration
        t = max((pos.spread.expiration - d).days, 0) / 365.0
        if expired:
            value = spread_value(pos.spread, spot, 0.0, pos.sigma, sim.risk_free)
            paid = value
        else:
            value = spread_value(pos.spread, spot, t, pos.sigma, sim.risk_free)
            paid = min(value + sim.slippage, pos.spread.width)
        n = pos.spread.contracts
        pnl = (pos.spread.credit - paid) * CONTRACT_MULTIPLIER * n
        pnl -= sim.commission_per_leg * 4 * n
        equity += pnl
        trades.append(SpreadTrade(
            underlying=ticker, side=pos.spread.side, rating=pos.rating,
            entry_date=str(pos.entry), exit_date=str(d),
            short_strike=pos.spread.short_strike, long_strike=pos.spread.long_strike,
            contracts=n, credit=round(pos.spread.credit, 4), exit_value=round(value, 4),
            exit_reason=reason, pnl=round(pnl, 2), max_loss=round(pos.spread.max_loss, 2),
            equity_after=round(equity, 2),
        ))

    for i, (d, spot) in enumerate(zip(dates, prices, strict=True)):
        # 1) manage open positions at today's close
        for pos in list(open_):
            if d >= pos.spread.expiration:
                close(pos, d, spot, "expired")
                open_.remove(pos)
                continue
            t = (pos.spread.expiration - d).days / 365.0
            value = spread_value(pos.spread, spot, t, pos.sigma, sim.risk_free)
            signal = evaluate_exit(pos.spread, value, risk_cfg, d)
            if signal.action != "hold":
                close(pos, d, spot, signal.action)
                open_.remove(pos)

        # 2) act on signals whose date has arrived (first bar on/after the date)
        while next_signal < len(pending) and pending[next_signal][0] <= d:
            sig_date, rating = pending[next_signal]
            next_signal += 1
            if rating in BULLISH:
                side = "put"
            elif rating in BEARISH:
                side = "call"
            else:
                skipped.append(SkippedSignal(str(sig_date), rating, ["no directional rating"]))
                continue
            rv = vol.iloc[i]
            if pd.isna(rv) or rv <= 0:
                skipped.append(SkippedSignal(str(sig_date), rating, ["not enough price history for volatility"]))
                continue
            sigma = float(rv) * sim.iv_markup
            t = sim.target_dte / 365.0
            short = pick_short_strike(spot, t, sigma, sim.risk_free, sim.short_loss_prob, side, sim.strike_step)
            long_ = short - sim.width if side == "put" else short + sim.width
            if long_ <= 0 or short <= 0:
                skipped.append(SkippedSignal(str(sig_date), rating, ["strikes not positive"]))
                continue
            proto = CreditSpread(
                underlying=ticker, side=side, short_strike=short, long_strike=long_,
                credit=0.01, expiration=d + timedelta(days=sim.target_dte),
                contracts=sim.contracts,
            )
            raw = spread_value(proto, spot, t, sigma, sim.risk_free)
            credit = round(raw - sim.slippage, 2)
            if credit <= 0 or credit >= sim.width:
                skipped.append(SkippedSignal(str(sig_date), rating, [f"no workable credit ({credit:.2f})"]))
                continue
            nxt = next((e for e in earnings if e >= d), None)
            spread = replace(
                proto, credit=credit, iv_rank=iv_rank_proxy(vol, i, sim.rank_window, sim.min_rank_history),
                bid_ask=sim.assumed_bid_ask, earnings_date=nxt,
            )
            decision = evaluate_entry(spread, [p.spread for p in open_], replace(risk_cfg, account_size=equity), d)
            if not decision.allowed:
                skipped.append(SkippedSignal(str(sig_date), rating, decision.reasons))
                continue
            open_.append(_Open(replace(spread, contracts=decision.contracts), rating, d, sigma))

    # mark any still-open positions to market on the last bar
    if dates:
        last_d, last_px = dates[-1], prices[-1]
        for pos in list(open_):
            close(pos, last_d, last_px, "end_of_data")
            open_.remove(pos)

    result = SpreadBacktest(ticker, risk_cfg.account_size, trades, skipped)
    result.summary = summarize_spreads(result)
    return result


def summarize_spreads(result: SpreadBacktest) -> dict[str, Any]:
    trades = result.trades
    start = result.initial_equity
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak, max_dd = start, 0.0
    for t in trades:
        peak = max(peak, t.equity_after)
        max_dd = max(max_dd, (peak - t.equity_after) / peak if peak else 0.0)
    total = sum(pnls)
    return {
        "trades": len(trades),
        "skipped": len(result.skipped),
        "win_rate": len(wins) / len(trades) if trades else None,
        "total_pnl": round(total, 2),
        "return_pct": total / start if start else None,
        "ending_equity": round(start + total, 2),
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "expectancy": total / len(trades) if trades else None,
        "max_drawdown": max_dd,
        "exits": dict(Counter(t.exit_reason for t in trades)),
        "skip_reasons": dict(Counter(_category(r) for s in result.skipped for r in s.reasons)),
    }


# ---------------------------------------------------------------------------
# Integration with the walk-forward backtest
# ---------------------------------------------------------------------------

def run_spread_scoring(
    backtest_result: Any,
    *,
    risk_cfg: RiskConfig | None = None,
    sim_cfg: SpreadSimConfig | None = None,
    price_loader: Callable[[str, str, str], pd.Series] | None = None,
    earnings_loader: Callable[[str], list[date]] | None = None,
) -> SpreadBacktest:
    """Re-score a ``BacktestResult``'s ratings as credit-spread income trades."""
    from tradingagents.backtest import fetch_closes
    from tradingagents.dataflows.yfinance_earnings import fetch_earnings_dates

    sim = sim_cfg or SpreadSimConfig()
    cfg = risk_cfg or RiskConfig()
    signals = [(r.trade_date, r.rating) for r in backtest_result.records if r.rating]
    if not signals:
        return SpreadBacktest(backtest_result.ticker, cfg.account_size, [], [])
    first = _as_date(min(d for d, _ in signals))
    last = _as_date(max(d for d, _ in signals))
    start = (first - timedelta(days=int(sim.rank_window * 1.6))).isoformat()
    end = (last + timedelta(days=sim.target_dte + 10)).isoformat()
    closes = (price_loader or fetch_closes)(backtest_result.ticker, start, end)
    earnings = (earnings_loader or fetch_earnings_dates)(backtest_result.ticker)
    return simulate_spreads(backtest_result.ticker, closes, signals, earnings, cfg, sim)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def render_spread_summary(result: SpreadBacktest) -> str:
    s = result.summary
    lines = [
        f"# Credit-spread income backtest: {result.ticker}",
        "",
        "> Synthetic pricing: Black-Scholes with volatility proxied from realized "
        "volatility. A rules sanity check, not a return forecast.",
        "",
        f"- Starting equity: {_usd(result.initial_equity)}  ->  ending {_usd(s.get('ending_equity'))}",
        f"- Trades: {s.get('trades', 0)} (skipped signals: {s.get('skipped', 0)})",
        f"- Win rate: {_pct(s.get('win_rate'))}",
        f"- Total P&L: {_usd(s.get('total_pnl'))} ({_pct(s.get('return_pct'))})",
        f"- Average win / loss: {_usd(s.get('avg_win'))} / {_usd(s.get('avg_loss'))}",
        f"- Expectancy per trade: {_usd(s.get('expectancy'))}",
        f"- Max drawdown: {_pct(s.get('max_drawdown'))}",
    ]
    if s.get("exits"):
        lines += ["", "## Exits"] + [f"- {k}: {v}" for k, v in sorted(s["exits"].items())]
    if s.get("skip_reasons"):
        lines += ["", "## Why signals were skipped"] + [f"- {k}: {v}" for k, v in sorted(s["skip_reasons"].items())]
    return "\n".join(lines) + "\n"


def write_spread_results(result: SpreadBacktest, output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(t) for t in result.trades]).to_csv(out / "spread_trades.csv", index=False)
    (out / "spread_summary.json").write_text(json.dumps(result.summary, indent=2, default=str), encoding="utf-8")
    (out / "spread_summary.md").write_text(render_spread_summary(result), encoding="utf-8")
