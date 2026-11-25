"""
Feed Público de Cotações, Candles e Watchlist Global (FyNex Quant AI)
=====================================================================
Consulta Yahoo Finance JSON para Forex, Metais, Commodities e Índices
com failover query1/query2 e cache TTL unificado (`core.infra.cache.TTLCache`).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import binance_client
from core.infra.cache import TTLCache
from core.markets.catalog import MARKET_INSTRUMENTS, NON_CRYPTO_CATEGORIES, is_forex_symbol, list_instruments

FOREX_QUOTE_CACHE: TTLCache[str, Dict[str, Any]] = TTLCache(max_size=64, default_ttl=15.0)
FOREX_CANDLES_CACHE: TTLCache[Tuple[str, str, str], List[Dict[str, Any]]] = TTLCache(max_size=64, default_ttl=10.0)
WATCHLIST_CACHE: TTLCache[str, List[Dict[str, Any]]] = TTLCache(max_size=16, default_ttl=15.0)

_YAHOO_TF_MAP: Dict[str, Tuple[str, str]] = {
    "1m": ("1m", "1d"),
    "3m": ("2m", "1d"),
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "5d"),
    "1h": ("60m", "1mo"),
}

_YAHOO_HISTORY_MAP: Dict[str, Tuple[str, str, int]] = {
    "1m": ("1m", "7d", 60),
    "3m": ("2m", "7d", 120),
    "5m": ("5m", "60d", 300),
    "15m": ("15m", "60d", 900),
    "30m": ("30m", "60d", 1800),
    "1h": ("60m", "2y", 3600),
    "1d": ("1d", "5y", 86400),
}


def _fm_fn(name: str, default):
    fm = sys.modules.get("core.forex_market")
    if fm is not None and hasattr(fm, name):
        return getattr(fm, name)
    return default


def clear_forex_caches() -> None:
    FOREX_QUOTE_CACHE.clear()
    FOREX_CANDLES_CACHE.clear()
    WATCHLIST_CACHE.clear()


def _http_get_json(url: str, timeout: float = 6.0, max_bytes: int = 2_097_152) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FyNexQuantAI/2.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes).decode("utf-8", errors="replace")
    return json.loads(raw)


def _fetch_yahoo_chart(yahoo_ticker: str, interval: str = "1m", range_str: str = "1d") -> Dict[str, Any]:
    encoded = urllib.parse.quote(yahoo_ticker, safe="")
    last_err: Optional[Exception] = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = (
            f"https://{host}/v8/finance/chart/{encoded}"
            f"?interval={interval}&range={range_str}&includePrePost=false"
        )
        try:
            data = _http_get_json(url, timeout=6.0)
            results = (data.get("chart") or {}).get("result") or []
            if results:
                return results[0]
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    raise ValueError(f"Sem dados de mercado retornados para {yahoo_ticker}")


def fetch_forex_quote(symbol: str, ttl_seconds: float = 15.0) -> Dict[str, Any]:
    """Retorna cotação ao vivo de um ativo Forex/Commodity/Índice com Bid/Ask e estatísticas 24h."""
    sym = symbol.upper()
    info = MARKET_INSTRUMENTS.get(sym)
    if not info or info.get("category") not in NON_CRYPTO_CATEGORIES:
        raise ValueError(f"Instrumento de mercado global não suportado: {symbol}")

    hit, cached = FOREX_QUOTE_CACHE.get(sym, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return dict(cached)

    t0 = time.perf_counter()
    chart = _fm_fn("_fetch_yahoo_chart", _fetch_yahoo_chart)(info["yahoo"], interval="5m", range_str="1d")
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    meta = chart.get("meta") or {}
    raw_quotes = (chart.get("indicators") or {}).get("quote") or []
    quote_block = raw_quotes[0] if (raw_quotes and isinstance(raw_quotes[0], dict)) else {}
    closes = [c for c in (quote_block.get("close") or []) if c is not None]
    highs = [h for h in (quote_block.get("high") or []) if h is not None]
    lows = [l for l in (quote_block.get("low") or []) if l is not None]
    vols = [v for v in (quote_block.get("volume") or []) if v is not None]

    scale = float(info.get("price_scale", 1.0) or 1.0)
    raw_price = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0.0))
    price = raw_price * scale
    if not (price > 0 and price == price and price < float("inf")):
        raise ValueError(f"Cotação inválida retornada para {sym}.")

    raw_prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or (closes[0] if closes else raw_price))
    prev_close = raw_prev * scale
    change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    high_24h = float(meta.get("regularMarketDayHigh") or (max(highs) if highs else raw_price)) * scale
    low_24h = float(meta.get("regularMarketDayLow") or (min(lows) if lows else raw_price)) * scale
    volume_24h = float(meta.get("regularMarketVolume") or sum(vols) or 0.0)

    pip_size = float(info["pip_size"])
    spread = float(info["typical_spread_pips"]) * pip_size
    half_spread = spread / 2.0
    bid = max(0.0, price - half_spread)
    ask = price + half_spread
    spread_pct = (spread / price * 100.0) if price > 0 else 0.0

    result = {
        "symbol": sym,
        "name": info["name"],
        "category": info["category"],
        "badge": info["badge"],
        "price": round(price, info["decimals"]),
        "bid": round(bid, info["decimals"]),
        "ask": round(ask, info["decimals"]),
        "spread": round(spread, info["decimals"] + 1),
        "spread_pips": float(info["typical_spread_pips"]),
        "spread_pct": round(spread_pct, 5),
        "open_24h": round(prev_close, info["decimals"]),
        "change_24h_pct": round(change_pct, 3),
        "high_24h": round(high_24h, info["decimals"]),
        "low_24h": round(low_24h, info["decimals"]),
        "volume_24h": volume_24h,
        "latency_ms": latency_ms,
        "source_mirror": "yahoo-fx-feed",
        "timestamp": int(time.time() * 1000),
    }

    FOREX_QUOTE_CACHE.set(sym, result)
    return dict(result)


def fetch_forex_candles(
    symbol: str,
    timeframe: str = "1m",
    limit: int = 150,
    ttl_seconds: float = 10.0,
) -> List[Dict[str, Any]]:
    """Retorna candles OHLCV de Forex/Commodities/Índices formatados para o TradingView."""
    sym = symbol.upper()
    info = MARKET_INSTRUMENTS.get(sym)
    if not info or info.get("category") not in NON_CRYPTO_CATEGORIES:
        raise ValueError(f"Instrumento de mercado global não suportado: {symbol}")

    safe_limit = max(1, min(int(limit), 1000))
    interval, range_str = _YAHOO_TF_MAP.get(timeframe, ("5m", "5d"))
    cache_key = (sym, interval, range_str)
    hit, cached = FOREX_CANDLES_CACHE.get(cache_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached[-safe_limit:]

    chart = _fm_fn("_fetch_yahoo_chart", _fetch_yahoo_chart)(info["yahoo"], interval=interval, range_str=range_str)
    timestamps = chart.get("timestamp") or []
    raw_quotes = (chart.get("indicators") or {}).get("quote") or []
    quote_block = raw_quotes[0] if (raw_quotes and isinstance(raw_quotes[0], dict)) else {}
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    scale = float(info.get("price_scale", 1.0) or 1.0)
    max_idx = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    candles: List[Dict[str, Any]] = []
    seen_times = set()
    for i in range(max_idx):
        ts = timestamps[i]
        if ts is None:
            continue
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        t_int = int(ts)
        if t_int in seen_times:
            continue
        seen_times.add(t_int)
        o_f, h_f, l_f, c_f = float(o) * scale, float(h) * scale, float(l) * scale, float(c) * scale
        if not (c_f > 0 and h_f >= l_f):
            continue
        v = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        candles.append({
            "time": t_int,
            "open": o_f,
            "high": h_f,
            "low": l_f,
            "close": c_f,
            "volume": max(0.0, v),
        })

    candles.sort(key=lambda x: x["time"])
    FOREX_CANDLES_CACHE.set(cache_key, candles)
    return candles[-safe_limit:]


def fetch_forex_history(
    symbol: str,
    timeframe: str = "1h",
    target_candles: int = 3000,
) -> "Any":
    """
    Baixa histórico longo de velas fechadas de Forex/Commodities/Índices via Yahoo Finance
    (até 2 anos em `1h`, 60 dias em `15m`/`30m`) e retorna DataFrame pronto para o SQLite e ML.
    """
    import pandas as pd

    sym = symbol.upper()
    info = MARKET_INSTRUMENTS.get(sym)
    if not info or info.get("category") not in NON_CRYPTO_CATEGORIES:
        raise ValueError(f"Instrumento de mercado global não suportado: {symbol}")

    interval, range_str, tf_sec = _YAHOO_HISTORY_MAP.get(timeframe, ("60m", "2y", 3600))
    chart = _fm_fn("_fetch_yahoo_chart", _fetch_yahoo_chart)(info["yahoo"], interval=interval, range_str=range_str)
    timestamps = chart.get("timestamp") or []
    raw_quotes = (chart.get("indicators") or {}).get("quote") or []
    quote_block = raw_quotes[0] if (raw_quotes and isinstance(raw_quotes[0], dict)) else {}
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    scale = float(info.get("price_scale", 1.0) or 1.0)
    now_sec = int(time.time())
    max_idx = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    rows: List[Dict[str, Any]] = []
    seen_times = set()
    for i in range(max_idx):
        ts = timestamps[i]
        if ts is None:
            continue
        t_int = int(ts)
        if t_int in seen_times or (t_int + tf_sec) > now_sec:
            continue
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        o_f, h_f, l_f, c_f = float(o) * scale, float(h) * scale, float(l) * scale, float(c) * scale
        if not (c_f > 0 and h_f >= l_f):
            continue
        seen_times.add(t_int)
        v_f = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        rows.append({
            "timestamp": t_int * 1000,
            "open": o_f,
            "high": h_f,
            "low": l_f,
            "close": c_f,
            "volume": max(0.0, v_f),
        })

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    if target_candles > 0 and len(df) > target_candles:
        df = df.iloc[-int(target_candles):].reset_index(drop=True)
    return df


def fetch_any_quote(symbol: str) -> Dict[str, Any]:
    """Busca cotação ao vivo tanto para Cripto (Binance) quanto para Forex (Yahoo feed)."""
    if is_forex_symbol(symbol):
        return _fm_fn("fetch_forex_quote", fetch_forex_quote)(symbol)
    ticker = dict(binance_client.fetch_live_ticker(symbol=symbol, testnet=False))
    info = MARKET_INSTRUMENTS.get(symbol.upper(), {})
    ticker["category"] = "crypto"
    ticker["badge"] = info.get("badge", "CRIPTO")
    ticker["name"] = info.get("name", symbol)
    return ticker


def fetch_watchlist(category: str = "all", ttl_seconds: float = 15.0) -> List[Dict[str, Any]]:
    """Retorna cotações resumidas para a Watchlist multi-ativos em paralelo com cache curto."""
    cat_key = category if category in ("crypto", "forex", "commodities", "indices") else "all"
    hit, cached = WATCHLIST_CACHE.get(cat_key, ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached

    instruments = list_instruments(cat_key if cat_key != "all" else None)
    results: Dict[str, Dict[str, Any]] = {}
    quote_fn = _fm_fn("fetch_any_quote", fetch_any_quote)

    def _load_one(inst: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        sym = inst["symbol"]
        try:
            q = quote_fn(sym)
            return sym, {
                "symbol": sym,
                "name": inst["name"],
                "category": inst["category"],
                "badge": inst["badge"],
                "decimals": inst["decimals"],
                "price": q.get("price", 0.0),
                "bid": q.get("bid", 0.0),
                "ask": q.get("ask", 0.0),
                "change_24h_pct": q.get("change_24h_pct", 0.0),
                "spread_pct": q.get("spread_pct", 0.0),
                "ok": True,
            }
        except Exception:
            return sym, {
                "symbol": sym,
                "name": inst["name"],
                "category": inst["category"],
                "badge": inst["badge"],
                "decimals": inst["decimals"],
                "price": 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "change_24h_pct": 0.0,
                "spread_pct": 0.0,
                "ok": False,
            }

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_load_one, inst) for inst in instruments]
        for fut in as_completed(futures):
            sym, item = fut.result()
            results[sym] = item

    ordered = [results[i["symbol"]] for i in instruments if i["symbol"] in results]
    return WATCHLIST_CACHE.set(cat_key, ordered)
