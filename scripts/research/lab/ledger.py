"""The lab's memory: every strategy ever tested, winners and failures alike.

One JSON object per line in ``ledger.jsonl``. Records are only ever appended; a failed
idea is never deleted, so the next idea can be checked against it.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

LEDGER = Path(__file__).with_name("ledger.jsonl")
_WORD = re.compile(r"[a-z0-9]+")
_GENERIC_TAGS = {"daily", "intraday", "etf", "futures", "weekly"}
_STOP = {"the", "a", "an", "and", "or", "of", "to", "on", "in", "when", "is", "it", "for", "with"}


def load(path: Path = LEDGER) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append(record: dict, path: Path = LEDGER) -> None:
    record = {"date": date.today().isoformat(), **record}
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=False) + "\n")


def strategies_tested(records: list[dict]) -> int:
    """Distinct strategies, the count the significance bar is split across."""
    return len({r["name"] for r in records})


def holdout_used(name: str, records: list[dict]) -> bool:
    return any(r["name"] == name and r.get("final") for r in records)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def similar(
    tags: tuple[str, ...], hypothesis: str, records: list[dict], name: str = ""
) -> list[dict]:
    """Earlier records that look like the same idea: shared tags or heavy word overlap."""
    mine = _words(hypothesis)
    hits = []
    for r in records:
        if r["name"] == name:
            continue
        shared_tags = (set(tags) & set(r.get("tags", []))) - _GENERIC_TAGS
        theirs = _words(r.get("hypothesis", ""))
        overlap = len(mine & theirs) / max(1, min(len(mine), len(theirs)))
        if len(shared_tags) >= 2 or overlap >= 0.6:
            hits.append(r)
    return hits


def brief(records: list[dict]) -> str:
    """What the idea agent must read before proposing anything new."""
    latest = {}
    for r in records:
        latest[r["name"]] = r
    lines = [f"Strategies tested so far: {len(latest)}", ""]
    for r in latest.values():
        lines.append(f"- {r['name']} [{r['verdict'].upper()}] {r['hypothesis']}")
        if r.get("why"):
            lines.append(f"    why: {r['why']}")
        if r.get("do_not_repeat"):
            lines.append(f"    do not repeat: {r['do_not_repeat']}")
    return "\n".join(lines)
