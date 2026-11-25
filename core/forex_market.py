"""
Facade de compatibilidade para Mercado Forex & Mesa XTB (FyNex Quant AI)
========================================================================
A implementação modularizada reside em `core.markets`:
- `core.markets.catalog`: catálogo de instrumentos (`MARKET_INSTRUMENTS`)
- `core.markets.yahoo_feed`: cotações, candles e watchlist com `TTLCache`
- `core.markets.xtb_desk`: simulador da mesa XTB (`XTBDeskStore`)
"""

from core.markets import (
    DEFAULT_XTB_CAPITAL_USD,
    MARKET_INSTRUMENTS,
    NON_CRYPTO_CATEGORIES as _NON_CRYPTO_CATEGORIES,
    XTBDeskStore,
    _fetch_yahoo_chart,
    _http_get_json,
    clear_forex_caches,
    fetch_any_quote,
    fetch_forex_candles,
    fetch_forex_history,
    fetch_forex_quote,
    fetch_watchlist,
    is_forex_symbol,
    list_instruments,
)

__all__ = [
    "MARKET_INSTRUMENTS",
    "DEFAULT_XTB_CAPITAL_USD",
    "_NON_CRYPTO_CATEGORIES",
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
