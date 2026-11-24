"""
Pool de Conexões CCXT, Failover de Mirrors e Telemetria Binance (FyNex Quant AI)
================================================================================
Gerencia instâncias `ccxt.binance` em cache thread-safe, failover automático entre
mirrors oficiais Spot (incluindo fallback público global para geo-block HTTP 451),
retry inteligente com backoff e diagnóstico de saúde/clock drift.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import ccxt

from core.infra.cache import TTLCache

logger = logging.getLogger("BinanceClient")

BINANCE_SPOT_MIRRORS: Tuple[str, ...] = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://data-api.binance.vision",
)
BINANCE_PUBLIC_FALLBACK_MIRROR = "https://data-api.binance.vision"
_BINANCE_HOST_RE = re.compile(r"https://(?:api[0-9]?\.binance\.com|data-api\.binance\.vision)")

EXCHANGE_CACHE: TTLCache[Tuple[str, str, bool, int], ccxt.binance] = TTLCache(max_size=16, default_ttl=86400.0)
TICKER_CACHE: TTLCache[Tuple[str, bool], Dict[str, Any]] = TTLCache(max_size=128, default_ttl=2.0)
ORDERBOOK_CACHE: TTLCache[Tuple[str, int, bool], Dict[str, Any]] = TTLCache(max_size=64, default_ttl=2.0)
CANDLES_CACHE: TTLCache[Tuple[str, str, int, bool], Any] = TTLCache(max_size=128, default_ttl=5.0)
BALANCE_CACHE: TTLCache[Tuple[str, bool], Dict[str, Any]] = TTLCache(max_size=32, default_ttl=20.0)
HEALTH_CACHE: TTLCache[bool, Dict[str, Any]] = TTLCache(max_size=4, default_ttl=5.0)

_METRICS_LOCK = threading.Lock()
_ACTIVE_MIRROR_IDX = 0
_CONNECTION_METRICS: Dict[str, Any] = {
    "total_requests": 0,
    "failed_requests": 0,
    "retries": 0,
    "mirror_switches": 0,
    "last_latency_ms": None,
    "avg_latency_ms": None,
    "last_success_ts": None,
    "last_error": None,
    "last_error_ts": None,
    "consecutive_failures": 0,
}


def _sleep(seconds: float) -> None:
    bc = sys.modules.get("binance_client")
    time_mod = getattr(bc, "time", time) if bc is not None else time
    time_mod.sleep(seconds)


def clear_exchange_cache() -> None:
    """Limpa todas as instâncias e caches de mercado/carteira em memória."""
    EXCHANGE_CACHE.clear()
    TICKER_CACHE.clear()
    ORDERBOOK_CACHE.clear()
    CANDLES_CACHE.clear()
    BALANCE_CACHE.clear()
    HEALTH_CACHE.clear()


def _get_active_mirror() -> str:
    with _METRICS_LOCK:
        return BINANCE_SPOT_MIRRORS[_ACTIVE_MIRROR_IDX % len(BINANCE_SPOT_MIRRORS)]


def _rotate_mirror(reason: str = "") -> str:
    """Avança para o próximo mirror oficial da Binance Spot em produção."""
    global _ACTIVE_MIRROR_IDX
    with _METRICS_LOCK:
        _ACTIVE_MIRROR_IDX = (_ACTIVE_MIRROR_IDX + 1) % len(BINANCE_SPOT_MIRRORS)
        _CONNECTION_METRICS["mirror_switches"] += 1
        new_mirror = BINANCE_SPOT_MIRRORS[_ACTIVE_MIRROR_IDX]
    EXCHANGE_CACHE.delete_matching(lambda k: not k[2])
    logger.warning(f"🔀 Rotacionando mirror Binance Spot para {new_mirror} ({reason or 'falha de rede'}).")
    return new_mirror


def _apply_mirror_to_exchange(exchange: ccxt.binance, mirror_base: str) -> None:
    """Aplica o mirror base às URLs Spot do CCXT sem redirecionar endpoints privados para `data-api.binance.vision`."""
    try:
        api_urls = exchange.urls.get("api")
        public_only = mirror_base == BINANCE_PUBLIC_FALLBACK_MIRROR
        if isinstance(api_urls, dict):
            for key, val in list(api_urls.items()):
                if public_only and any(tok in str(key).lower() for tok in ("private", "sapi", "wapi", "dapi", "fapi")):
                    continue
                if isinstance(val, str) and _BINANCE_HOST_RE.search(val):
                    api_urls[key] = _BINANCE_HOST_RE.sub(mirror_base, val)
    except Exception:
        pass


def _record_request_result(latency_ms: Optional[float], error: Optional[str] = None, retried: bool = False) -> None:
    now = time.time()
    with _METRICS_LOCK:
        _CONNECTION_METRICS["total_requests"] += 1
        if retried:
            _CONNECTION_METRICS["retries"] += 1
        if error is None:
            _CONNECTION_METRICS["consecutive_failures"] = 0
            _CONNECTION_METRICS["last_success_ts"] = now
            if latency_ms is not None:
                _CONNECTION_METRICS["last_latency_ms"] = round(latency_ms, 1)
                prev_avg = _CONNECTION_METRICS["avg_latency_ms"]
                _CONNECTION_METRICS["avg_latency_ms"] = (
                    round(latency_ms, 1)
                    if prev_avg is None
                    else round(prev_avg * 0.8 + latency_ms * 0.2, 1)
                )
        else:
            _CONNECTION_METRICS["failed_requests"] += 1
            _CONNECTION_METRICS["consecutive_failures"] += 1
            _CONNECTION_METRICS["last_error"] = str(error)[:240]
            _CONNECTION_METRICS["last_error_ts"] = now


def get_connection_metrics() -> Dict[str, Any]:
    """Retorna snapshot thread-safe das métricas de conectividade com a Binance."""
    with _METRICS_LOCK:
        snap = dict(_CONNECTION_METRICS)
        mirror = BINANCE_SPOT_MIRRORS[_ACTIVE_MIRROR_IDX % len(BINANCE_SPOT_MIRRORS)]
    cf = snap["consecutive_failures"]
    snap["status"] = "healthy" if cf == 0 else ("degraded" if cf < 3 else "offline")
    snap["active_mirror"] = mirror
    return snap


def call_with_retry(
    fn: Callable[[], Any],
    *,
    exchange: Optional[ccxt.binance] = None,
    testnet: bool = False,
    max_attempts: int = 3,
    base_delay: float = 0.4,
    allow_mirror_failover: bool = True,
):
    """
    Executa chamada à API Binance com retry inteligente:
    - Re-sincroniza relógio em erro -1021
    - Backoff exponencial em RateLimitExceeded / DDoSProtection
    - Rotaciona mirror oficial (api1..api4 / data-api) em falhas de rede em produção
    """
    global _ACTIVE_MIRROR_IDX
    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        t0 = time.perf_counter()
        try:
            res = fn()
            latency_ms = (time.perf_counter() - t0) * 1000.0
            _record_request_result(latency_ms, error=None, retried=(attempt > 1))
            return res
        except (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.InvalidOrder, ccxt.InsufficientFunds) as e:
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            raise
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
            last_exc = e
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            if attempt < max_attempts:
                _sleep(min(8.0, base_delay * 3.0 * (2 ** (attempt - 1))))
                continue
            raise
        except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
            last_exc = e
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            if not testnet and allow_mirror_failover and attempt < max_attempts:
                err_str = str(e).lower()
                is_authenticated = bool(getattr(exchange, "apiKey", None))
                if ("451" in err_str or "restricted location" in err_str) and not is_authenticated:
                    with _METRICS_LOCK:
                        _ACTIVE_MIRROR_IDX = BINANCE_SPOT_MIRRORS.index(BINANCE_PUBLIC_FALLBACK_MIRROR)
                        _CONNECTION_METRICS["mirror_switches"] += 1
                    EXCHANGE_CACHE.delete_matching(lambda k: not k[2])
                    new_mirror = BINANCE_PUBLIC_FALLBACK_MIRROR
                    logger.warning(f"🌎 Geo-block HTTP 451 detectado! Alternando para mirror público global {new_mirror}.")
                else:
                    new_mirror = _rotate_mirror(type(e).__name__)
                    if is_authenticated and new_mirror == BINANCE_PUBLIC_FALLBACK_MIRROR:
                        new_mirror = _rotate_mirror("skip_public_only_mirror_for_auth")
                if exchange is not None:
                    _apply_mirror_to_exchange(exchange, new_mirror)
            if attempt < max_attempts:
                _sleep(min(5.0, base_delay * (2 ** (attempt - 1))))
                continue
            raise
        except ccxt.ExchangeError as e:
            last_exc = e
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            if "-1021" in str(e) and attempt < max_attempts and exchange is not None:
                try:
                    exchange.load_time_difference()
                except Exception:
                    pass
                _sleep(0.25)
                continue
            raise
        except Exception as e:
            last_exc = e
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Falha desconhecida em call_with_retry")


def get_binance_exchange(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    testnet: bool = False,
    timeout: int = 15000,
    use_cache: bool = True,
) -> ccxt.binance:
    """Instancia ou reutiliza do cache thread-safe o cliente `ccxt.binance`."""
    clean_key = api_key.strip() if api_key else ""
    clean_secret = api_secret.strip() if api_secret else ""
    cache_key = (clean_key, clean_secret, bool(testnet), int(timeout))

    if use_cache:
        hit, cached = EXCHANGE_CACHE.get(cache_key)
        if hit and cached is not None:
            return cached

    config: Dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": timeout,
        "options": {
            "defaultType": "spot",
            "fetchMarkets": ["spot"],
            "adjustForTimeDifference": True,
            "recvWindow": 10000,
        },
    }
    if clean_key and clean_secret:
        config["apiKey"] = clean_key
        config["secret"] = clean_secret

    exchange = ccxt.binance(config)
    if testnet:
        exchange.set_sandbox_mode(True)
    else:
        active_mirror = _get_active_mirror()
        if active_mirror != BINANCE_SPOT_MIRRORS[0]:
            _apply_mirror_to_exchange(exchange, active_mirror)

    if use_cache:
        EXCHANGE_CACHE.set(cache_key, exchange)
    return exchange


def _resolve_get_exchange():
    bc = sys.modules.get("binance_client")
    if bc is not None and hasattr(bc, "get_binance_exchange"):
        return bc.get_binance_exchange
    return get_binance_exchange


def check_binance_health(testnet: bool = False, ttl_seconds: float = 5.0) -> Dict[str, Any]:
    """Diagnóstico leve da conexão pública Binance: latência, clock drift, mirror ativo e métricas."""
    hit, cached = HEALTH_CACHE.get(bool(testnet), ttl_seconds=ttl_seconds)
    if hit and cached is not None:
        return cached

    exchange = _resolve_get_exchange()(testnet=testnet, timeout=6000)
    t0 = time.perf_counter()
    try:
        server_time_ms = int(
            call_with_retry(
                lambda: exchange.fetch_time(),
                exchange=exchange,
                testnet=testnet,
                max_attempts=2,
            )
        )
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        clock_drift_ms = server_time_ms - int(time.time() * 1000)
        metrics = get_connection_metrics()
        result = {
            "ok": True,
            "status": "healthy" if abs(clock_drift_ms) < 5000 else "clock_warning",
            "testnet": bool(testnet),
            "latency_ms": latency_ms,
            "server_time_ms": server_time_ms,
            "clock_drift_ms": clock_drift_ms,
            "clock_synced": abs(clock_drift_ms) < 5000,
            "active_mirror": "testnet.binance.vision" if testnet else metrics["active_mirror"],
            "metrics": metrics,
        }
    except Exception as e:
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        metrics = get_connection_metrics()
        result = {
            "ok": False,
            "status": "offline",
            "testnet": bool(testnet),
            "latency_ms": latency_ms,
            "server_time_ms": None,
            "clock_drift_ms": None,
            "clock_synced": False,
            "active_mirror": "testnet.binance.vision" if testnet else metrics["active_mirror"],
            "error": str(e),
            "metrics": metrics,
        }

    return HEALTH_CACHE.set(bool(testnet), result)
