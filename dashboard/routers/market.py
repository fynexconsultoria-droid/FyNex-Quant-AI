"""Rotas de Mercado ao Vivo, Watchlist, Order Book, Notícias e Macro."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import binance_client
from core import forex_ai_trader, forex_market, macro_intelligence, news_feed
from dashboard.bot_controller import allow_forced_refresh
from dashboard.schemas import SYMBOL_PATTERN

router = APIRouter(tags=["market"])


@router.get("/api/market/live")
def get_live_market_ticker(symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN)):
    try:
        if forex_market.is_forex_symbol(symbol):
            return forex_market.fetch_forex_quote(symbol=symbol)
        return binance_client.fetch_live_ticker(symbol=symbol, testnet=False)
    except Exception:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar cotação ao vivo para {symbol}.")


@router.get("/api/market/instruments")
def get_market_instruments(
    category: Optional[str] = Query(default=None, pattern="^(crypto|forex|commodities|indices)$"),
):
    return {"instruments": forex_market.list_instruments(category=category)}


@router.get("/api/market/watchlist")
def get_market_watchlist(
    category: str = Query(default="all", pattern="^(all|crypto|forex|commodities|indices)$"),
):
    return {"items": forex_market.fetch_watchlist(category=category)}


@router.get("/api/market/depth")
def get_live_market_depth(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    limit: int = Query(default=15, ge=5, le=50),
):
    if forex_market.is_forex_symbol(symbol):
        try:
            q = forex_market.fetch_forex_quote(symbol=symbol)
            return {
                "symbol": q["symbol"],
                "best_bid": q["bid"],
                "best_ask": q["ask"],
                "spread": q["spread"],
                "spread_pct": q["spread_pct"],
                "buy_pressure_pct": 50.0,
                "sell_pressure_pct": 50.0,
                "latency_ms": q["latency_ms"],
                "timestamp": q["timestamp"],
            }
        except Exception:
            raise HTTPException(status_code=503, detail="Falha ao consultar book Forex.")
    try:
        return binance_client.fetch_order_book_summary(symbol=symbol, limit=limit, testnet=False)
    except Exception:
        raise HTTPException(status_code=503, detail="Falha ao consultar livro de ofertas na Binance.")


@router.get("/api/news")
def get_live_news(
    category: str = Query(default="all", pattern="^(all|cripto|forex|macro)$"),
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=30, ge=1, le=100),
    refresh: bool = Query(default=False),
):
    if refresh and allow_forced_refresh("news", cooldown_sec=15.0):
        news_feed.clear_news_cache()
    return news_feed.fetch_live_news(category=category, query=q, limit=limit)


@router.get("/api/macro/live")
def get_live_macro_intelligence(refresh: bool = Query(default=False)):
    if refresh and allow_forced_refresh("macro", cooldown_sec=15.0):
        macro_intelligence.clear_macro_cache()
    return macro_intelligence.fetch_macro_snapshot()


@router.get("/api/forex/ai-status")
def get_forex_ai_status():
    is_open, msg = forex_ai_trader.is_forex_market_open()
    return {
        "market_open": is_open,
        "status_message": msg,
        "monitored_pairs": list(forex_ai_trader.FOREX_AI_PAIRS),
    }
