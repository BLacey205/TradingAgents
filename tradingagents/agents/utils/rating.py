"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

The Portfolio Manager also states a 0-100 confidence, read back by
``parse_confidence`` for backtest calibration.

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re

# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Two-pass strategy:
    1. Look for an explicit "Rating: X" label (tolerant of markdown bold).
    2. Fall back to the first 5-tier rating word found anywhere in the text.

    Returns a Title-cased rating string, or ``default`` if no rating word appears.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    return default


# Matches "Confidence: 72%" / "**Confidence**: 72" / "confidence - 0.72" on one line.
_CONFIDENCE_RE = re.compile(
    r"confidence\W*?[:\-][\s*]*(\d{1,3}(?:\.\d+)?)\s*(%)?", re.IGNORECASE,
)


def parse_confidence(text: str) -> int | None:
    """Extract the Portfolio Manager's stated confidence as an int percentage.

    Reads the first labelled ``Confidence: N`` line; fractions such as ``0.72``
    without a percent sign read as 72. Returns ``None`` when no confidence is
    stated or the number is outside 0-100, so older decisions and free-text
    fallbacks simply carry no confidence rather than a guessed one.
    """
    for line in (text or "").splitlines():
        m = _CONFIDENCE_RE.search(line)
        if not m:
            continue
        number = float(m.group(1))
        if not m.group(2) and "." in m.group(1) and number < 1:
            number *= 100
        if 0 <= number <= 100:
            return int(round(number))
        return None
    return None
