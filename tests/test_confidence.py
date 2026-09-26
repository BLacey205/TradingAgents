"""Portfolio Manager confidence: schema coercion, rendering, parsing, and the
backtest calibration report built on it."""

import json

import pandas as pd
import pytest

from tradingagents import backtest as bt
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision
from tradingagents.agents.utils.rating import parse_confidence, parse_rating


def _decision(**kwargs):
    return PortfolioDecision(
        rating=kwargs.pop("rating", PortfolioRating.BUY),
        executive_summary="Enter on weakness.",
        investment_thesis="Demand is strong.",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Schema and rendering
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [
    (72, 72), ("72%", 72), (" 72 % ", 72), (72.6, 73), (0.72, 72), ("0.72", 72),
    (1, 1), (0, 0), (100, 100), (140, 100), (-5, 0),
    ("0.72%", 1), ("N/A", None), ("none", None), ("high", None), (None, None), (True, None),
])
def test_confidence_is_normalised_to_a_percentage(raw, expected):
    assert _decision(confidence=raw).confidence == expected


@pytest.mark.unit
def test_confidence_is_optional_for_older_callers():
    assert _decision().confidence is None


@pytest.mark.unit
def test_rendered_decision_carries_confidence_after_the_rating():
    text = render_pm_decision(_decision(rating=PortfolioRating.SELL, confidence=65))
    lines = [line for line in text.splitlines() if line]
    assert lines[0] == "**Rating**: Sell"
    assert lines[1] == "**Confidence**: 65%"
    assert parse_rating(text) == "Sell"
    assert parse_confidence(text) == 65


@pytest.mark.unit
def test_rendered_decision_without_confidence_is_unchanged():
    text = render_pm_decision(_decision())
    assert "Confidence" not in text
    assert text.startswith("**Rating**: Buy\n\n**Executive Summary**")


@pytest.mark.unit
def test_confidence_field_is_documented_for_the_model():
    description = PortfolioDecision.model_fields["confidence"].description
    assert "0 to 100" in description and "calibrated" in description.lower()


# ---------------------------------------------------------------------------
# Parsing free text
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("text, expected", [
    ("**Rating**: Buy\n\n**Confidence**: 80%", 80),
    ("Rating: Hold\nConfidence: 55", 55),
    ("Confidence - **62%**", 62),
    ("confidence: 0.7", 70),
    ("Rating: Buy\nConfidence: 150%", None),
    ("Rating: Buy\nI am fairly confident in this.", None),
    ("", None),
    (None, None),
])
def test_parse_confidence(text, expected):
    assert parse_confidence(text) == expected


@pytest.mark.unit
def test_portfolio_manager_prompt_asks_for_confidence():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "tradingagents" / "agents" / "managers" / "portfolio_manager.py"
    ).read_text(encoding="utf-8")
    assert "**Confidence**" in src


# ---------------------------------------------------------------------------
# Backtest calibration
# ---------------------------------------------------------------------------

class ConfidentGraph:
    """Scripted (rating, confidence) per date, rendered like the real PM."""

    def __init__(self, calls):
        self.calls = calls

    def propagate(self, ticker, trade_date, asset_type="stock"):
        rating, confidence = self.calls[trade_date]
        text = render_pm_decision(_decision(rating=rating, confidence=confidence))
        return {"final_trade_decision": text}, rating

    def _resolve_benchmark(self, ticker):
        return "SPY"


def _closes(values):
    return pd.Series(values, index=pd.bdate_range("2025-01-06", periods=len(values)), dtype=float)


def _run(tmp_path, calls, prices):
    loader = {"NVDA": _closes(prices), "SPY": _closes([50.0] * len(prices))}
    runner = bt.Backtester(
        ConfidentGraph(calls), "NVDA", tmp_path, holding_days=1,
        price_loader=lambda symbol, start, end: loader[symbol],
    )
    return runner.run(list(calls))


@pytest.mark.unit
def test_backtest_records_confidence_and_scores_calibration(tmp_path):
    # Prices go up, down, up, up, flat: one-bar windows starting each day.
    prices = [100, 110, 99, 105, 110, 110]
    dates = list(pd.bdate_range("2025-01-06", periods=5).strftime("%Y-%m-%d"))
    calls = dict(zip(dates, [
        ("Buy", 90),    # up   -> right
        ("Buy", 85),    # down -> wrong
        ("Sell", 60),   # up   -> wrong
        ("Buy", 55),    # up   -> right
        ("Hold", 70),   # Hold is not directional: excluded
    ], strict=True))
    result = _run(tmp_path, calls, prices)
    s = result.summary

    assert [r.confidence for r in result.records] == [90, 85, 60, 55, 70]
    assert s["confidence_calls"] == 4
    assert s["avg_confidence"] == pytest.approx((0.90 + 0.85 + 0.60 + 0.55) / 4)
    assert s["confidence_hit_rate"] == pytest.approx(0.5)
    expected_brier = ((0.9 - 1) ** 2 + (0.85 - 0) ** 2 + (0.6 - 0) ** 2 + (0.55 - 1) ** 2) / 4
    assert s["brier_score"] == pytest.approx(expected_brier)
    assert [(row["range"], row["calls"], row["hit_rate"]) for row in s["calibration"]] == [
        ("50-59", 1, 1.0), ("60-69", 1, 0.0), ("80-89", 1, 0.0), ("90-100", 1, 1.0),
    ]

    md = (tmp_path / "summary.md").read_text()
    assert "## Confidence calibration" in md
    assert "overconfident by 23 points" in md
    assert "too few to judge" in md
    assert json.loads((tmp_path / "summary.json").read_text())["confidence_calls"] == 4


@pytest.mark.unit
def test_calibration_verdicts():
    def summary(conf, hits):
        records = [
            bt.DecisionRecord(
                trade_date=f"2025-01-{i + 1:02d}", rating="Buy", confidence=conf,
                exposure=1.0, raw_return=0.01 if i < hits else -0.01,
            )
            for i in range(20)
        ]
        s = {**bt.calibration(records)}
        return "\n".join(bt._render_calibration(s))

    assert "well calibrated" in summary(70, 14)
    assert "underconfident by 30 points" in summary(50, 16)
    assert "too few" not in summary(70, 14)


@pytest.mark.unit
def test_no_confidence_means_no_calibration_section(tmp_path):
    class Plain(ConfidentGraph):
        def propagate(self, ticker, trade_date, asset_type="stock"):
            return {"final_trade_decision": "**Rating**: Buy"}, "Buy"

    runner = bt.Backtester(
        Plain({}), "NVDA", tmp_path, holding_days=1,
        price_loader=lambda *a: _closes([100, 101]),
    )
    result = runner.run(["2025-01-06"])
    assert result.records[0].confidence is None
    assert result.summary["confidence_calls"] == 0
    assert "calibration" not in (tmp_path / "summary.md").read_text().lower()


@pytest.mark.unit
def test_progress_from_before_confidence_still_resumes(tmp_path):
    # decisions.jsonl written before the confidence field existed.
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"trade_date": "2025-01-06", "rating": "Buy"}) + "\n"
    )
    runner = bt.Backtester(
        ConfidentGraph({}), "NVDA", tmp_path, holding_days=1,
        price_loader=lambda *a: _closes([100, 101]),
    )
    result = runner.run(["2025-01-06"])
    assert result.records[0].rating == "Buy"
    assert result.records[0].confidence is None
