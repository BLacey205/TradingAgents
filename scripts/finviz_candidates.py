"""Load a Finviz screener export and (optionally) run TradingAgents on it.

Finviz is used only as a candidate generator. Do not backtest on today's
screener results: a screen run now selects stocks with hindsight, so applying
it to past dates leaks look-ahead bias. For historical tests, rebuild the
universe point-in-time and pass explicit dates to ``tradingagents.backtest``.

Sources (pick one):
    --csv PATH          a CSV saved from the Finviz screener "Export" button
    --url URL           a Finviz Elite export URL; the token is read from the
                        FINVIZ_API_TOKEN env var and appended as ``auth=``,
                        so do not put it in the URL itself

Usage:
    python scripts/finviz_candidates.py --csv screen.csv --limit 10
    FINVIZ_API_TOKEN=... python scripts/finviz_candidates.py \
        --url "https://elite.finviz.com/export.ashx?v=111&f=sh_relvol_o2,ta_sma200_pa" \
        --run --date 2026-10-08
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd
import requests

from tradingagents.dataflows.utils import safe_ticker_component

ALLOWED_HOSTS = {"elite.finviz.com", "finviz.com"}


def with_token(url: str, token: str) -> str:
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing non-Finviz URL: {parts.hostname!r}")
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "auth"]
    query.append(("auth", token))
    return urlunparse(parts._replace(query=urlencode(query)))


def read_screen(csv: str | None, url: str | None) -> pd.DataFrame:
    if csv:
        return pd.read_csv(csv)
    token = os.environ.get("FINVIZ_API_TOKEN")
    if not token:
        raise SystemExit("set FINVIZ_API_TOKEN to use --url")
    resp = requests.get(with_token(url, token), timeout=30)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text))


def tickers_from(df: pd.DataFrame, limit: int | None) -> list[str]:
    if "Ticker" not in df.columns:
        raise SystemExit(f"no 'Ticker' column; got {list(df.columns)[:8]}")
    out: list[str] = []
    for raw in df["Ticker"].dropna().astype(str):
        try:
            out.append(safe_ticker_component(raw.strip().upper()))
        except ValueError:
            continue  # skip anything that is not a plain symbol
    out = list(dict.fromkeys(out))
    return out[:limit] if limit else out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--url")
    ap.add_argument("--limit", type=int, default=10, help="max tickers (default 10)")
    ap.add_argument("--run", action="store_true", help="run TradingAgents on each ticker")
    ap.add_argument("--date", help="trade date YYYY-MM-DD (required with --run)")
    args = ap.parse_args()

    tickers = tickers_from(read_screen(args.csv, args.url), args.limit)
    print("\n".join(tickers))
    if not args.run:
        return 0
    if not args.date:
        ap.error("--run requires --date")

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(config=DEFAULT_CONFIG.copy())
    for t in tickers:
        _, decision = graph.propagate(t, args.date)
        print(f"{t}: {decision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
