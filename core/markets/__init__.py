"""Pacote de Mercados Globais e Mesa XTB (catálogo, feed Yahoo Finance e simulador XTB)."""

from core.markets.catalog import (
    MARKET_INSTRUMENTS,
    NON_CRYPTO_CATEGORIES,
    is_forex_symbol,
    list_instruments,
)
from core.markets.xtb_desk import DEFAULT_XTB_CAPITAL_USD, XTBDeskStore
from core.markets.yahoo_feed import (
    FOREX_CANDLES_CACHE,
    FOREX_QUOTE_CACHE,
    WATCHLIST_CACHE,
    _fetch_yahoo_chart,
    _http_get_json,
    clear_forex_caches,
    fetch_any_quote,
    fetch_forex_candles,
    fetch_forex_history,
    fetch_forex_quote,
    fetch_watchlist,
)

__all__ = [
    "MARKET_INSTRUMENTS",
    "NON_CRYPTO_CATEGORIES",
    "DEFAULT_XTB_CAPITAL_USD",
    "FOREX_QUOTE_CACHE",
    "FOREX_CANDLES_CACHE",
    "WATCHLIST_CACHE",
    "is_forex_symbol",
    "list_instruments",
    "clear_forex_caches",
    "fetch_forex_quote",
    "fetch_forex_candles",
    "fetch_forex_history",
    "fetch_any_quote",
    "fetch_watchlist",
    "XTBDeskStore",
    "_http_get_json",
    "_fetch_yahoo_chart",
]
