"""
Módulo de Mercado Forex & Mesa de Operações estilo XTB (FyNex Quant AI)
=======================================================================
Fornece:
- Catálogo unificado de instrumentos (Cripto Binance Spot + Forex / Moedas estilo XTB)
- Cotações e candles OHLCV ao vivo de pares de moedas (EUR/USD, GBP/USD, USD/JPY,
  USD/BRL, EUR/BRL, AUD/USD, USD/CHF, XAU/USD) via feed público JSON com cache curto
- Motor de operações manuais (BUY / SELL com Lote, Alavancagem, Stop Loss, Take Profit,
  Margem e PnL flutuante em tempo real) persistido em SQLite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import binance_client

# -----------------------------------------------------------------------------
# Catálogo de Instrumentos (Cripto + Forex estilo XTB)
# -----------------------------------------------------------------------------
MARKET_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    # --- Cripto (Binance Spot) ---
    "BTC/USDT": {
        "symbol": "BTC/USDT",
        "name": "Bitcoin / Tether",
        "category": "crypto",
        "badge": "CRIPTO",
        "decimals": 2,
        "pip_size": 1.0,
        "contract_size": 1.0,
        "default_lot": 0.01,
        "min_lot": 0.001,
        "max_leverage": 10,
        "typical_spread_pips": 1.0,
    },
    "ETH/USDT": {
        "symbol": "ETH/USDT",
        "name": "Ethereum / Tether",
        "category": "crypto",
        "badge": "CRIPTO",
        "decimals": 2,
        "pip_size": 0.1,
        "contract_size": 1.0,
        "default_lot": 0.1,
        "min_lot": 0.01,
        "max_leverage": 10,
        "typical_spread_pips": 0.5,
    },
    "SOL/USDT": {
        "symbol": "SOL/USDT",
        "name": "Solana / Tether",
        "category": "crypto",
        "badge": "CRIPTO",
        "decimals": 2,
        "pip_size": 0.01,
        "contract_size": 1.0,
        "default_lot": 1.0,
        "min_lot": 0.1,
        "max_leverage": 10,
        "typical_spread_pips": 2.0,
    },
    "BNB/USDT": {
        "symbol": "BNB/USDT",
        "name": "BNB / Tether",
        "category": "crypto",
        "badge": "CRIPTO",
        "decimals": 2,
        "pip_size": 0.01,
        "contract_size": 1.0,
        "default_lot": 0.5,
        "min_lot": 0.05,
        "max_leverage": 10,
        "typical_spread_pips": 2.0,
    },
    "XRP/USDT": {
        "symbol": "XRP/USDT",
        "name": "Ripple / Tether",
        "category": "crypto",
        "badge": "CRIPTO",
        "decimals": 4,
        "pip_size": 0.0001,
        "contract_size": 1.0,
        "default_lot": 100.0,
        "min_lot": 10.0,
        "max_leverage": 10,
        "typical_spread_pips": 1.5,
    },
    # --- Forex & Metais (Estilo XTB) ---
    "EUR/USD": {
        "symbol": "EUR/USD",
        "name": "Euro / Dólar EUA",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "EURUSD=X",
        "decimals": 5,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 100,
        "typical_spread_pips": 0.8,
    },
    "GBP/USD": {
        "symbol": "GBP/USD",
        "name": "Libra / Dólar EUA",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "GBPUSD=X",
        "decimals": 5,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 100,
        "typical_spread_pips": 1.1,
    },
    "USD/JPY": {
        "symbol": "USD/JPY",
        "name": "Dólar EUA / Iene",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "USDJPY=X",
        "decimals": 3,
        "pip_size": 0.01,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 100,
        "typical_spread_pips": 0.9,
    },
    "USD/BRL": {
        "symbol": "USD/BRL",
        "name": "Dólar EUA / Real BR",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "USDBRL=X",
        "decimals": 4,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 30,
        "typical_spread_pips": 12.0,
    },
    "EUR/BRL": {
        "symbol": "EUR/BRL",
        "name": "Euro / Real BR",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "EURBRL=X",
        "decimals": 4,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 30,
        "typical_spread_pips": 15.0,
    },
    "AUD/USD": {
        "symbol": "AUD/USD",
        "name": "Dólar Australiano / USD",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "AUDUSD=X",
        "decimals": 5,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 100,
        "typical_spread_pips": 1.0,
    },
    "USD/CHF": {
        "symbol": "USD/CHF",
        "name": "Dólar EUA / Franco Suíço",
        "category": "forex",
        "badge": "FOREX",
        "yahoo": "USDCHF=X",
        "decimals": 5,
        "pip_size": 0.0001,
        "contract_size": 100_000.0,
        "default_lot": 0.10,
        "min_lot": 0.01,
        "max_leverage": 100,
        "typical_spread_pips": 1.1,
    },
    "XAU/USD": {
        "symbol": "XAU/USD",
        "name": "Ouro Spot / Dólar EUA",
        "category": "forex",
        "badge": "METAL",
        "yahoo": "GC=F",
        "decimals": 2,
        "pip_size": 0.1,
        "contract_size": 100.0,
        "default_lot": 0.05,
        "min_lot": 0.01,
        "max_leverage": 50,
        "typical_spread_pips": 2.5,
    },
}

_FOREX_QUOTE_CACHE_LOCK = threading.Lock()
_FOREX_QUOTE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_FOREX_CANDLES_CACHE_LOCK = threading.Lock()
_FOREX_CANDLES_CACHE: Dict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]] = {}

_YAHOO_TF_MAP: Dict[str, Tuple[str, str]] = {
    "1m": ("1m", "1d"),
    "3m": ("2m", "1d"),
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "5d"),
    "1h": ("60m", "1mo"),
}


def is_forex_symbol(symbol: str) -> bool:
    info = MARKET_INSTRUMENTS.get((symbol or "").upper())
    return bool(info and info.get("category") == "forex")


def list_instruments(category: Optional[str] = None) -> List[Dict[str, Any]]:
    items = list(MARKET_INSTRUMENTS.values())
    if category and category.lower() in ("crypto", "forex"):
        items = [i for i in items if i["category"] == category.lower()]
    return items


def clear_forex_caches() -> None:
    with _FOREX_QUOTE_CACHE_LOCK:
        _FOREX_QUOTE_CACHE.clear()
    with _FOREX_CANDLES_CACHE_LOCK:
        _FOREX_CANDLES_CACHE.clear()


def _http_get_json(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FyNexQuantAI/2.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _fetch_yahoo_chart(yahoo_ticker: str, interval: str = "1m", range_str: str = "1d") -> Dict[str, Any]:
    encoded = urllib.parse.quote(yahoo_ticker, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?interval={interval}&range={range_str}&includePrePost=false"
    )
    data = _http_get_json(url, timeout=6.0)
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        raise ValueError(f"Sem dados de mercado retornados para {yahoo_ticker}")
    return results[0]


def fetch_forex_quote(symbol: str, ttl_seconds: float = 5.0) -> Dict[str, Any]:
    """Retorna cotação ao vivo de um par Forex/Metal com Bid/Ask, spread em pips e estatísticas 24h."""
    sym = symbol.upper()
    info = MARKET_INSTRUMENTS.get(sym)
    if not info or info.get("category") != "forex":
        raise ValueError(f"Par Forex não suportado: {symbol}")

    now = time.monotonic()
    with _FOREX_QUOTE_CACHE_LOCK:
        cached = _FOREX_QUOTE_CACHE.get(sym)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    t0 = time.perf_counter()
    chart = _fetch_yahoo_chart(info["yahoo"], interval="5m", range_str="1d")
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    meta = chart.get("meta") or {}
    quote_block = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quote_block.get("close") or []) if c is not None]
    highs = [h for h in (quote_block.get("high") or []) if h is not None]
    lows = [l for l in (quote_block.get("low") or []) if l is not None]
    vols = [v for v in (quote_block.get("volume") or []) if v is not None]

    price = float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0.0))
    prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or (closes[0] if closes else price))
    change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    high_24h = float(meta.get("regularMarketDayHigh") or (max(highs) if highs else price))
    low_24h = float(meta.get("regularMarketDayLow") or (min(lows) if lows else price))
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
        "category": "forex",
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

    with _FOREX_QUOTE_CACHE_LOCK:
        _FOREX_QUOTE_CACHE[sym] = (now, result)
    return result


def fetch_forex_candles(
    symbol: str,
    timeframe: str = "1m",
    limit: int = 150,
    ttl_seconds: float = 10.0,
) -> List[Dict[str, Any]]:
    """Retorna candles OHLCV de Forex formatados para o TradingView Lightweight Charts."""
    sym = symbol.upper()
    info = MARKET_INSTRUMENTS.get(sym)
    if not info or info.get("category") != "forex":
        raise ValueError(f"Par Forex não suportado: {symbol}")

    safe_limit = max(1, min(int(limit), 1000))
    cache_key = (sym, timeframe, safe_limit)
    now = time.monotonic()
    with _FOREX_CANDLES_CACHE_LOCK:
        cached = _FOREX_CANDLES_CACHE.get(cache_key)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    interval, range_str = _YAHOO_TF_MAP.get(timeframe, ("5m", "5d"))
    chart = _fetch_yahoo_chart(info["yahoo"], interval=interval, range_str=range_str)
    timestamps = chart.get("timestamp") or []
    quote_block = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    candles: List[Dict[str, Any]] = []
    seen_times = set()
    for i, ts in enumerate(timestamps):
        if ts is None or i >= len(closes):
            continue
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        t_int = int(ts)
        if t_int in seen_times:
            continue
        seen_times.add(t_int)
        v = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        candles.append({
            "time": t_int,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": v,
        })

    candles.sort(key=lambda x: x["time"])
    candles = candles[-safe_limit:]

    with _FOREX_CANDLES_CACHE_LOCK:
        _FOREX_CANDLES_CACHE[cache_key] = (now, candles)
    return candles


def fetch_any_quote(symbol: str) -> Dict[str, Any]:
    """Busca cotação ao vivo tanto para Cripto (Binance) quanto para Forex (XTB feed)."""
    if is_forex_symbol(symbol):
        return fetch_forex_quote(symbol)
    ticker = binance_client.fetch_live_ticker(symbol=symbol, testnet=False)
    info = MARKET_INSTRUMENTS.get(symbol.upper(), {})
    ticker["category"] = "crypto"
    ticker["badge"] = info.get("badge", "CRIPTO")
    ticker["name"] = info.get("name", symbol)
    return ticker


def fetch_watchlist(category: str = "all") -> List[Dict[str, Any]]:
    """Retorna cotações resumidas para a Watchlist estilo XTB (em paralelo)."""
    instruments = list_instruments(category if category in ("crypto", "forex") else None)
    results: Dict[str, Dict[str, Any]] = {}

    def _load_one(inst: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        sym = inst["symbol"]
        try:
            q = fetch_any_quote(sym)
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

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_load_one, inst) for inst in instruments]
        for fut in as_completed(futures):
            sym, item = fut.result()
            results[sym] = item

    # Preserva ordem original do catálogo
    return [results[i["symbol"]] for i in instruments if i["symbol"] in results]


# -----------------------------------------------------------------------------
# Motor da Mesa de Operações XTB (Simulador Multi-Ativos Forex + Cripto)
# -----------------------------------------------------------------------------
DEFAULT_XTB_CAPITAL_USD = 10_000.0


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class XTBDeskStore:
    """
    Gerencia posições abertas e histórico de operações manuais estilo corretora XTB
    (BUY/SELL, lotes, alavancagem, margem, SL/TP e PnL flutuante em USD).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xtb_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots REAL NOT NULL,
                    contract_size REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    margin_usd REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    opened_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xtb_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    side TEXT NOT NULL,
                    lots REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    margin_usd REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    reason TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                );
            """)
            conn.commit()

    @staticmethod
    def calculate_notional_usd(symbol: str, lots: float, price: float, contract_size: float) -> float:
        """
        Calcula valor nocional aproximado em USD da posição:
        - XXX/USD ou XXX/USDT (ex.: EUR/USD, BTC/USDT, XAU/USD): lots * contract_size * price
        - USD/XXX (ex.: USD/JPY, USD/BRL, USD/CHF): lots * contract_size (base já é USD)
        - Cruzados (ex.: EUR/BRL): lots * contract_size * 1.08 (aproximação conservadora)
        """
        sym = symbol.upper()
        base, _, quote = sym.partition("/")
        units = lots * contract_size
        if quote in ("USD", "USDT"):
            return units * price
        if base == "USD":
            return units
        return units * 1.08

    @staticmethod
    def calculate_pnl_usd(
        symbol: str,
        side: str,
        lots: float,
        contract_size: float,
        entry_price: float,
        current_price: float,
    ) -> float:
        """Calcula PnL em USD de uma operação BUY (Long) ou SELL (Short)."""
        sym = symbol.upper()
        base, _, quote = sym.partition("/")
        units = lots * contract_size
        diff = (current_price - entry_price) if side.upper() == "BUY" else (entry_price - current_price)
        if quote in ("USD", "USDT"):
            return diff * units
        if base == "USD" and current_price > 0:
            return (diff * units) / current_price
        # Cruzados como EUR/BRL -> converte variação de BRL para USD aproximado (~5.0)
        return (diff * units) / max(1.0, current_price / 1.08)

    def _realized_pnl_usd(self, conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0.0) AS total FROM xtb_trades").fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def _used_margin_usd(self, conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT COALESCE(SUM(margin_usd), 0.0) AS total FROM xtb_positions").fetchone()
        return float(row["total"] or 0.0) if row else 0.0

    def open_position(
        self,
        symbol: str,
        side: str,
        lots: float,
        leverage: int = 30,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        quote_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sym = symbol.upper()
        inst = MARKET_INSTRUMENTS.get(sym)
        if not inst:
            raise ValueError(f"Instrumento '{symbol}' não suportado na mesa XTB.")

        side_u = (side or "").upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError("Lado da ordem inválido: use 'BUY' ou 'SELL'.")

        lots_f = float(lots)
        if lots_f < float(inst["min_lot"]) or lots_f > 1000.0:
            raise ValueError(f"Lote fora do intervalo permitido (mínimo {inst['min_lot']}).")

        lev = max(1, min(int(leverage), int(inst["max_leverage"])))
        quote = quote_override if quote_override is not None else fetch_any_quote(sym)
        entry_price = float(quote["ask"] if side_u == "BUY" else quote["bid"])
        if entry_price <= 0:
            entry_price = float(quote.get("price") or 0.0)
        if entry_price <= 0:
            raise ValueError(f"Cotação indisponível no momento para {sym}.")

        if sl_price is not None and sl_price > 0:
            if side_u == "BUY" and sl_price >= entry_price:
                raise ValueError("Stop Loss em ordem BUY deve ficar abaixo do preço de entrada.")
            if side_u == "SELL" and sl_price <= entry_price:
                raise ValueError("Stop Loss em ordem SELL deve ficar acima do preço de entrada.")
        else:
            sl_price = None

        if tp_price is not None and tp_price > 0:
            if side_u == "BUY" and tp_price <= entry_price:
                raise ValueError("Take Profit em ordem BUY deve ficar acima do preço de entrada.")
            if side_u == "SELL" and tp_price >= entry_price:
                raise ValueError("Take Profit em ordem SELL deve ficar abaixo do preço de entrada.")
        else:
            tp_price = None

        contract_size = float(inst["contract_size"])
        notional = self.calculate_notional_usd(sym, lots_f, entry_price, contract_size)
        margin_usd = round(notional / lev, 2)

        with self._connect() as conn:
            balance = DEFAULT_XTB_CAPITAL_USD + self._realized_pnl_usd(conn)
            free_margin = balance - self._used_margin_usd(conn)
            if margin_usd > free_margin:
                raise ValueError(
                    f"Margem livre insuficiente: necessária ${margin_usd:,.2f}, disponível ${max(0.0, free_margin):,.2f}."
                )
            opened_at = _utc_now_str()
            cur = conn.execute(
                """
                INSERT INTO xtb_positions
                    (symbol, category, side, lots, contract_size, leverage, entry_price, margin_usd, sl_price, tp_price, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sym, inst["category"], side_u, lots_f, contract_size, lev, entry_price, margin_usd, sl_price, tp_price, opened_at),
            )
            pos_id = cur.lastrowid
            conn.commit()

        return {
            "id": pos_id,
            "symbol": sym,
            "category": inst["category"],
            "side": side_u,
            "lots": lots_f,
            "leverage": lev,
            "entry_price": entry_price,
            "margin_usd": margin_usd,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "opened_at": opened_at,
        }

    def close_position(
        self,
        position_id: int,
        reason: str = "MANUAL",
        quote_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM xtb_positions WHERE id = ?", (int(position_id),)).fetchone()
            if not row:
                raise ValueError(f"Posição #{position_id} não encontrada.")
            pos = dict(row)

        quote = quote_override if quote_override is not None else fetch_any_quote(pos["symbol"])
        # BUY fecha no BID; SELL fecha no ASK
        exit_price = float(quote["bid"] if pos["side"] == "BUY" else quote["ask"])
        if exit_price <= 0:
            exit_price = float(quote.get("price") or pos["entry_price"])

        pnl_usd = round(
            self.calculate_pnl_usd(
                pos["symbol"], pos["side"], pos["lots"], pos["contract_size"], pos["entry_price"], exit_price
            ),
            2,
        )
        pnl_pct = round((pnl_usd / pos["margin_usd"] * 100.0), 2) if pos["margin_usd"] > 0 else 0.0
        closed_at = _utc_now_str()

        with self._connect() as conn:
            conn.execute("DELETE FROM xtb_positions WHERE id = ?", (pos["id"],))
            conn.execute(
                """
                INSERT INTO xtb_trades
                    (symbol, category, side, lots, leverage, entry_price, exit_price, margin_usd, pnl_usd, pnl_pct, reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos["symbol"], pos["category"], pos["side"], pos["lots"], pos["leverage"],
                    pos["entry_price"], exit_price, pos["margin_usd"], pnl_usd, pnl_pct,
                    reason, pos["opened_at"], closed_at,
                ),
            )
            conn.commit()

        return {
            "closed_id": pos["id"],
            "symbol": pos["symbol"],
            "side": pos["side"],
            "lots": pos["lots"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "closed_at": closed_at,
        }

    def get_account_snapshot(self, live_prices: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Retorna resumo completo da conta XTB: saldo, equity, margem, posições com PnL vivo e histórico."""
        with self._connect() as conn:
            pos_rows = [dict(r) for r in conn.execute("SELECT * FROM xtb_positions ORDER BY id DESC").fetchall()]
            trade_rows = [dict(r) for r in conn.execute("SELECT * FROM xtb_trades ORDER BY id DESC LIMIT 30").fetchall()]
            realized_pnl = self._realized_pnl_usd(conn)

        quotes_map: Dict[str, Dict[str, Any]] = dict(live_prices or {})
        unique_syms = {p["symbol"] for p in pos_rows if p["symbol"] not in quotes_map}
        for sym in unique_syms:
            try:
                quotes_map[sym] = fetch_any_quote(sym)
            except Exception:
                quotes_map[sym] = {"price": 0.0, "bid": 0.0, "ask": 0.0}

        enriched_positions: List[Dict[str, Any]] = []
        floating_pnl_total = 0.0
        used_margin = 0.0

        for pos in pos_rows:
            q = quotes_map.get(pos["symbol"]) or {}
            mark = float(q.get("bid") if pos["side"] == "BUY" else q.get("ask"))
            if mark <= 0:
                mark = float(q.get("price") or pos["entry_price"])

            # Verifica disparo automático de SL/TP se o preço cruzou o alvo
            sl = pos.get("sl_price")
            tp = pos.get("tp_price")
            auto_reason = None
            if sl is not None:
                if (pos["side"] == "BUY" and mark <= sl) or (pos["side"] == "SELL" and mark >= sl):
                    auto_reason = "STOP_LOSS"
            if auto_reason is None and tp is not None:
                if (pos["side"] == "BUY" and mark >= tp) or (pos["side"] == "SELL" and mark <= tp):
                    auto_reason = "TAKE_PROFIT"

            if auto_reason is not None:
                closed = self.close_position(pos["id"], reason=auto_reason, quote_override=q)
                realized_pnl += closed["pnl_usd"]
                trade_rows.insert(0, {
                    "id": closed["closed_id"],
                    "symbol": pos["symbol"],
                    "category": pos["category"],
                    "side": pos["side"],
                    "lots": pos["lots"],
                    "leverage": pos["leverage"],
                    "entry_price": pos["entry_price"],
                    "exit_price": closed["exit_price"],
                    "margin_usd": pos["margin_usd"],
                    "pnl_usd": closed["pnl_usd"],
                    "pnl_pct": closed["pnl_pct"],
                    "reason": auto_reason,
                    "opened_at": pos["opened_at"],
                    "closed_at": closed["closed_at"],
                })
                continue

            pnl_usd = round(
                self.calculate_pnl_usd(
                    pos["symbol"], pos["side"], pos["lots"], pos["contract_size"], pos["entry_price"], mark
                ),
                2,
            )
            pnl_pct = round((pnl_usd / pos["margin_usd"] * 100.0), 2) if pos["margin_usd"] > 0 else 0.0
            floating_pnl_total += pnl_usd
            used_margin += float(pos["margin_usd"])

            inst = MARKET_INSTRUMENTS.get(pos["symbol"], {})
            pos["current_price"] = round(mark, inst.get("decimals", 4))
            pos["pnl_usd"] = pnl_usd
            pos["pnl_pct"] = pnl_pct
            pos["decimals"] = inst.get("decimals", 4)
            enriched_positions.append(pos)

        balance = round(DEFAULT_XTB_CAPITAL_USD + realized_pnl, 2)
        equity = round(balance + floating_pnl_total, 2)
        free_margin = round(equity - used_margin, 2)
        margin_level_pct = round((equity / used_margin * 100.0), 1) if used_margin > 0 else None

        return {
            "balance_usd": balance,
            "equity_usd": equity,
            "used_margin_usd": round(used_margin, 2),
            "free_margin_usd": free_margin,
            "margin_level_pct": margin_level_pct,
            "floating_pnl_usd": round(floating_pnl_total, 2),
            "realized_pnl_usd": round(realized_pnl, 2),
            "open_positions": enriched_positions,
            "recent_trades": trade_rows[:30],
        }
