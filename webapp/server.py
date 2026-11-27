"""Phone-friendly web app for TradingAgents: a JSON API plus one static page.

Standard library only (``http.server``), so it runs anywhere the package does:

    WEBAPP_PASSCODE=choose-one python -m webapp --host 0.0.0.0 --port 8000

Set ``WEBAPP_PASSCODE`` whenever the server is reachable from the internet: every
analysis spends the model provider's quota or credits.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from webapp.jobs import JobManager
from webapp.tickers import parse_ticker

STATIC = Path(__file__).with_name("static")
LEDGER = Path(__file__).resolve().parents[1] / "scripts" / "research" / "lab" / "ledger.jsonl"
MAX_BODY = 16 * 1024


def track_record(ledger_path: Path = LEDGER) -> dict:
    """What testing says about how far to trust a rating, from the research ledger."""
    records = []
    if ledger_path.exists():
        records = [json.loads(x) for x in ledger_path.read_text().splitlines() if x.strip()]
    latest = {r["name"]: r for r in records}
    agent = latest.get("tradingagents_free_model")
    agent_test = None
    if agent:
        m = re.search(r"(\d+) of (\d+)\D*?\((\d+)%\)", agent.get("why", ""))
        agent_test = {
            "summary": agent["why"],
            "date": agent["date"],
            "right": int(m.group(1)) if m else None,
            "calls": int(m.group(2)) if m else None,
            "hit_rate": int(m.group(3)) if m else None,
        }
    return {
        "agent_test": agent_test,
        "strategies_tested": len(latest),
        "strategies_passed": sum(1 for r in latest.values() if r.get("verdict") == "pass"),
    }


def default_manager() -> JobManager:
    """Real TradingAgents wiring, configured by the usual TRADINGAGENTS_* variables."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.llm_clients.factory import create_llm_client

    config = dict(DEFAULT_CONFIG)

    def graph_factory(asset_type: str, callbacks: list):
        analysts = ["market", "social", "news"] + (
            [] if asset_type == "crypto" else ["fundamentals"]
        )
        return TradingAgentsGraph(analysts, config=config, callbacks=callbacks)

    llm = None

    def ask_model(system: str, question: str) -> str:
        nonlocal llm
        if llm is None:
            llm = create_llm_client(
                provider=config["llm_provider"],
                model=config["quick_think_llm"],
                base_url=config.get("backend_url"),
            ).get_llm()
        content = llm.invoke([("system", system), ("human", question)]).content
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return str(content).strip()

    data_dir = os.environ.get("WEBAPP_DATA_DIR") or str(Path(config["results_dir"]).parent / "web")
    label = f"{config['llm_provider']} · {config['deep_think_llm']}"
    return JobManager(data_dir, graph_factory, ask_model, model_label=label)


def make_handler(manager: JobManager, passcode: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingAgentsWeb/1.0"

        def log_message(self, fmt, *args):  # keep request lines, drop query strings
            print(f"{self.address_string()} {fmt % args}".split("?")[0])

        # -- helpers ------------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _authorized(self) -> bool:
            if not passcode:
                return True
            given = self.headers.get("X-Passcode", "")
            return hmac.compare_digest(given.encode(), passcode.encode())

        def _body(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large.")
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._error(HTTPStatus.BAD_REQUEST, "Request body must be JSON.")
                return None
            if not isinstance(data, dict):
                self._error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
                return None
            return data

        # -- routes ---------------------------------------------------------------

        def do_GET(self):  # noqa: N802 (http.server naming)
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/") :])
            if path == "/api/health":
                return self._json(200, {"ok": True, "passcode_required": bool(passcode)})
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "Wrong or missing passcode.")
            if path == "/api/track-record":
                return self._json(200, track_record())
            if path == "/api/analyses":
                return self._json(200, {"analyses": manager.history()})
            m = re.fullmatch(r"/api/analyses/([^/]+)/(\d{4}-\d{2}-\d{2})", path)
            if m:
                analysis = manager.load(m.group(1), m.group(2))
                if not analysis:
                    return self._error(HTTPStatus.NOT_FOUND, "No saved analysis for that day.")
                return self._json(200, analysis)
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})", path)
            if m:
                view = manager.view(m.group(1))
                if not view:
                    return self._error(
                        HTTPStatus.NOT_FOUND, "Unknown job (the server may have restarted)."
                    )
                return self._json(200, view)
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if not self._authorized():
                return self._error(HTTPStatus.UNAUTHORIZED, "Wrong or missing passcode.")
            data = self._body()
            if data is None:
                return None
            if path == "/api/analyze":
                try:
                    ticker, asset = parse_ticker(str(data.get("query", "")))
                except ValueError as exc:
                    return self._error(HTTPStatus.BAD_REQUEST, str(exc))
                job = manager.submit(ticker, asset, refresh=bool(data.get("refresh")))
                return self._json(HTTPStatus.ACCEPTED, manager.view(job.id))
            if path == "/api/ask":
                ticker, day = str(data.get("ticker", "")), str(data.get("date", ""))
                question = str(data.get("question", "")).strip()
                if not question:
                    return self._error(HTTPStatus.BAD_REQUEST, "Type a question first.")
                try:
                    answer = manager.ask(ticker, day, question)
                except LookupError as exc:
                    return self._error(HTTPStatus.NOT_FOUND, str(exc))
                except Exception as exc:  # model/provider failures go back to the page
                    return self._error(
                        HTTPStatus.BAD_GATEWAY, f"The model didn't answer: {exc}"[:300]
                    )
                return self._json(200, {"answer": answer})
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")

        def _static(self, name: str) -> None:
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "Not found.")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

    return Handler


def serve(host: str, port: int, manager: JobManager | None = None) -> ThreadingHTTPServer:
    passcode = os.environ.get("WEBAPP_PASSCODE") or None
    if not passcode and host not in ("127.0.0.1", "localhost"):
        print("warning: WEBAPP_PASSCODE is not set; anyone who finds this address can run analyses")
    server = ThreadingHTTPServer((host, port), make_handler(manager or default_manager(), passcode))
    return server
