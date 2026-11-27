"""Research lab command line.

    python scripts/research/lab/run.py list                   # the ledger, newest last
    python scripts/research/lab/run.py brief                  # what the idea agent reads first
    python scripts/research/lab/run.py test <strategy>        # train + test verdict, logged
    python scripts/research/lab/run.py test <strategy> --final  # also open the holdout (once)

A strategy is a file in ``strategies/`` that defines ``STRATEGY``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
import ledger  # noqa: E402

RESULTS = HERE.parents[2] / "results" / "lab"


def load_strategy(name: str) -> harness.Strategy:
    path = HERE / "strategies" / f"{name}.py"
    if not path.exists():
        raise SystemExit(f"No strategy file {path}")
    spec = importlib.util.spec_from_file_location(f"lab_strategy_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.STRATEGY


def fetch(symbol: str, interval: str, start: str):
    import yfinance as yf

    bars = yf.Ticker(symbol).history(start=start, interval=interval, auto_adjust=True)
    bars = bars[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if bars.empty:
        raise SystemExit(f"No data for {symbol}")
    bars.index = bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
    return bars


def pct(v):
    return "n/a" if v is None else f"{v:+.1%}"


def report(s: harness.Strategy, result: dict) -> str:
    out = [
        f"{s.name}: {result['verdict'].upper()}  (t-stat needed {result['t_required']}, "
        f"{result['strategies_counted']} strategies counted)"
    ]
    for seg in result["segments"]:
        m, b = seg["strategy"], seg["buy_and_hold"]
        out.append(
            f"  {seg['segment']:<8}{seg['start']} -> {seg['end']}  trades {m['trades']:<4} "
            f"win {'n/a' if m['win_rate'] is None else format(m['win_rate'], '.0%'):>4}  "
            f"return {pct(m['total_return'])} vs hold {pct(b['total_return'])}  "
            f"Sharpe {m['sharpe']:.2f} vs {b['sharpe']:.2f}  maxDD {m['max_drawdown']:.0%}"
        )
        for reason in seg["reasons"]:
            out.append(f"            fail: {reason}")
    return "\n".join(out)


def cmd_test(args) -> None:
    s = load_strategy(args.strategy)
    records = ledger.load()
    if args.final and ledger.holdout_used(s.name, records):
        raise SystemExit(f"The holdout for {s.name} was already opened; it can be scored once.")
    for r in ledger.similar(s.tags, s.hypothesis, records, name=s.name):
        print(f"warning: looks like {r['name']} ({r['verdict']}): {r.get('why', '')}")
    names = {r["name"] for r in records} | {s.name}
    result = harness.evaluate(
        s, fetch(s.symbol, s.interval, s.start), n_tested=len(names), final=args.final
    )
    print(report(s, result))
    out_dir = RESULTS / s.name
    out_dir.mkdir(parents=True, exist_ok=True)
    result.pop("frame").to_csv(out_dir / "bars.csv")
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    if not args.no_log:
        failed = [f"{seg['segment']}: {r}" for seg in result["segments"] for r in seg["reasons"]]
        ledger.append(
            {
                "name": s.name,
                "source": "lab",
                "verdict": result["verdict"],
                "final": args.final,
                "symbol": s.symbol,
                "interval": s.interval,
                "tags": list(s.tags),
                "hypothesis": s.hypothesis,
                "disproof": s.disproof,
                "cost_bps": s.cost_bps,
                "why": "; ".join(failed) if failed else "passed every rule",
                "segments": result["segments"],
                "t_required": result["t_required"],
            }
        )


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("brief")
    t = sub.add_parser("test")
    t.add_argument("strategy")
    t.add_argument("--final", action="store_true", help="also score the sealed holdout (once)")
    t.add_argument("--no-log", action="store_true", help="do not record this run in the ledger")
    args = p.parse_args(argv)
    if args.cmd == "list":
        for r in ledger.load():
            print(f"{r['date']}  {r['verdict'].upper():<5} {r['name']:<28} {r.get('why', '')}")
    elif args.cmd == "brief":
        print(ledger.brief(ledger.load()))
    else:
        cmd_test(args)


if __name__ == "__main__":
    main()
