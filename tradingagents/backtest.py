"""Walk-forward backtest runner for TradingAgents.

Runs the full agent graph for one ticker on a schedule of historical dates,
records each rating, then scores the calls against realized forward returns:

- each rating maps to a target exposure (Buy +1 ... Sell -1),
- the position is entered at the close of the trade date (the last bar the
  agents could see) and held for ``holding_days`` price bars,
- the scorecard reports hit rates, per-decision and compounded returns versus
  the benchmark, max drawdown, and LLM usage.

Progress is appended to ``decisions.jsonl`` after every date, so an interrupted
run resumes where it stopped when pointed at the same output directory. Scoring
happens once at the end, from one price download per symbol.

The memory log is disabled by default: its deferred reflection resolves past
decisions with *live* prices, which would leak outcomes after the simulated
date into later decisions and pollute the user's real decision log.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_confidence

logger = logging.getLogger(__name__)

# Target exposure per rating: fraction of capital long (+) or short (-).
RATING_EXPOSURE: dict[str, float] = {
    "Buy": 1.0,
    "Overweight": 0.5,
    "Hold": 0.0,
    "Underweight": -0.5,
    "Sell": -1.0,
}

FREQUENCIES = ("daily", "weekly", "monthly")

PriceLoader = Callable[[str, str, str], pd.Series]


# ---------------------------------------------------------------------------
# Schedule and prices
# ---------------------------------------------------------------------------

def trade_dates(start: str, end: str, frequency: str = "weekly") -> list[str]:
    """Business-day schedule between ``start`` and ``end`` (inclusive).

    ``weekly`` repeats on the first business day's weekday (so windows sit
    exactly five business days apart) and ``monthly`` takes the first business
    day of each month. Exchange holidays are not removed: the agents still run
    on data up to that date, and scoring enters at the next available bar.
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"frequency must be one of {FREQUENCIES}, got {frequency!r}")
    days = pd.bdate_range(start=start, end=end)
    if frequency == "weekly" and len(days):
        weekday = days[0].day_name()[:3].upper()
        days = pd.date_range(start=days[0], end=end, freq=f"W-{weekday}")
    elif frequency == "monthly":
        days = pd.DatetimeIndex(pd.Series(days).groupby(days.to_period("M")).first())
    return [d.strftime("%Y-%m-%d") for d in days]


def fetch_closes(symbol: str, start: str, end: str) -> pd.Series:
    """Daily closes for ``symbol`` from ``start`` up to (excluding) ``end``."""
    import yfinance as yf

    from tradingagents.dataflows.symbol_utils import normalize_symbol

    history = yf.Ticker(normalize_symbol(symbol)).history(start=start, end=end)
    return history["Close"] if "Close" in history else pd.Series(dtype=float)


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def forward_return(
    closes: pd.Series, trade_date: str, holding_days: int,
) -> tuple[float | None, int | None]:
    """Return from the first close on/after ``trade_date`` to ``holding_days`` bars later.

    Returns ``(None, None)`` when the entry bar or the full holding window is
    not in ``closes`` yet (too recent, delisted, or missing data), so a partial
    window is never scored as if it were complete.
    """
    if closes is None or len(closes) == 0:
        return None, None
    dates = _naive_dates(closes.index)
    entry = int(dates.searchsorted(pd.Timestamp(trade_date)))
    exit_ = entry + holding_days
    if exit_ >= len(closes):
        return None, None
    start_px = float(closes.iloc[entry])
    end_px = float(closes.iloc[exit_])
    if not start_px or math.isnan(start_px) or math.isnan(end_px):
        return None, None
    return (end_px - start_px) / start_px, holding_days


# ---------------------------------------------------------------------------
# Records and scoring
# ---------------------------------------------------------------------------

@dataclass
class DecisionRecord:
    """One scheduled date: the agents' call and, once scored, its outcome."""

    trade_date: str
    rating: str | None = None
    confidence: int | None = None
    error: str | None = None
    seconds: float | None = None
    llm_calls: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Filled in by scoring.
    exposure: float | None = None
    raw_return: float | None = None
    benchmark_return: float | None = None
    alpha: float | None = None
    strategy_return: float | None = None
    holding_days: int | None = None


@dataclass
class BacktestResult:
    ticker: str
    benchmark: str
    holding_days: int
    long_only: bool
    records: list[DecisionRecord]
    summary: dict[str, Any] = field(default_factory=dict)
    output_dir: Path | None = None


def rating_exposure(rating: str | None, long_only: bool = False) -> float | None:
    if rating not in RATING_EXPOSURE:
        return None
    exposure = RATING_EXPOSURE[rating]
    return max(exposure, 0.0) if long_only else exposure


def _max_drawdown(returns: list[float]) -> float:
    equity = np.cumprod([1.0 + r for r in returns])
    peaks = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    return float(np.min(equity / peaks - 1.0)) if len(equity) else 0.0


def _mean(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _compound(values: list[float]) -> float | None:
    return float(np.prod([1.0 + v for v in values]) - 1.0) if values else None


def _min_spacing_bdays(dates: list[str]) -> int | None:
    if len(dates) < 2:
        return None
    ordered = sorted(np.datetime64(d) for d in dates)
    return int(min(np.busday_count(a, b) for a, b in zip(ordered, ordered[1:], strict=False)))


# Stated-confidence buckets for the calibration table: [low, high).
CONFIDENCE_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101),
)


def calibration(records: list[DecisionRecord]) -> dict[str, Any]:
    """Compare stated confidence with how often directional calls were right.

    Only scored Buy/Overweight/Underweight/Sell calls that carry a confidence
    count: a call is right when the price moved in the called direction over
    the holding window. Hold has no direction to check, so it is left out.
    """
    calls = [
        r for r in records
        if r.exposure and r.raw_return is not None and r.confidence is not None
    ]
    if not calls:
        return {"confidence_calls": 0, "avg_confidence": None, "confidence_hit_rate": None,
                "brier_score": None, "calibration": []}

    def hit(r: DecisionRecord) -> float:
        return 1.0 if r.exposure * r.raw_return > 0 else 0.0

    rows = []
    for low, high in CONFIDENCE_BUCKETS:
        bucket = [r for r in calls if low <= r.confidence < high]
        if bucket:
            rows.append({
                "range": f"{low}-{min(high - 1, 100)}",
                "calls": len(bucket),
                "avg_confidence": _mean([r.confidence / 100 for r in bucket]),
                "hit_rate": _mean([hit(r) for r in bucket]),
            })
    return {
        "confidence_calls": len(calls),
        "avg_confidence": _mean([r.confidence / 100 for r in calls]),
        "confidence_hit_rate": _mean([hit(r) for r in calls]),
        # Mean squared gap between stated probability and outcome: 0 is perfect,
        # 0.25 is what always saying 50% scores.
        "brier_score": _mean([(r.confidence / 100 - hit(r)) ** 2 for r in calls]),
        "calibration": rows,
    }


def summarize(records: list[DecisionRecord], holding_days: int) -> dict[str, Any]:
    """Aggregate scored records into the backtest scorecard."""
    decided = [r for r in records if r.rating and not r.error]
    scored = sorted(
        (r for r in decided if r.strategy_return is not None),
        key=lambda r: r.trade_date,
    )
    active = [r for r in scored if r.exposure]
    strategy = [r.strategy_return for r in scored]
    bench = [r.benchmark_return for r in scored if r.benchmark_return is not None]

    def _hits(attr: str) -> float | None:
        vals = [r for r in active if getattr(r, attr) is not None]
        if not vals:
            return None
        return sum(1 for r in vals if r.exposure * getattr(r, attr) > 0) / len(vals)

    spacing = _min_spacing_bdays([r.trade_date for r in scored])
    overlapping = spacing is not None and spacing < holding_days

    def _total(attr: str) -> int:
        return sum(getattr(r, attr) or 0 for r in records)

    return {
        "scheduled": len(records),
        "decisions": len(decided),
        "errors": sum(1 for r in records if r.error),
        "scored": len(scored),
        "unscored": len(decided) - len(scored),
        "rating_counts": dict(Counter(r.rating for r in decided)),
        "active_calls": len(active),
        "hit_rate": _hits("raw_return"),
        "alpha_hit_rate": _hits("alpha"),
        "avg_strategy_return": _mean(strategy),
        "avg_benchmark_return": _mean(bench),
        "avg_alpha": _mean([r.alpha for r in scored if r.alpha is not None]),
        "cumulative_strategy_return": _compound(strategy),
        "cumulative_benchmark_return": _compound(bench),
        "max_drawdown": _max_drawdown(strategy) if strategy else None,
        "overlapping_windows": overlapping,
        **calibration(scored),
        "llm_calls": _total("llm_calls"),
        "tokens_in": _total("tokens_in"),
        "tokens_out": _total("tokens_out"),
        "seconds": round(sum(r.seconds or 0.0 for r in records), 1),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Backtester:
    """Run a graph over a date schedule and score the resulting calls.

    ``graph`` is anything with ``propagate(ticker, date, asset_type=...)``
    returning ``(final_state, rating)``, normally a ``TradingAgentsGraph``.
    ``stats`` is an optional object exposing cumulative ``llm_calls``,
    ``tokens_in`` and ``tokens_out`` (e.g. the CLI's ``StatsCallbackHandler``
    registered as a graph callback); per-date usage is recorded as deltas.
    """

    PROGRESS_FILE = "decisions.jsonl"
    RUN_FILE = "run.json"

    def __init__(
        self,
        graph: Any,
        ticker: str,
        output_dir: str | Path,
        *,
        holding_days: int = 5,
        benchmark: str | None = None,
        asset_type: str = "stock",
        long_only: bool = False,
        stats: Any = None,
        price_loader: PriceLoader | None = None,
        continue_on_error: bool = True,
        on_progress: Callable[[DecisionRecord, int, int], None] | None = None,
        run_settings: dict[str, Any] | None = None,
    ):
        if holding_days < 1:
            raise ValueError(f"holding_days must be >= 1, got {holding_days}")
        self.graph = graph
        self.ticker = ticker
        self.output_dir = Path(output_dir)
        self.holding_days = holding_days
        self.asset_type = asset_type
        self.long_only = long_only
        self.stats = stats
        self.price_loader = price_loader
        self.continue_on_error = continue_on_error
        self.on_progress = on_progress
        # Anything that changes the ratings (ticker, models, analysts, ...):
        # recorded in run.json so a resume never mixes calls from different setups.
        self.run_settings = {"ticker": ticker, "asset_type": asset_type, **(run_settings or {})}
        if benchmark is None:
            resolve = getattr(graph, "_resolve_benchmark", None)
            benchmark = resolve(ticker) if resolve else "SPY"
        self.benchmark = benchmark

    # -- progress log -------------------------------------------------------

    @property
    def progress_path(self) -> Path:
        return self.output_dir / self.PROGRESS_FILE

    def _check_run_settings(self) -> None:
        path = self.output_dir / self.RUN_FILE
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous != self.run_settings:
                changed = sorted(
                    k for k in previous.keys() | self.run_settings.keys()
                    if previous.get(k) != self.run_settings.get(k)
                )
                raise ValueError(
                    f"{self.output_dir} holds a backtest with different settings "
                    f"({', '.join(changed)}); resuming would mix their ratings. "
                    "Use a different output directory."
                )
        else:
            path.write_text(json.dumps(self.run_settings, indent=2), encoding="utf-8")

    def load_progress(self) -> dict[str, DecisionRecord]:
        """Completed dates from a previous run in this directory (errors retried)."""
        done: dict[str, DecisionRecord] = {}
        if not self.progress_path.exists():
            return done
        for line in self.progress_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = DecisionRecord(**json.loads(line))
            except (ValueError, TypeError):
                # A line cut short by a crash mid-write: that date just reruns.
                logger.warning("Skipping unreadable progress line in %s", self.progress_path)
                continue
            if rec.error:
                done.pop(rec.trade_date, None)
            else:
                done[rec.trade_date] = rec
        return done

    def _append_progress(self, record: DecisionRecord) -> None:
        with open(self.progress_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def _usage(self) -> tuple[int, int, int]:
        s = self.stats
        if s is None:
            return 0, 0, 0
        return (
            getattr(s, "llm_calls", 0),
            getattr(s, "tokens_in", 0),
            getattr(s, "tokens_out", 0),
        )

    # -- run ------------------------------------------------------------------

    def run_date(self, trade_date: str) -> DecisionRecord:
        record = DecisionRecord(trade_date=trade_date)
        before = self._usage()
        started = time.monotonic()
        try:
            state, rating = self.graph.propagate(
                self.ticker, trade_date, asset_type=self.asset_type,
            )
            if rating not in RATINGS_5_TIER:
                raise ValueError(f"unrecognised rating {rating!r}")
            record.rating = rating
            if isinstance(state, dict):
                record.confidence = parse_confidence(state.get("final_trade_decision", ""))
        except Exception as exc:
            if not self.continue_on_error:
                raise
            logger.warning("Backtest %s on %s failed: %s", self.ticker, trade_date, exc)
            record.error = f"{type(exc).__name__}: {exc}"
        record.seconds = round(time.monotonic() - started, 2)
        after = self._usage()
        record.llm_calls, record.tokens_in, record.tokens_out = (
            a - b for a, b in zip(after, before, strict=True)
        )
        return record

    def run(self, dates: Iterable[str]) -> BacktestResult:
        dates = list(dates)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._check_run_settings()
        done = self.load_progress()
        records: list[DecisionRecord] = []
        for i, trade_date in enumerate(dates, 1):
            if trade_date in done:
                record = done[trade_date]
            else:
                record = self.run_date(trade_date)
                self._append_progress(record)
            records.append(record)
            if self.on_progress:
                self.on_progress(record, i, len(dates))
        return self.score(records)

    def score(self, records: list[DecisionRecord]) -> BacktestResult:
        """Attach forward returns to each record and write the result files."""
        closes, bench = self._load_prices([r.trade_date for r in records])
        for r in records:
            r.exposure = rating_exposure(r.rating, self.long_only)
            if r.exposure is None:
                continue
            raw, days = forward_return(closes, r.trade_date, self.holding_days)
            b_ret, _ = forward_return(bench, r.trade_date, self.holding_days)
            if raw is None or b_ret is None:
                continue
            r.raw_return, r.benchmark_return, r.holding_days = raw, b_ret, days
            r.alpha = raw - b_ret
            r.strategy_return = r.exposure * raw

        result = BacktestResult(
            ticker=self.ticker,
            benchmark=self.benchmark,
            holding_days=self.holding_days,
            long_only=self.long_only,
            records=records,
            summary=summarize(records, self.holding_days),
            output_dir=self.output_dir,
        )
        write_results(result)
        return result

    def _load_prices(self, dates: list[str]) -> tuple[pd.Series, pd.Series]:
        if not dates:
            empty = pd.Series(dtype=float)
            return empty, empty
        start = min(dates)
        # Enough calendar slack for the holding window plus weekends/holidays.
        end_dt = datetime.strptime(max(dates), "%Y-%m-%d") + timedelta(
            days=self.holding_days * 2 + 10
        )
        end = end_dt.strftime("%Y-%m-%d")
        closes = self._safe_load(self.ticker, start, end)
        bench = self._safe_load(self.benchmark, start, end)
        return closes, bench

    def _safe_load(self, symbol: str, start: str, end: str) -> pd.Series:
        try:
            return (self.price_loader or fetch_closes)(symbol, start, end)
        except Exception as exc:
            logger.warning("Could not load prices for %s: %s", symbol, exc)
            return pd.Series(dtype=float)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def render_summary(result: BacktestResult) -> str:
    s = result.summary
    counts = ", ".join(
        f"{r} {s['rating_counts'][r]}" for r in RATINGS_5_TIER if r in s["rating_counts"]
    ) or "none"
    lines = [
        f"# Backtest: {result.ticker} vs {result.benchmark}",
        "",
        f"Holding period: {result.holding_days} bars"
        + (" (long-only)" if result.long_only else ""),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Scheduled dates | {s['scheduled']} |",
        f"| Decisions (errors) | {s['decisions']} ({s['errors']}) |",
        f"| Scored / not yet scorable | {s['scored']} / {s['unscored']} |",
        f"| Ratings | {counts} |",
        f"| Hit rate (direction vs price) | {_rate(s['hit_rate'])} of {s['active_calls']} active calls |",
        f"| Hit rate (direction vs benchmark) | {_rate(s['alpha_hit_rate'])} |",
        f"| Avg return per decision | {_pct(s['avg_strategy_return'])} |",
        f"| Avg benchmark return per window | {_pct(s['avg_benchmark_return'])} |",
        f"| Cumulative strategy return | {_pct(s['cumulative_strategy_return'])} |",
        f"| Cumulative benchmark return | {_pct(s['cumulative_benchmark_return'])} |",
        f"| Max drawdown | {_pct(s['max_drawdown'])} |",
        f"| LLM calls / tokens in / out | {s['llm_calls']} / {s['tokens_in']} / {s['tokens_out']} |",
        f"| Agent run time | {s['seconds']}s |",
    ]
    if s["overlapping_windows"]:
        lines += [
            "",
            "> Holding windows overlap (dates are closer together than the holding "
            "period), so cumulative figures and drawdown double-count returns. Use a "
            "sparser schedule or a shorter holding period for a compounding view.",
        ]
    lines += _render_calibration(s)
    lines += [
        "",
        "Positions enter at the trade date's close and exit after the holding period. "
        "No transaction costs or slippage are modelled, news and social sources "
        "return current content even for past dates, and LLM output varies between "
        "runs, so treat one backtest as a single sample.",
    ]
    return "\n".join(lines) + "\n"


def _render_calibration(s: dict[str, Any]) -> list[str]:
    if not s.get("confidence_calls"):
        return []
    gap = s["avg_confidence"] - s["confidence_hit_rate"]
    if abs(gap) < 0.05:
        verdict = "well calibrated"
    elif gap > 0:
        verdict = f"overconfident by {gap * 100:.0f} points"
    else:
        verdict = f"underconfident by {-gap * 100:.0f} points"
    lines = [
        "",
        "## Confidence calibration",
        "",
        f"Across {s['confidence_calls']} directional calls the agents stated "
        f"{_rate(s['avg_confidence'])} confidence on average and were right "
        f"{_rate(s['confidence_hit_rate'])} of the time: {verdict}. "
        f"Brier score {s['brier_score']:.3f} (0 is perfect; always saying 50% scores 0.250).",
        "",
        "| Stated confidence | Calls | Avg stated | Actually right |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {row['range']}% | {row['calls']} | {_rate(row['avg_confidence'])} | "
        f"{_rate(row['hit_rate'])} |"
        for row in s["calibration"]
    ]
    if s["confidence_calls"] < 20:
        lines += ["", f"> Only {s['confidence_calls']} calls: too few to judge calibration reliably."]
    return lines


def write_results(result: BacktestResult) -> None:
    out = result.output_dir
    if out is None:
        return
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(r) for r in result.records]).to_csv(out / "results.csv", index=False)
    meta = {
        "ticker": result.ticker,
        "benchmark": result.benchmark,
        "holding_days": result.holding_days,
        "long_only": result.long_only,
        **result.summary,
    }
    (out / "summary.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render_summary(result), encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

# Config keys that change what the agents decide; a resume must match them.
_RATING_SETTINGS = (
    "llm_provider", "deep_think_llm", "quick_think_llm", "backend_url",
    "max_debate_rounds", "max_risk_discuss_rounds", "output_language",
    "temperature", "google_thinking_level", "openai_reasoning_effort",
    "anthropic_effort", "memory_log_path",
)


def backtest_config(config: dict, use_memory: bool = False) -> dict:
    """Copy ``config`` for a backtest; the memory log is off unless requested."""
    cfg = dict(config)
    if not use_memory:
        cfg["memory_log_path"] = None
    return cfg


def default_output_dir(config: dict, ticker: str, start: str, end: str) -> Path:
    from tradingagents.dataflows.utils import safe_ticker_component

    name = f"{safe_ticker_component(ticker)}_{start}_{end}"
    return Path(config["results_dir"]) / "backtests" / name


def run_backtest(
    ticker: str,
    start: str,
    end: str,
    *,
    frequency: str = "weekly",
    holding_days: int = 5,
    config: dict | None = None,
    selected_analysts: Iterable[str] = ("market", "social", "news", "fundamentals"),
    asset_type: str = "stock",
    benchmark: str | None = None,
    long_only: bool = False,
    use_memory: bool = False,
    output_dir: str | Path | None = None,
    on_progress: Callable[[DecisionRecord, int, int], None] | None = None,
) -> BacktestResult:
    """Build a ``TradingAgentsGraph`` and backtest ``ticker`` from ``start`` to ``end``."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    selected_analysts = list(selected_analysts)
    dates = trade_dates(start, end, frequency)
    if not dates:
        raise ValueError(f"no business days between {start} and {end}")
    cfg = backtest_config(config or DEFAULT_CONFIG, use_memory=use_memory)

    stats = UsageCounter()
    graph = TradingAgentsGraph(
        selected_analysts=selected_analysts, config=cfg, callbacks=[stats],
    )
    runner = Backtester(
        graph,
        ticker,
        output_dir or default_output_dir(cfg, ticker, start, end),
        holding_days=holding_days,
        benchmark=benchmark,
        asset_type=asset_type,
        long_only=long_only,
        stats=stats,
        on_progress=on_progress,
        run_settings={
            "analysts": selected_analysts,
            **{k: cfg.get(k) for k in _RATING_SETTINGS},
        },
    )
    return runner.run(dates)


class UsageCounter(BaseCallbackHandler):
    """Minimal cumulative LLM-call and token counter for per-date usage."""

    def __init__(self) -> None:
        super().__init__()
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.llm_calls += 1

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self.llm_calls += 1

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            usage = response.generations[0][0].message.usage_metadata or {}
        except (AttributeError, IndexError, TypeError):
            return
        self.tokens_in += usage.get("input_tokens", 0)
        self.tokens_out += usage.get("output_tokens", 0)
