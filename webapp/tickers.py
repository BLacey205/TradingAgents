"""Turn what a phone user types or pastes (a ticker, ``$NVDA``, or a quote-page
link) into a canonical Yahoo symbol and its asset type."""

from __future__ import annotations

import re

from tradingagents.dataflows.symbol_utils import normalize_symbol

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")

# Coin names used in crypto price-page URLs.
_COIN_NAMES = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "cardano": "ADA",
    "dogecoin": "DOGE",
    "litecoin": "LTC",
    "bitcoin-cash": "BCH",
    "polkadot": "DOT",
    "avalanche": "AVAX",
    "chainlink": "LINK",
}

# (pattern, kind). The first capture group is the symbol, or a coin name for "coin".
_URL_PATTERNS = [
    (r"finance\.yahoo\.com/quote/([^/?#]+)", "symbol"),
    (r"robinhood\.com/(?:[a-z]{2}/[a-z]{2}/)?stocks/([A-Za-z0-9.\-]+)", "symbol"),
    (r"robinhood\.com/(?:[a-z]{2}/[a-z]{2}/)?crypto/([A-Za-z0-9]+)", "crypto"),
    (r"tradingview\.com/symbols/(?:[A-Za-z]+-)?([A-Za-z0-9.!]+)", "symbol"),
    (r"google\.com/finance/quote/([A-Za-z0-9.\-]+)", "symbol"),
    (r"cnbc\.com/quotes/([A-Za-z0-9.\-]+)", "symbol"),
    (r"marketwatch\.com/investing/(?:stock|fund|index)/([A-Za-z0-9.]+)", "symbol"),
    (r"stocktwits\.com/symbol/([A-Za-z0-9.\-]+)", "symbol"),
    (r"coinbase\.com/(?:[a-z]{2}/)?price/([a-z\-]+)", "coin"),
    (r"coingecko\.com/(?:[a-z]{2}/)?coins/([a-z\-]+)", "coin"),
]

_SAFE = re.compile(r"^[A-Za-z0-9._\-\^=]{1,32}$")


def _from_url(text: str) -> tuple[str, str] | None:
    for pattern, kind in _URL_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1), kind
    return None


def parse_ticker(text: str) -> tuple[str, str]:
    """Return ``(symbol, asset_type)``; raises ValueError with a readable message."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Type a ticker like NVDA, or paste a link to a stock or coin page.")
    looks_like_link = "/" in raw or raw.lower().startswith(("http", "www."))
    hit = _from_url(raw) if looks_like_link else None
    if hit:
        value, kind = hit
        if kind == "coin":
            base = _COIN_NAMES.get(value.lower())
            if not base:
                raise ValueError(f"Don't know the coin '{value}'. Type its symbol, like BTC-USD.")
            value = f"{base}-USD"
        elif kind == "crypto":
            value = f"{value.upper()}-USD"
        value = value.split(":")[0].rstrip("!")  # Google's NVDA:NASDAQ, TradingView's BTCUSD!
    elif looks_like_link:
        raise ValueError("Couldn't find a ticker in that link. Type the ticker instead.")
    elif len(raw.split()) > 1:
        raise ValueError("Type one ticker, like NVDA.")
    else:
        value = raw.lstrip("$")
    if not _SAFE.match(value):
        raise ValueError(f"'{value}' doesn't look like a ticker.")
    symbol = normalize_symbol(value)
    asset = "crypto" if symbol.upper().endswith(CRYPTO_SUFFIXES) else "stock"
    return symbol.upper(), asset
