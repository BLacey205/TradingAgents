"""Is the agents' directional signal better than chance? An edge-measurement harness.

Takes the ratings from a walk-forward backtest (``decisions.jsonl``) and asks
two questions with real prices:

1. Directional edge: do Buy/Overweight calls rise, and Sell/Underweight calls
   fall, more often than the market's own base rate? (exact binomial test)
2. Spread edge: does replaying the ratings as credit spreads (see
   ``spread_backtest``) earn more than the same trades with the ratings
   shuffled across the same dates? (permutation test)

Read the output with care:

- Overlapping holding windows make hit-rate p-values optimistic; the report
  flags this. Prefer decisions spaced at least ``horizon`` bars apart.
- Run many tickers/periods: a single ticker needs on the order of 150
  independent decisions to confirm even a 60% hit rate (``decisions_needed``).
- Use dates *after* the model's training cutoff. Earlier dates let the model
  recall what happened, which inflates any apparent edge.
- Test several variants and you will find a "significant" one by luck;
  correct for the number of looks.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import NormalDist, mean
from typing import Any

import pandas as pd

from tradingagents.backtest import fetch_closes, forward_return
from tradingagents.risk_gate import RiskConfig
from tradingagents.spread_backtest import (
    BEARISH,
    BULLISH,
    SpreadSimConfig,
    _as_date,
    simulate_spreads,
)

_N = NormalDist()


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), computed in log space."""
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    total = 0.0
    for i in range(k, n + 1):
        log_term = (
            math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p)
        )
        total += math.exp(log_term)
    return min(total, 1.0)


def decisions_needed(hit_rate: float, base_rate: float = 0.5, alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Independent decisions needed to detect ``hit_rate`` > ``base_rate`` (one-sided)."""
    if hit_rate <= base_rate:
        return None
    za, zb = _N.inv_cdf(1 - alpha), _N.inv_cdf(power)
    num = za * math.sqrt(base_rate * (1 - base_rate)) + zb * math.sqrt(hit_rate * (1 - hit_rate))
    return math.ceil((num / (hit_rate - base_rate)) ** 2)


# ---------------------------------------------------------------------------
# Loading decisions
# ---------------------------------------------------------------------------

def load_decisions(results_dir: str | Path) -> tuple[str, list[tuple[str, str]]]:
    """Read ``(ticker, [(date, rating), ...])`` from a backtest output directory."""
    out = Path(results_dir)
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    latest: dict[str, str] = {}
    for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a line cut short by a crash
        if rec.get("error") or not rec.get("rating"):
            latest.pop(rec.get("trade_date"), None)
        else:
            latest[rec["trade_date"]] = rec["rating"]
    return run["ticker"], sorted(latest.items())


# ---------------------------------------------------------------------------
# Directional edge
# ---------------------------------------------------------------------------

def _bar_index(closes: pd.Series, when: str | date) -> int:
    idx = pd.DatetimeIndex(closes.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return int(idx.normalize().searchsorted(pd.Timestamp(_as_date(when))))


def directional_edge(closes: pd.Series, signals: Sequence[tuple[str, str]], horizon: int = 20) -> dict[str, Any]:
    """Hit rates of bullish/bearish calls versus the market's own up/down base rate."""
    dated = [(d, r) for d, r in signals if r in BULLISH | BEARISH]
    if not dated:
        return {"horizon": horizon, "bullish": None, "bearish": None, "overlap": False, "n_calls": 0}

    first = _bar_index(closes, min(d for d, _ in dated))
    last = _bar_index(closes, max(d for d, _ in dated))
    fwd = (closes.shift(-horizon) / closes - 1).iloc[first:last + 1].dropna()
    base_up = float((fwd > 0).mean()) if len(fwd) else 0.5

    def side(ratings: set[str], want_up: bool) -> dict[str, Any] | None:
        rets = []
        for d, r in dated:
            if r not in ratings:
                continue
            ret, _ = forward_return(closes, d, horizon)
            if ret is not None:
                rets.append(ret)
        if not rets:
            return None
        hits = sum(1 for x in rets if (x > 0) == want_up)
        base = base_up if want_up else 1 - base_up
        return {
            "n": len(rets), "hits": hits, "hit_rate": hits / len(rets), "base_rate": base,
            "p_value": binom_sf(hits, len(rets), base), "mean_return": mean(rets),
        }

    positions = sorted(_bar_index(closes, d) for d, _ in dated)
    gaps = [b - a for a, b in zip(positions, positions[1:], strict=False)]
    overlap = bool(gaps) and sorted(gaps)[len(gaps) // 2] < horizon
    return {
        "horizon": horizon, "base_up": base_up, "overlap": overlap, "n_calls": len(dated),
        "bullish": side(BULLISH, True), "bearish": side(BEARISH, False),
    }


# ---------------------------------------------------------------------------
# Spread edge (permutation test)
# ---------------------------------------------------------------------------

def spread_permutation_test(
    ticker: str,
    closes: pd.Series,
    signals: Sequence[tuple[str, str]],
    earnings: Sequence[date],
    risk_cfg: RiskConfig,
    sim_cfg: SpreadSimConfig | None = None,
    n_perm: int = 100,
    seed: int = 0,
) -> dict[str, Any]:
    """Compare the actual spread P&L with the same dates and ratings shuffled."""
    sim = sim_cfg or SpreadSimConfig()

    def total(sigs):
        return simulate_spreads(ticker, closes, sigs, earnings, risk_cfg, sim).summary.get("total_pnl", 0.0)

    actual = total(signals)
    dates = [d for d, _ in signals]
    ratings = [r for _, r in signals]
    rng = random.Random(seed)
    null = []
    for _ in range(n_perm):
        shuffled = ratings[:]
        rng.shuffle(shuffled)
        null.append(total(list(zip(dates, shuffled, strict=True))))
    ge = sum(1 for x in null if x >= actual)
    always_bull = total([(d, "Buy" if r in BULLISH | BEARISH else r) for d, r in signals])
    always_bear = total([(d, "Sell" if r in BULLISH | BEARISH else r) for d, r in signals])
    return {
        "actual_pnl": actual, "null_mean": mean(null) if null else None,
        "null_best": max(null) if null else None, "null_worst": min(null) if null else None,
        "p_value": (ge + 1) / (n_perm + 1), "n_perm": n_perm,
        "always_bullish_pnl": always_bull, "always_bearish_pnl": always_bear,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class EdgeReport:
    ticker: str
    n_signals: int
    directional: dict[str, Any]
    spread: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def _verdict(p: float | None) -> str:
    if p is None:
        return "not enough data"
    if p < 0.05:
        return "statistically significant (p < 0.05) - confirm on other tickers and periods"
    if p < 0.20:
        return "suggestive but not significant"
    return "no evidence of edge"


def evaluate_signal_edge(
    ticker: str,
    signals: Sequence[tuple[str, str]],
    *,
    closes: pd.Series | None = None,
    earnings: Sequence[date] | None = None,
    price_loader: Callable[[str, str, str], pd.Series] | None = None,
    earnings_loader: Callable[[str], list[date]] | None = None,
    horizon: int = 20,
    risk_cfg: RiskConfig | None = None,
    sim_cfg: SpreadSimConfig | None = None,
    n_perm: int = 100,
    seed: int = 0,
) -> EdgeReport:
    """Measure whether ``signals`` ((date, rating) pairs) beat chance on ``ticker``."""
    from datetime import timedelta

    sim = sim_cfg or SpreadSimConfig()
    cfg = risk_cfg or RiskConfig()
    if closes is None:
        first = _as_date(min(d for d, _ in signals))
        last = _as_date(max(d for d, _ in signals))
        start = (first - timedelta(days=int(sim.rank_window * 1.6))).isoformat()
        end = (last + timedelta(days=max(sim.target_dte, horizon * 2) + 10)).isoformat()
        closes = (price_loader or fetch_closes)(ticker, start, end)
    if earnings is None:
        from tradingagents.dataflows.yfinance_earnings import fetch_earnings_dates

        earnings = (earnings_loader or fetch_earnings_dates)(ticker)

    directional = directional_edge(closes, signals, horizon)
    spread = spread_permutation_test(ticker, closes, signals, earnings, cfg, sim, n_perm, seed) if n_perm else None
    notes = []
    if directional.get("overlap"):
        notes.append(
            f"Decisions are spaced closer than the {horizon}-bar horizon, so outcomes overlap "
            "and the hit-rate p-values are optimistic."
        )
    if directional["n_calls"] < 100:
        notes.append(
            f"Only {directional['n_calls']} directional calls: too few to confirm a modest edge. "
            "Pool several tickers and periods."
        )
    notes.append("Use dates after the model's training cutoff; earlier dates leak outcomes into the ratings.")
    return EdgeReport(ticker, len(signals), directional, spread, notes)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def render_edge_report(report: EdgeReport) -> str:
    d = report.directional
    lines = [f"# Signal edge: {report.ticker}", "", f"- Decisions: {report.n_signals} ({d['n_calls']} directional)"]
    for key, label in (("bullish", "Bullish (Buy/Overweight)"), ("bearish", "Bearish (Sell/Underweight)")):
        s = d.get(key)
        if not s:
            lines.append(f"- {label}: no scorable calls")
            continue
        need = decisions_needed(s["hit_rate"], s["base_rate"])
        lines.append(
            f"- {label}: {s['hits']}/{s['n']} correct over {d['horizon']} bars = {_pct(s['hit_rate'])} "
            f"vs {_pct(s['base_rate'])} base rate; mean move {_pct(s['mean_return'])}; "
            f"p = {s['p_value']:.3f} -> {_verdict(s['p_value'])}"
            + (f" (about {need} independent decisions to confirm at this hit rate)" if need else "")
        )
    sp = report.spread
    if sp:
        lines += [
            "",
            "## Spread P&L vs shuffled ratings (synthetic pricing)",
            f"- Actual total P&L: ${sp['actual_pnl']:,.2f}",
            f"- Shuffled ratings ({sp['n_perm']} runs): mean ${sp['null_mean']:,.2f}, "
            f"range ${sp['null_worst']:,.2f} to ${sp['null_best']:,.2f}",
            f"- Permutation p = {sp['p_value']:.3f} -> {_verdict(sp['p_value'])}",
            f"- Baselines: always bullish ${sp['always_bullish_pnl']:,.2f}, "
            f"always bearish ${sp['always_bearish_pnl']:,.2f}",
        ]
    if report.notes:
        lines += ["", "## Caveats"] + [f"- {n}" for n in report.notes]
    return "\n".join(lines) + "\n"
