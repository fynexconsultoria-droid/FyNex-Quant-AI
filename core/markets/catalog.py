"""
Catálogo Unificado de Instrumentos Financeiros (FyNex Quant AI)
===============================================================
Define metadados de Cripto Spot (Binance), Forex, Metais/Commodities e Índices
usados tanto pelo feed de cotações quanto pela mesa XTB e dashboard.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

NON_CRYPTO_CATEGORIES = ("forex", "commodities", "indices")

MARKET_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    # --- Cripto (Binance Spot) ---
    "BTC/USDT": {
        "symbol": "BTC/USDT", "name": "Bitcoin / Tether", "category": "crypto", "badge": "CRIPTO",
        "decimals": 2, "pip_size": 1.0, "contract_size": 1.0,
        "default_lot": 0.01, "min_lot": 0.001, "max_leverage": 10, "typical_spread_pips": 1.0,
    },
    "ETH/USDT": {
        "symbol": "ETH/USDT", "name": "Ethereum / Tether", "category": "crypto", "badge": "CRIPTO",
        "decimals": 2, "pip_size": 0.1, "contract_size": 1.0,
        "default_lot": 0.1, "min_lot": 0.01, "max_leverage": 10, "typical_spread_pips": 0.5,
    },
    "SOL/USDT": {
        "symbol": "SOL/USDT", "name": "Solana / Tether", "category": "crypto", "badge": "CRIPTO",
        "decimals": 2, "pip_size": 0.01, "contract_size": 1.0,
        "default_lot": 1.0, "min_lot": 0.1, "max_leverage": 10, "typical_spread_pips": 2.0,
    },
    "BNB/USDT": {
        "symbol": "BNB/USDT", "name": "BNB / Tether", "category": "crypto", "badge": "CRIPTO",
        "decimals": 2, "pip_size": 0.01, "contract_size": 1.0,
        "default_lot": 0.5, "min_lot": 0.05, "max_leverage": 10, "typical_spread_pips": 2.0,
    },
    "XRP/USDT": {
        "symbol": "XRP/USDT", "name": "Ripple / Tether", "category": "crypto", "badge": "CRIPTO",
        "decimals": 4, "pip_size": 0.0001, "contract_size": 1.0,
        "default_lot": 100.0, "min_lot": 10.0, "max_leverage": 10, "typical_spread_pips": 1.5,
    },
    # --- Forex ---
    "EUR/USD": {
        "symbol": "EUR/USD", "name": "Euro / Dólar EUA", "category": "forex", "badge": "FOREX",
        "yahoo": "EURUSD=X", "decimals": 5, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 100, "typical_spread_pips": 0.8,
    },
    "GBP/USD": {
        "symbol": "GBP/USD", "name": "Libra / Dólar EUA", "category": "forex", "badge": "FOREX",
        "yahoo": "GBPUSD=X", "decimals": 5, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 100, "typical_spread_pips": 1.1,
    },
    "USD/JPY": {
        "symbol": "USD/JPY", "name": "Dólar EUA / Iene", "category": "forex", "badge": "FOREX",
        "yahoo": "USDJPY=X", "decimals": 3, "pip_size": 0.01, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 100, "typical_spread_pips": 0.9,
    },
    "USD/BRL": {
        "symbol": "USD/BRL", "name": "Dólar EUA / Real BR", "category": "forex", "badge": "FOREX",
        "yahoo": "USDBRL=X", "decimals": 4, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 30, "typical_spread_pips": 12.0,
    },
    "EUR/BRL": {
        "symbol": "EUR/BRL", "name": "Euro / Real BR", "category": "forex", "badge": "FOREX",
        "yahoo": "EURBRL=X", "decimals": 4, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 30, "typical_spread_pips": 15.0,
    },
    "AUD/USD": {
        "symbol": "AUD/USD", "name": "Dólar Australiano / USD", "category": "forex", "badge": "FOREX",
        "yahoo": "AUDUSD=X", "decimals": 5, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 100, "typical_spread_pips": 1.0,
    },
    "USD/CHF": {
        "symbol": "USD/CHF", "name": "Dólar EUA / Franco Suíço", "category": "forex", "badge": "FOREX",
        "yahoo": "USDCHF=X", "decimals": 5, "pip_size": 0.0001, "contract_size": 100_000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 100, "typical_spread_pips": 1.1,
    },
    # --- Metais & Commodities ---
    "XAU/USD": {
        "symbol": "XAU/USD", "name": "Ouro Spot / Dólar EUA", "category": "commodities", "badge": "OURO",
        "yahoo": "GC=F", "decimals": 2, "pip_size": 0.1, "contract_size": 100.0,
        "default_lot": 0.05, "min_lot": 0.01, "max_leverage": 50, "typical_spread_pips": 2.5,
    },
    "XAG/USD": {
        "symbol": "XAG/USD", "name": "Prata Spot / Dólar EUA", "category": "commodities", "badge": "PRATA",
        "yahoo": "SI=F", "decimals": 3, "pip_size": 0.01, "contract_size": 1000.0,
        "default_lot": 0.05, "min_lot": 0.01, "max_leverage": 30, "typical_spread_pips": 2.0,
    },
    "XPT/USD": {
        "symbol": "XPT/USD", "name": "Platina / Dólar EUA", "category": "commodities", "badge": "METAL",
        "yahoo": "PL=F", "decimals": 2, "pip_size": 0.1, "contract_size": 50.0,
        "default_lot": 0.05, "min_lot": 0.01, "max_leverage": 20, "typical_spread_pips": 3.0,
    },
    "COPPER/USD": {
        "symbol": "COPPER/USD", "name": "Cobre Industrial LME / USD (ton)", "category": "commodities", "badge": "METAL",
        "yahoo": "HG=F", "price_scale": 2204.62262, "decimals": 2, "pip_size": 1.0, "contract_size": 1.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 20, "typical_spread_pips": 4.0,
    },
    "OIL/USD": {
        "symbol": "OIL/USD", "name": "Petróleo WTI / USD", "category": "commodities", "badge": "ENERGIA",
        "yahoo": "CL=F", "decimals": 2, "pip_size": 0.01, "contract_size": 100.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 20, "typical_spread_pips": 3.0,
    },
    "BRENT/USD": {
        "symbol": "BRENT/USD", "name": "Petróleo Brent / USD", "category": "commodities", "badge": "ENERGIA",
        "yahoo": "BZ=F", "decimals": 2, "pip_size": 0.01, "contract_size": 100.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 20, "typical_spread_pips": 3.0,
    },
    "NATGAS/USD": {
        "symbol": "NATGAS/USD", "name": "Gás Natural / USD", "category": "commodities", "badge": "ENERGIA",
        "yahoo": "NG=F", "decimals": 3, "pip_size": 0.001, "contract_size": 1000.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 10, "typical_spread_pips": 4.0,
    },
    "REMX/USD": {
        "symbol": "REMX/USD", "name": "Terras Raras (ETF REMX)", "category": "commodities", "badge": "R.EARTH",
        "yahoo": "REMX", "decimals": 2, "pip_size": 0.01, "contract_size": 100.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 10, "typical_spread_pips": 5.0,
    },
    # --- Índices ---
    "US500/USD": {
        "symbol": "US500/USD", "name": "Índice S&P 500 Futuros (EUA)", "category": "indices", "badge": "ÍNDICE",
        "yahoo": "ES=F", "decimals": 2, "pip_size": 0.1, "contract_size": 1.0,
        "default_lot": 0.50, "min_lot": 0.05, "max_leverage": 20, "typical_spread_pips": 4.0,
    },
    "US100/USD": {
        "symbol": "US100/USD", "name": "Índice Nasdaq 100 Futuros (EUA)", "category": "indices", "badge": "ÍNDICE",
        "yahoo": "NQ=F", "decimals": 2, "pip_size": 0.1, "contract_size": 1.0,
        "default_lot": 0.10, "min_lot": 0.01, "max_leverage": 20, "typical_spread_pips": 6.0,
    },
}


def is_forex_symbol(symbol: str) -> bool:
    """Retorna True para instrumentos não-cripto (Forex, Metais, Commodities e Índices)."""
    info = MARKET_INSTRUMENTS.get((symbol or "").upper())
    return bool(info and info.get("category") in NON_CRYPTO_CATEGORIES)


def list_instruments(category: Optional[str] = None) -> List[Dict[str, Any]]:
    items = list(MARKET_INSTRUMENTS.values())
    if category and category.lower() in ("crypto", "forex", "commodities", "indices"):
        items = [i for i in items if i["category"] == category.lower()]
    return items
