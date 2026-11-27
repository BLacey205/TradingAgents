"""Analysis jobs for the web app: one TradingAgents run at a time, progress by stage,
results saved per ticker per day, and follow-up questions answered from a saved run."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.agents.utils.rating import parse_confidence, parse_rating

STAGES = [
    ("analysts", "Analysts gather data"),
    ("research", "Bull vs. bear debate"),
    ("trader", "Trader drafts a plan"),
    ("risk", "Risk team weighs in"),
    ("decision", "Portfolio manager decides"),
]
EXPECTED_CALLS = 18  # a full four-analyst run makes roughly this many model calls


def stage_of(node: str | None) -> str | None:
    if not node:
        return None
    if node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
        return "risk"
    if node == "Portfolio Manager":
        return "decision"
    if node == "Trader":
        return "trader"
    if node in ("Bull Researcher", "Bear Researcher", "Research Manager"):
        return "research"
    if "Analyst" in node or node.startswith("tools_"):
        return "analysts"
    return None


class ProgressTracker(BaseCallbackHandler):
    """Counts model calls and notes which pipeline stage is running."""

    def __init__(self, on_progress: Callable[[str | None, int], None]):
        super().__init__()
        self.calls = 0
        self._on_progress = on_progress

    def _seen(self, metadata: dict | None) -> None:
        self.calls += 1
        self._on_progress(stage_of((metadata or {}).get("langgraph_node")), self.calls)

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs) -> None:
        self._seen(metadata)

    def on_llm_start(self, serialized, prompts, *, metadata=None, **kwargs) -> None:
        self._seen(metadata)


def reports_from_state(state: dict) -> list[dict]:
    """The readable pieces of a finished run, in pipeline order."""
    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    pieces = [
        ("Market analyst", state.get("market_report")),
        ("Sentiment analyst", state.get("sentiment_report")),
        ("News analyst", state.get("news_report")),
        ("Fundamentals analyst", state.get("fundamentals_report")),
        ("Bull case", debate.get("bull_history")),
        ("Bear case", debate.get("bear_history")),
        ("Research manager", debate.get("judge_decision")),
        ("Trader", state.get("trader_investment_plan")),
        ("Aggressive risk view", risk.get("aggressive_history")),
        ("Conservative risk view", risk.get("conservative_history")),
        ("Neutral risk view", risk.get("neutral_history")),
    ]
    return [{"title": t, "text": x} for t, x in pieces if x]


@dataclass
class Job:
    id: str
    ticker: str
    asset_type: str
    trade_date: str
    status: str = "queued"  # queued | running | done | error
    stage: str | None = None
    calls: int = 0
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    cached: bool = False

    def view(self, position: int | None = None) -> dict:
        done_stages = []
        if self.stage:
            order = [s for s, _ in STAGES]
            done_stages = order[: order.index(self.stage)]
        if self.status == "done":
            done_stages = [s for s, _ in STAGES]
        elapsed = None
        if self.started:
            elapsed = round((self.finished or time.time()) - self.started)
        return {
            "id": self.id,
            "ticker": self.ticker,
            "asset_type": self.asset_type,
            "date": self.trade_date,
            "status": self.status,
            "stage": self.stage,
            "stages": [
                {"key": k, "label": label, "done": k in done_stages, "active": k == self.stage}
                for k, label in STAGES
            ],
            "calls": self.calls,
            "progress": 1.0 if self.status == "done" else min(0.95, self.calls / EXPECTED_CALLS),
            "elapsed_seconds": elapsed,
            "queue_position": position,
            "error": self.error,
            "cached": self.cached,
        }


@dataclass
class JobManager:
    """Runs analyses on one background worker so a free-tier API key isn't flooded."""

    data_dir: Path
    graph_factory: Callable[[str, list], Any]  # (asset_type, callbacks) -> graph
    ask_model: Callable[[str, str], str]  # (system, user) -> answer text
    model_label: str = ""
    jobs: dict[str, Job] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    # -- storage ---------------------------------------------------------------

    def _path(self, ticker: str, trade_date: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in ticker)
        return self.data_dir / f"{trade_date}_{safe}.json"

    def load(self, ticker: str, trade_date: str) -> dict | None:
        path = self._path(ticker, trade_date)
        return json.loads(path.read_text()) if path.exists() else None

    def history(self, limit: int = 30) -> list[dict]:
        out = []
        for path in sorted(
            self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                a = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({k: a.get(k) for k in ("ticker", "date", "rating", "confidence", "model")})
            if len(out) >= limit:
                break
        return out

    # -- jobs ------------------------------------------------------------------

    def submit(self, ticker: str, asset_type: str, refresh: bool = False) -> Job:
        today = date.today().isoformat()
        with self._lock:
            for job in self.jobs.values():
                if (
                    job.ticker == ticker
                    and job.trade_date == today
                    and job.status in ("queued", "running")
                ):
                    return job
            job = Job(uuid.uuid4().hex[:12], ticker, asset_type, today)
            if not refresh and self.load(ticker, today):
                job.status, job.cached = "done", True
                self.jobs[job.id] = job
                return job
            self.jobs[job.id] = job
        self._queue.put(job.id)
        return job

    def view(self, job_id: str) -> dict | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            waiting = [j for j in self.jobs.values() if j.status == "queued"]
            position = waiting.index(job) + 1 if job in waiting else None
            return job.view(position)

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.jobs[job_id]
            job.status, job.started = "running", time.time()

            def progress(stage, calls, job=job):
                job.calls = calls
                if stage:
                    job.stage = stage

            try:
                graph = self.graph_factory(job.asset_type, [ProgressTracker(progress)])
                state, rating = graph.propagate(
                    job.ticker, job.trade_date, asset_type=job.asset_type
                )
                decision = state.get("final_trade_decision", "")
                result = {
                    "ticker": job.ticker,
                    "asset_type": job.asset_type,
                    "date": job.trade_date,
                    "rating": parse_rating(decision, default=str(rating or "Hold")),
                    "confidence": parse_confidence(decision),
                    "decision": decision,
                    "reports": reports_from_state(state),
                    "model": self.model_label,
                    "llm_calls": job.calls,
                    "seconds": round(time.time() - job.started),
                }
                self._path(job.ticker, job.trade_date).write_text(json.dumps(result, indent=1))
                job.status = "done"
            except Exception as exc:  # report every failure to the page, keep the worker alive
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"[:500]
            finally:
                job.finished = time.time()
                self._queue.task_done()

    # -- follow-up questions -------------------------------------------------------

    def ask(self, ticker: str, trade_date: str, question: str) -> str:
        analysis = self.load(ticker, trade_date)
        if not analysis:
            raise LookupError(f"No saved analysis of {ticker} for {trade_date}.")
        parts = [f"## Final decision\n{analysis['decision']}"]
        parts += [f"## {r['title']}\n{r['text']}" for r in analysis["reports"]]
        context = "\n\n".join(parts)[:60000]
        system = (
            "You answer questions about one saved stock or crypto analysis produced by a team "
            "of AI agents. Use only the analysis below. If it doesn't cover the question, say so. "
            "Answer in plain language in at most 120 words. This is research, not financial "
            "advice: never tell the reader to buy or sell.\n\n"
            f"Ticker: {ticker}, analysis date: {trade_date}, rating: {analysis['rating']}.\n\n"
            f"{context}"
        )
        return self.ask_model(system, question.strip()[:500])
