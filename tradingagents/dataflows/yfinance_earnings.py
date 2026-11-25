"""yfinance earnings-calendar lookups.

Used by the options risk gate (no short options through earnings) and exposed
to agents as the ``get_next_earnings`` tool.

Look-ahead note: for a past ``curr_date`` the dates come from the *current*
yfinance calendar, so they are the dates earnings actually happened. Real
traders know the upcoming date weeks ahead, so this is a mild, standard
approximation, not a leak of price information.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

import yfinance as yf

from .stockstats_utils import yf_retry
from .symbol_utils import normalize_symbol


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def fetch_earnings_dates(symbol: str, limit: int = 40) -> list[date]:
    """Sorted, de-duplicated earnings dates (past and upcoming) for ``symbol``.

    Returns an empty list when yfinance has none (ETFs, crypto, or an outage).
    """
    ticker = yf.Ticker(normalize_symbol(symbol))
    try:
        frame = yf_retry(lambda: ticker.get_earnings_dates(limit=limit))
    except Exception:
        return []
    if frame is None or len(frame) == 0:
        return []
    return sorted({_to_date(ts) for ts in frame.index})


def next_earnings_date(dates: Iterable[date], as_of: date) -> date | None:
    """First earnings date on or after ``as_of`` (None if there isn't one)."""
    upcoming = [d for d in dates if d >= as_of]
    return min(upcoming) if upcoming else None


def get_next_earnings(ticker: str, curr_date: str) -> str:
    """Report the next earnings date relative to ``curr_date`` (yyyy-mm-dd)."""
    as_of = _to_date(curr_date)
    dates = fetch_earnings_dates(ticker)
    if not dates:
        return f"No earnings calendar available for {ticker.upper()}."
    nxt = next_earnings_date(dates, as_of)
    last = max((d for d in dates if d < as_of), default=None)
    lines = [f"# Earnings calendar for {ticker.upper()} (as of {as_of})"]
    if nxt:
        lines.append(f"Next earnings date: {nxt} ({(nxt - as_of).days} days away)")
    else:
        lines.append("Next earnings date: not yet announced")
    if last:
        lines.append(f"Most recent earnings date: {last}")
    lines.append(
        "Rule of thumb for short-option income trades: avoid holding short "
        "options through an earnings announcement."
    )
    return "\n".join(lines)
