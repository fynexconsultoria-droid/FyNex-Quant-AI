"""
Dados de Mercado ao Vivo e Consulta de Carteira Spot Binance (FyNex Quant AI)
=============================================================================
Fornece ticker ao vivo, order book resumido, candles OHLCV em formato TradingView,
consulta autenticada de saldos com cache curto e validação de conexão/permissões.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

import ccxt

from core.exchange.config import load_binance_config
from core.exchange.connection import (
    BALANCE_CACHE,
    CANDLES_CACHE,
    ORDERBOOK_CACHE,
    TICKER_CACHE,
    _get_active_mirror,
    call_with_retry,
    get_binance_exchange,
)
from core.infra.env_store import scrub_secrets


def _bc_fn(name: str, default):
    bc = sys.modules.get("binance_client")
    if bc is not None and hasattr(bc, name):
        return getattr(bc, name)
    return default


def fetch_live_ticker(
    symbol: str = "BTC/USDT",
    testnet: bool = False,
    ttl_seconds: float = 2.0,
) -> Dict[str, Any]:
    """Retorna cotação ao vivo, spread e estatísticas 24h com cache TTL curto."""
    cache_key = (symbol, bool(testnet))
    hit, cached = TICKER_CACHE.get(cache_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached

    t0 = time.perf_counter()
    exchange = _bc_fn("get_binance_exchange", get_binance_exchange)(testnet=testnet, timeout=8000)
    ticker = call_with_retry(
        lambda: exchange.fetch_ticker(symbol),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    price = float(ticker.get("last") or ticker.get("close") or 0.0)
    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    spread = max(0.0, ask - bid) if (bid > 0 and ask > 0) else 0.0
    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else price
    spread_pct = (spread / mid * 100.0) if mid > 0 else 0.0

    result = {
        "symbol": symbol,
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread": round(spread, 6),
        "spread_pct": round(spread_pct, 5),
        "open_24h": float(ticker.get("open") or 0.0),
        "change_24h_pct": float(ticker.get("percentage") or 0.0),
        "high_24h": float(ticker.get("high") or 0.0),
        "low_24h": float(ticker.get("low") or 0.0),
        "volume_24h": float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0.0),
        "base_volume_24h": float(ticker.get("baseVolume") or 0.0),
        "vwap": float(ticker.get("vwap") or 0.0),
        "latency_ms": latency_ms,
        "source_mirror": "testnet.binance.vision" if testnet else _get_active_mirror(),
        "timestamp": int(ticker.get("timestamp") or int(time.time() * 1000)),
    }
    return TICKER_CACHE.set(cache_key, result)


def fetch_order_book_summary(
    symbol: str = "BTC/USDT",
    limit: int = 15,
    testnet: bool = False,
    ttl_seconds: float = 2.0,
) -> Dict[str, Any]:
    """Retorna resumo do livro de ofertas (top bids/asks, spread e pressão compradora vs vendedora)."""
    safe_limit = max(5, min(int(limit), 50))
    cache_key = (symbol, safe_limit, bool(testnet))
    hit, cached = ORDERBOOK_CACHE.get(cache_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached

    t0 = time.perf_counter()
    exchange = _bc_fn("get_binance_exchange", get_binance_exchange)(testnet=testnet, timeout=8000)
    ob = call_with_retry(
        lambda: exchange.fetch_order_book(symbol, limit=safe_limit),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    spread = max(0.0, best_ask - best_bid) if (best_bid > 0 and best_ask > 0) else 0.0
    mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
    spread_pct = (spread / mid * 100.0) if mid > 0 else 0.0

    bid_vol = sum(float(level[1]) for level in bids)
    ask_vol = sum(float(level[1]) for level in asks)
    total_vol = bid_vol + ask_vol
    buy_pressure_pct = round((bid_vol / total_vol * 100.0), 1) if total_vol > 0 else 50.0

    result = {
        "symbol": symbol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 6),
        "spread_pct": round(spread_pct, 5),
        "bid_volume": round(bid_vol, 6),
        "ask_volume": round(ask_vol, 6),
        "buy_pressure_pct": buy_pressure_pct,
        "sell_pressure_pct": round(100.0 - buy_pressure_pct, 1),
        "top_bids": [[float(p), float(q)] for p, q in bids[:5]],
        "top_asks": [[float(p), float(q)] for p, q in asks[:5]],
        "latency_ms": latency_ms,
        "timestamp": int(ob.get("timestamp") or int(time.time() * 1000)),
    }
    return ORDERBOOK_CACHE.set(cache_key, result)


def fetch_live_candles(
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    limit: int = 150,
    testnet: bool = False,
    ttl_seconds: float = 5.0,
) -> List[Dict[str, Any]]:
    """Busca candles OHLCV diretamente da Binance no formato TradingView (time em segundos)."""
    safe_limit = max(1, min(int(limit), 1000))
    cache_key = (symbol, timeframe, safe_limit, bool(testnet))
    hit, cached = CANDLES_CACHE.get(cache_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached

    exchange = _bc_fn("get_binance_exchange", get_binance_exchange)(testnet=testnet, timeout=8000)
    raw = call_with_retry(
        lambda: exchange.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=safe_limit),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    candles = [
        {
            "time": int(row[0] // 1000),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in (raw or [])
    ]
    return CANDLES_CACHE.set(cache_key, candles)


def test_binance_connection(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    testnet: Optional[bool] = None,
) -> Dict[str, Any]:
    """Testa conexão autenticada com a Binance, recupera saldos Spot e valida permissões."""
    cfg = _bc_fn("load_binance_config", load_binance_config)()
    api_key = api_key or cfg["api_key"]
    api_secret = api_secret or cfg["api_secret"]
    if testnet is None:
        testnet = cfg["testnet"]

    if not api_key or not api_secret:
        return {
            "success": False,
            "error_code": "NO_KEYS",
            "message": "Chave de API ou Chave Secreta não configuradas no sistema.",
            "instructions": "Preencha a API Key e API Secret geradas no painel da Binance.",
        }

    try:
        t0 = time.perf_counter()
        is_adhoc = bool((api_key and api_key != cfg["api_key"]) or (api_secret and api_secret != cfg["api_secret"]))
        exchange = _bc_fn("get_binance_exchange", get_binance_exchange)(
            api_key=api_key, api_secret=api_secret, testnet=testnet, use_cache=not is_adhoc
        )
        raw_balance = call_with_retry(
            lambda: exchange.fetch_balance(),
            exchange=exchange,
            testnet=bool(testnet),
            max_attempts=2,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        clock_drift_ms: Optional[int] = None
        try:
            srv_ms = int(exchange.fetch_time())
            clock_drift_ms = srv_ms - int(time.time() * 1000)
        except Exception:
            pass

        assets: List[Dict[str, Any]] = []
        for currency, data in raw_balance.get("total", {}).items():
            if data and float(data) > 0.000001:
                assets.append({
                    "asset": currency,
                    "free": float(raw_balance.get("free", {}).get(currency, 0.0) or 0.0),
                    "locked": float(raw_balance.get("used", {}).get(currency, 0.0) or 0.0),
                    "total": float(data),
                })

        priority = {"USDT": 0, "BTC": 1, "BNB": 2}
        assets.sort(key=lambda item: (priority.get(item["asset"], 3), -item["total"]))

        usdt_free = float(raw_balance.get("free", {}).get("USDT", 0.0) or 0.0)
        usdt_total = float(raw_balance.get("total", {}).get("USDT", 0.0) or 0.0)
        btc_total = float(raw_balance.get("total", {}).get("BTC", 0.0) or 0.0)

        account_info = raw_balance.get("info", {})
        can_trade = bool(account_info.get("canTrade", False)) if account_info else True
        can_withdraw = bool(account_info.get("canWithdraw", False))

        env_name = "Binance Spot Testnet (Sandbox)" if testnet else "Binance Spot Oficial (Produção Real)"

        return {
            "success": True,
            "environment": env_name,
            "testnet": testnet,
            "can_trade": can_trade,
            "can_withdraw": can_withdraw,
            "usdt_free": usdt_free,
            "usdt_total": usdt_total,
            "btc_total": btc_total,
            "assets": assets[:10],
            "latency_ms": latency_ms,
            "clock_drift_ms": clock_drift_ms,
            "active_mirror": "testnet.binance.vision" if testnet else _get_active_mirror(authenticated=True),
            "message": f"Conexão com a {env_name} estabelecida com sucesso ({latency_ms:.0f} ms)! Carteira sincronizada.",
        }

    except ccxt.AuthenticationError as e:
        return {
            "success": False,
            "error_code": "AUTH_ERROR",
            "message": "Erro de Autenticação Binance: API Key ou Secret inválida ou expirada.",
            "detail": scrub_secrets(e, (api_key, api_secret)),
        }
    except ccxt.ExchangeNotAvailable as e:
        return {
            "success": False,
            "error_code": "EXCHANGE_UNAVAILABLE",
            "message": "Servidores da Binance temporariamente inacessíveis ou sob manutenção.",
            "detail": scrub_secrets(e, (api_key, api_secret)),
        }
    except ccxt.NetworkError as e:
        return {
            "success": False,
            "error_code": "NETWORK_ERROR",
            "message": "Falha de rede ao conectar com a Binance. Verifique sua conexão com a internet.",
            "detail": scrub_secrets(e, (api_key, api_secret)),
        }
    except Exception as e:
        error_msg = scrub_secrets(e, (api_key, api_secret))
        if "-2015" in error_msg:
            msg = "Chave de API inválida, IP não autorizado ou permissões insuficientes na Binance."
        elif "-1021" in error_msg:
            msg = "Dessincronização de horário com o servidor da Binance. Ajuste a hora do seu Windows."
        else:
            msg = f"Falha ao validar credenciais da Binance: {error_msg}"
        return {"success": False, "error_code": "GENERAL_ERROR", "message": msg, "detail": error_msg}


def fetch_cached_balance(
    testnet: Optional[bool] = None,
    ttl_seconds: float = 20.0,
) -> Dict[str, Any]:
    """Retorna os saldos da carteira Spot em cache curto (20s)."""
    cfg = _bc_fn("load_binance_config", load_binance_config)()
    if not cfg["has_keys"]:
        return {
            "connected": False,
            "success": False,
            "has_keys": False,
            "message": "Nenhuma chave Binance configurada.",
        }

    use_testnet = cfg["testnet"] if testnet is None else bool(testnet)
    cache_key = (cfg["api_key_masked"], use_testnet)
    hit, cached = BALANCE_CACHE.get(cache_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return dict(cached)

    res = _bc_fn("test_binance_connection", test_binance_connection)(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=use_testnet,
    )
    res["connected"] = bool(res.get("success"))
    res["has_keys"] = True
    res["trading_mode"] = cfg["trading_mode"]
    return dict(BALANCE_CACHE.set(cache_key, res))
