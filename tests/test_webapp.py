"""Web app: ticker parsing, the job queue, the JSON API, auth and static serving."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from webapp.jobs import JobManager, stage_of
from webapp.server import make_handler, track_record
from webapp.tickers import parse_ticker

DECISION = "**Rating**: Overweight\n\n**Confidence**: 64% (most analysts agree)\n\n**Executive Summary**: Add slowly."


class FakeGraph:
    """Calls the progress callback like the real graph's LLMs would, then finishes."""

    def __init__(self, callbacks, fail=False):
        self.callbacks = callbacks
        self.fail = fail

    def propagate(self, ticker, trade_date, asset_type="stock"):
        for node in (
            "Market Analyst",
            "Bull Researcher",
            "Trader",
            "Neutral Analyst",
            "Portfolio Manager",
        ):
            for cb in self.callbacks:
                cb.on_chat_model_start({}, [], metadata={"langgraph_node": node})
        if self.fail:
            raise RuntimeError("provider quota exhausted")
        state = {
            "market_report": "## Trend\n- up",
            "investment_debate_state": {
                "bull_history": "Bull says yes",
                "judge_decision": "Go long",
            },
            "risk_debate_state": {"neutral_history": "Balanced"},
            "trader_investment_plan": "Buy in thirds",
            "final_trade_decision": DECISION,
        }
        return state, "Overweight"


@pytest.fixture
def manager(tmp_path):
    asked = []

    def ask(system, question):
        asked.append((system, question))
        return f"Answer to: {question}"

    m = JobManager(tmp_path, lambda asset, cbs: FakeGraph(cbs), ask, model_label="fake · model")
    m.asked = asked
    return m


def _wait(manager, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = manager.view(job_id)
        if view["status"] in ("done", "error"):
            return view
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.mark.unit
@pytest.mark.parametrize(
    "text, expected",
    [
        ("nvda", ("NVDA", "stock")),
        ("$TSLA", ("TSLA", "stock")),
        ("https://finance.yahoo.com/quote/NVDA/", ("NVDA", "stock")),
        ("https://robinhood.com/us/en/stocks/AAPL/", ("AAPL", "stock")),
        ("https://robinhood.com/crypto/BTC", ("BTC-USD", "crypto")),
        ("https://www.tradingview.com/symbols/NASDAQ-NVDA/", ("NVDA", "stock")),
        ("https://www.google.com/finance/quote/MSFT:NASDAQ", ("MSFT", "stock")),
        ("https://www.coinbase.com/price/ethereum", ("ETH-USD", "crypto")),
        ("BTCUSD", ("BTC-USD", "crypto")),
        ("0700.HK", ("0700.HK", "stock")),
    ],
)
def test_parse_ticker(text, expected):
    assert parse_ticker(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "drop table", "https://example.com/page", "<script>"])
def test_parse_ticker_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_ticker(text)


@pytest.mark.unit
def test_stage_mapping_puts_risk_analysts_in_the_risk_stage():
    assert stage_of("Market Analyst") == "analysts"
    assert stage_of("Aggressive Analyst") == "risk"
    assert stage_of("Research Manager") == "research"
    assert stage_of("Portfolio Manager") == "decision"
    assert stage_of(None) is None


@pytest.mark.unit
def test_job_runs_saves_and_is_reused_the_same_day(manager):
    job = manager.submit("NVDA", "stock")
    view = _wait(manager, job.id)
    assert view["status"] == "done" and view["progress"] == 1.0
    saved = manager.load("NVDA", view["date"])
    assert saved["rating"] == "Overweight"
    assert saved["confidence"] == 64
    assert [r["title"] for r in saved["reports"]] == [
        "Market analyst",
        "Bull case",
        "Research manager",
        "Trader",
        "Neutral risk view",
    ]
    assert saved["llm_calls"] == 5
    again = manager.submit("NVDA", "stock")
    assert again.cached and manager.view(again.id)["status"] == "done"
    assert manager.history()[0]["ticker"] == "NVDA"


@pytest.mark.unit
def test_failed_run_reports_the_error_and_the_worker_keeps_going(tmp_path):
    flaky = {"fail": True}

    def factory(asset, cbs):
        graph = FakeGraph(cbs, fail=flaky["fail"])
        flaky["fail"] = False
        return graph

    m = JobManager(tmp_path, factory, lambda s, q: "")
    bad = _wait(m, m.submit("NVDA", "stock").id)
    assert bad["status"] == "error" and "quota" in bad["error"]
    good = _wait(m, m.submit("AAPL", "stock").id)
    assert good["status"] == "done"


@pytest.mark.unit
def test_questions_are_answered_from_the_saved_run_only(manager):
    view = _wait(manager, manager.submit("NVDA", "stock").id)
    answer = manager.ask("NVDA", view["date"], "Why overweight?")
    assert answer == "Answer to: Why overweight?"
    system, question = manager.asked[-1]
    assert "Go long" in system and "never tell the reader to buy or sell" in system
    with pytest.raises(LookupError):
        manager.ask("MSFT", view["date"], "anything")


@pytest.mark.unit
def test_track_record_reads_the_research_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "name": "tradingagents_free_model",
                "verdict": "fail",
                "why": "27 of 57 calls right (47%)",
                "date": "2026-10-12",
            }
        )
        + "\n"
        + json.dumps({"name": "x", "verdict": "pass"})
        + "\n"
    )
    t = track_record(ledger)
    assert t["agent_test"]["summary"] == "27 of 57 calls right (47%)"
    assert (t["agent_test"]["right"], t["agent_test"]["calls"], t["agent_test"]["hit_rate"]) == (
        27,
        57,
        47,
    )
    assert t["strategies_tested"] == 2 and t["strategies_passed"] == 1


# ------------------------------------------------------------------ HTTP


@pytest.fixture
def server(manager):
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, "s3cret"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(url, body=None, code="s3cret"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if code:
        req.add_header("X-Passcode", code)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.mark.unit
def test_api_flow_with_passcode(server, manager):
    status, _, body = _call(server + "/api/health", code=None)
    assert status == 200 and json.loads(body)["passcode_required"] is True
    assert _call(server + "/api/analyses", code=None)[0] == 401
    assert _call(server + "/api/analyses", code="wrong")[0] == 401

    status, _, body = _call(
        server + "/api/analyze", {"query": "https://finance.yahoo.com/quote/NVDA/"}
    )
    assert status == 202
    job = json.loads(body)
    assert job["ticker"] == "NVDA"
    done = _wait(manager, job["id"])

    status, _, body = _call(server + f"/api/jobs/{job['id']}")
    assert status == 200 and json.loads(body)["status"] == "done"
    status, _, body = _call(server + f"/api/analyses/NVDA/{done['date']}")
    assert status == 200 and json.loads(body)["rating"] == "Overweight"
    status, _, body = _call(
        server + "/api/ask", {"ticker": "NVDA", "date": done["date"], "question": "Risks?"}
    )
    assert status == 200 and json.loads(body)["answer"] == "Answer to: Risks?"

    assert _call(server + "/api/analyze", {"query": "drop table"})[0] == 400
    assert (
        _call(server + "/api/ask", {"ticker": "NVDA", "date": done["date"], "question": " "})[0]
        == 400
    )
    assert _call(server + "/api/jobs/000000000000")[0] == 404


@pytest.mark.unit
def test_static_page_is_served_with_a_strict_policy_and_no_traversal(server):
    status, headers, body = _call(server + "/", code=None)
    assert status == 200 and b"TradingAgents" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert _call(server + "/static/app.js", code=None)[0] == 200
    assert _call(server + "/static/../server.py", code=None)[0] == 404
    assert _call(server + "/static/%2e%2e/server.py", code=None)[0] == 404


@pytest.mark.unit
def test_bad_bodies_are_rejected(server):
    req = urllib.request.Request(server + "/api/analyze", data=b"not json", method="POST")
    req.add_header("X-Passcode", "s3cret")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
