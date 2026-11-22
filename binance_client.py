"""
Módulo de Integração com a API Binance (FyNex Quant AI)
======================================================
Responsável pelo gerenciamento de credenciais, conexão segura via CCXT,
consulta de saldos reais da carteira Spot, validação de chaves e execução de ordens
reais (Live) ou de teste (Testnet), incluindo proteção OCO do lado da exchange.
"""

import os
import re
import time
import uuid
import logging
import tempfile
import threading
from typing import Dict, Any, Optional, List, Tuple

import ccxt
from dotenv import dotenv_values

logger = logging.getLogger("BinanceClient")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

_EXCHANGE_CACHE_LOCK = threading.Lock()
_EXCHANGE_CACHE: Dict[Tuple[str, str, bool, int], ccxt.binance] = {}
_TICKER_CACHE_LOCK = threading.Lock()
_TICKER_CACHE: Dict[Tuple[str, bool], Tuple[float, Dict[str, Any]]] = {}
_ORDERBOOK_CACHE_LOCK = threading.Lock()
_ORDERBOOK_CACHE: Dict[Tuple[str, int, bool], Tuple[float, Dict[str, Any]]] = {}
_CANDLES_CACHE_LOCK = threading.Lock()
_CANDLES_CACHE: Dict[Tuple[str, str, int, bool], Tuple[float, List[Dict[str, Any]]]] = {}
_BALANCE_CACHE_LOCK = threading.Lock()
_BALANCE_CACHE: Dict[Tuple[str, bool], Tuple[float, Dict[str, Any]]] = {}
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE: Dict[bool, Tuple[float, Dict[str, Any]]] = {}

# Mirrors oficiais públicos/Spot da Binance para failover automático em produção
# (data-api.binance.vision fornece dados públicos sem restrição geográfica HTTP 451)
BINANCE_SPOT_MIRRORS: Tuple[str, ...] = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://data-api.binance.vision",
)
BINANCE_PUBLIC_FALLBACK_MIRROR = "https://data-api.binance.vision"

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

VALID_MODES = ("paper", "testnet", "live")
API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")

# Valores padrão dos limites de risco (podem ser sobrescritos no .env)
RISK_DEFAULTS = {
    "MAX_DAILY_LOSS_USDT": 20.0,
    "MAX_TRADES_PER_DAY": 10,
    "MAX_TRADE_AMOUNT_USDT": 200.0,
}


class EnvironmentConfigError(ValueError):
    """Combinação incoerente entre modo de operação e ambiente da Binance."""


# -----------------------------------------------------------------------------
# Configuração (.env)
# -----------------------------------------------------------------------------
def _get(env_data: Dict[str, Optional[str]], key: str, default: str = "") -> str:
    return (env_data.get(key) or os.environ.get(key) or default).strip()


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_binance_config() -> Dict[str, Any]:
    """
    Carrega as variáveis de configuração do arquivo .env com valores padrão seguros.
    """
    env_data: Dict[str, Optional[str]] = {}
    if os.path.exists(ENV_PATH):
        env_data = dotenv_values(ENV_PATH)

    api_key = _get(env_data, "BINANCE_API_KEY")
    api_secret = _get(env_data, "BINANCE_API_SECRET")
    testnet = _get(env_data, "BINANCE_TESTNET", "false").lower() in ["true", "1", "yes", "sim"]

    trading_mode = _get(env_data, "TRADING_MODE", "paper").lower()
    if trading_mode not in VALID_MODES:
        trading_mode = "paper"

    trade_amount_usdt = _to_float(_get(env_data, "TRADE_AMOUNT_USDT", "50"), 50.0)

    max_daily_loss = _to_float(_get(env_data, "MAX_DAILY_LOSS_USDT", str(RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"])),
                               RISK_DEFAULTS["MAX_DAILY_LOSS_USDT"])
    max_trades_day = int(_to_float(_get(env_data, "MAX_TRADES_PER_DAY", str(RISK_DEFAULTS["MAX_TRADES_PER_DAY"])),
                                   RISK_DEFAULTS["MAX_TRADES_PER_DAY"]))
    max_trade_amount = _to_float(_get(env_data, "MAX_TRADE_AMOUNT_USDT", str(RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"])),
                                 RISK_DEFAULTS["MAX_TRADE_AMOUNT_USDT"])
    use_oco = _get(env_data, "USE_EXCHANGE_OCO", "true").lower() in ["true", "1", "yes", "sim"]

    masked_key = ""
    if api_key:
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****"

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "api_key_masked": masked_key,
        "has_keys": bool(api_key and api_secret),
        "testnet": testnet,
        "trading_mode": trading_mode,
        "trade_amount_usdt": trade_amount_usdt,
        "max_daily_loss_usdt": max_daily_loss,
        "max_trades_per_day": max_trades_day,
        "max_trade_amount_usdt": max_trade_amount,
        "use_exchange_oco": use_oco,
        "env_path": ENV_PATH,
    }


def resolve_environment(trading_mode: str, keys_are_testnet: bool) -> bool:
    """
    Define de forma única e segura se a exchange deve operar no sandbox (testnet).

    - testnet -> SEMPRE sandbox. Exige que as chaves configuradas sejam de testnet.
    - live    -> SEMPRE produção. Exige que as chaves configuradas sejam de produção.
    - paper   -> dados públicos de PRODUÇÃO (preço real), nenhuma ordem é enviada.

    Lança EnvironmentConfigError quando a combinação é incoerente, evitando que
    o modo "testnet" envie ordens reais (ou que o "live" opere no sandbox).
    """
    mode = (trading_mode or "paper").lower()
    if mode not in VALID_MODES:
        raise EnvironmentConfigError(f"Modo de operação inválido: '{trading_mode}'.")
    if mode == "testnet":
        if not keys_are_testnet:
            raise EnvironmentConfigError(
                "Modo 'testnet' selecionado, mas BINANCE_TESTNET=false (chaves de PRODUÇÃO). "
                "Recusado para evitar ordens reais. Use chaves da testnet e BINANCE_TESTNET=true."
            )
        return True
    if mode == "live":
        if keys_are_testnet:
            raise EnvironmentConfigError(
                "Modo 'live' selecionado, mas BINANCE_TESTNET=true (chaves de TESTNET). "
                "Combinação incoerente recusada."
            )
        return False
    return False  # paper


def validate_api_credential(value: str, label: str) -> str:
    """Valida o formato de uma credencial (evita injeção de linhas no .env)."""
    value = (value or "").strip()
    if not API_KEY_PATTERN.match(value):
        raise ValueError(f"{label} inválida: use apenas letras e números (16 a 128 caracteres).")
    return value


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def save_binance_config(
    api_key: str,
    api_secret: str,
    testnet: bool,
    trading_mode: str,
    trade_amount_usdt: float
) -> bool:
    """
    Salva as configurações com segurança no arquivo .env (gravação atômica).
    Credenciais vazias (ou mascaradas) preservam as já existentes.
    O ambiente é derivado do modo: testnet -> sandbox, live -> produção.
    Os limites de risco existentes no .env são preservados.
    """
    current = load_binance_config()

    final_key = validate_api_credential(api_key, "API Key") if api_key and api_key.strip() else current["api_key"]
    if api_secret and api_secret.strip() and not api_secret.startswith(("*", "•")):
        final_secret = validate_api_credential(api_secret, "API Secret")
    else:
        final_secret = current["api_secret"]

    final_mode = trading_mode.lower() if trading_mode and trading_mode.lower() in VALID_MODES else "paper"
    if final_mode == "testnet":
        final_testnet = True
    elif final_mode == "live":
        final_testnet = False
    else:
        final_testnet = bool(testnet)

    amount = _to_float(str(trade_amount_usdt), 50.0)
    final_amount = min(max(10.0, amount), current["max_trade_amount_usdt"])

    managed_values = {
        "BINANCE_API_KEY": final_key,
        "BINANCE_API_SECRET": final_secret,
        "BINANCE_TESTNET": "true" if final_testnet else "false",
        "TRADING_MODE": final_mode,
        "TRADE_AMOUNT_USDT": f"{final_amount:.2f}",
        "MAX_DAILY_LOSS_USDT": f"{current['max_daily_loss_usdt']:.2f}",
        "MAX_TRADES_PER_DAY": str(current["max_trades_per_day"]),
        "MAX_TRADE_AMOUNT_USDT": f"{current['max_trade_amount_usdt']:.2f}",
        "USE_EXCHANGE_OCO": "true" if current["use_exchange_oco"] else "false",
    }

    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            existing_lines = f.read().splitlines()

        updated_lines: List[str] = []
        seen_keys = set()
        key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
        for line in existing_lines:
            m = key_re.match(line)
            if m and m.group(1) in managed_values:
                k = m.group(1)
                if k not in seen_keys:
                    updated_lines.append(f"{k}={managed_values[k]}")
                    seen_keys.add(k)
            else:
                updated_lines.append(line)

        for k, v in managed_values.items():
            if k not in seen_keys:
                updated_lines.append(f"{k}={v}")

        content = "\n".join(updated_lines) + "\n"
    else:
        content = f"""# ==============================================================================
# FyNex Quant AI - Configurações de Conexão com a Binance
# ==============================================================================

# Credenciais de API (Mantenha em sigilo absoluto! NUNCA habilite permissão de saque)
BINANCE_API_KEY={managed_values['BINANCE_API_KEY']}
BINANCE_API_SECRET={managed_values['BINANCE_API_SECRET']}

# Ambiente das chaves acima (true = Testnet, false = Produção)
BINANCE_TESTNET={managed_values['BINANCE_TESTNET']}

# Modo de Operação do Robô (paper | testnet | live)
TRADING_MODE={managed_values['TRADING_MODE']}

# Valor em USDT alocado para cada compra realizada pela IA
TRADE_AMOUNT_USDT={managed_values['TRADE_AMOUNT_USDT']}

# Limites de Risco (o robô para de abrir posições ao atingi-los)
MAX_DAILY_LOSS_USDT={managed_values['MAX_DAILY_LOSS_USDT']}
MAX_TRADES_PER_DAY={managed_values['MAX_TRADES_PER_DAY']}
MAX_TRADE_AMOUNT_USDT={managed_values['MAX_TRADE_AMOUNT_USDT']}

# Proteção na exchange: envia ordem OCO (Take Profit + Stop-Limit) após cada compra
USE_EXCHANGE_OCO={managed_values['USE_EXCHANGE_OCO']}
"""
    _atomic_write(ENV_PATH, content)
    clear_exchange_cache()
    return True


# -----------------------------------------------------------------------------
# Conexão (Pool Keep-Alive, Failover de Mirrors, Telemetria e Cache)
# -----------------------------------------------------------------------------
def clear_exchange_cache() -> None:
    """Limpa instâncias e caches em memória (chamado ao alterar credenciais no .env)."""
    with _EXCHANGE_CACHE_LOCK:
        _EXCHANGE_CACHE.clear()
    with _TICKER_CACHE_LOCK:
        _TICKER_CACHE.clear()
    with _ORDERBOOK_CACHE_LOCK:
        _ORDERBOOK_CACHE.clear()
    with _CANDLES_CACHE_LOCK:
        _CANDLES_CACHE.clear()
    with _BALANCE_CACHE_LOCK:
        _BALANCE_CACHE.clear()
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE.clear()


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
    with _EXCHANGE_CACHE_LOCK:
        # Remove apenas instâncias de produção (testnet=False) do cache
        prod_keys = [k for k in _EXCHANGE_CACHE if not k[2]]
        for k in prod_keys:
            _EXCHANGE_CACHE.pop(k, None)
    logger.warning(f"🔀 Rotacionando mirror Binance Spot para {new_mirror} ({reason or 'falha de rede'}).")
    return new_mirror


_BINANCE_HOST_RE = re.compile(r"https://(?:api[0-9]?\.binance\.com|data-api\.binance\.vision)")


def _apply_mirror_to_exchange(exchange: ccxt.binance, mirror_base: str) -> None:
    """Aplica o mirror base (ex.: https://api1.binance.com ou data-api.binance.vision) às URLs Spot do CCXT."""
    try:
        api_urls = exchange.urls.get("api")
        if isinstance(api_urls, dict):
            for key, val in list(api_urls.items()):
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
    if cf == 0:
        status = "healthy"
    elif cf < 3:
        status = "degraded"
    else:
        status = "offline"
    snap["status"] = status
    snap["active_mirror"] = mirror
    return snap


def call_with_retry(
    fn,
    *,
    exchange: Optional[ccxt.binance] = None,
    testnet: bool = False,
    max_attempts: int = 3,
    base_delay: float = 0.4,
    allow_mirror_failover: bool = True,
):
    """
    Executa uma chamada à API da Binance com retry inteligente:
    - Re-sincroniza relógio automaticamente em erro -1021
    - Aplica backoff em RateLimitExceeded / DDoSProtection
    - Rotaciona mirror oficial (api1..api4) em NetworkError / Timeout / ExchangeNotAvailable (produção)
    """
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
                time.sleep(min(8.0, base_delay * 3.0 * (2 ** (attempt - 1))))
                continue
            raise
        except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
            last_exc = e
            _record_request_result(None, error=str(e), retried=(attempt > 1))
            if not testnet and allow_mirror_failover and attempt < max_attempts:
                err_str = str(e).lower()
                if "451" in err_str or "restricted location" in err_str:
                    global _ACTIVE_MIRROR_IDX
                    with _METRICS_LOCK:
                        _ACTIVE_MIRROR_IDX = BINANCE_SPOT_MIRRORS.index(BINANCE_PUBLIC_FALLBACK_MIRROR)
                        _CONNECTION_METRICS["mirror_switches"] += 1
                    with _EXCHANGE_CACHE_LOCK:
                        for k in [k for k in _EXCHANGE_CACHE if not k[2]]:
                            _EXCHANGE_CACHE.pop(k, None)
                    new_mirror = BINANCE_PUBLIC_FALLBACK_MIRROR
                    logger.warning(f"🌎 Geo-block HTTP 451 detectado! Alternando para mirror público global {new_mirror}.")
                else:
                    new_mirror = _rotate_mirror(type(e).__name__)
                if exchange is not None:
                    _apply_mirror_to_exchange(exchange, new_mirror)
            if attempt < max_attempts:
                time.sleep(min(5.0, base_delay * (2 ** (attempt - 1))))
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
                time.sleep(0.25)
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
    """
    Instancia (ou reutiliza do cache thread-safe) o cliente `ccxt.binance` com
    sessão HTTP persistente (keep-alive), rate-limit automático, failover de mirror e ajuste de relógio.
    """
    clean_key = api_key.strip() if api_key else ""
    clean_secret = api_secret.strip() if api_secret else ""
    cache_key = (clean_key, clean_secret, bool(testnet), int(timeout))

    if use_cache:
        with _EXCHANGE_CACHE_LOCK:
            cached = _EXCHANGE_CACHE.get(cache_key)
            if cached is not None:
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
        with _EXCHANGE_CACHE_LOCK:
            _EXCHANGE_CACHE[cache_key] = exchange

    return exchange


def check_binance_health(testnet: bool = False, ttl_seconds: float = 5.0) -> Dict[str, Any]:
    """
    Executa um diagnóstico leve da conexão pública com a Binance:
    mede latência real (`fetch_time`), desvio de relógio (clock drift em ms),
    mirror ativo e métricas acumuladas.
    """
    now = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get(bool(testnet))
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    exchange = get_binance_exchange(testnet=testnet, timeout=6000)
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
        local_time_ms = int(time.time() * 1000)
        clock_drift_ms = server_time_ms - local_time_ms
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

    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE[bool(testnet)] = (now, result)
    return result


def fetch_live_ticker(
    symbol: str = "BTC/USDT",
    testnet: bool = False,
    ttl_seconds: float = 2.0,
) -> Dict[str, Any]:
    """
    Retorna cotação ao vivo, spread e estatísticas de 24h com cache curto (2s) e
    failover automático para alimentar o dashboard sem sobrecarregar a API da Binance.
    """
    cache_key = (symbol, bool(testnet))
    now = time.monotonic()
    with _TICKER_CACHE_LOCK:
        cached = _TICKER_CACHE.get(cache_key)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    t0 = time.perf_counter()
    exchange = get_binance_exchange(testnet=testnet, timeout=8000)
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
    with _TICKER_CACHE_LOCK:
        _TICKER_CACHE[cache_key] = (now, result)
    return result


def fetch_order_book_summary(
    symbol: str = "BTC/USDT",
    limit: int = 15,
    testnet: bool = False,
    ttl_seconds: float = 2.0,
) -> Dict[str, Any]:
    """
    Retorna resumo do livro de ofertas (top bids/asks, spread e pressão compradora vs vendedora)
    com cache curto de 2s.
    """
    safe_limit = max(5, min(int(limit), 50))
    cache_key = (symbol, safe_limit, bool(testnet))
    now = time.monotonic()
    with _ORDERBOOK_CACHE_LOCK:
        cached = _ORDERBOOK_CACHE.get(cache_key)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    t0 = time.perf_counter()
    exchange = get_binance_exchange(testnet=testnet, timeout=8000)
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
    with _ORDERBOOK_CACHE_LOCK:
        _ORDERBOOK_CACHE[cache_key] = (now, result)
    return result


def fetch_live_candles(
    symbol: str = "BTC/USDT",
    timeframe: str = "1m",
    limit: int = 150,
    testnet: bool = False,
    ttl_seconds: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Busca candles OHLCV diretamente da Binance no formato TradingView (time em segundos)
    com cache curto (5s), permitindo acompanhar qualquer par/timeframe mesmo com o robô offline.
    """
    safe_limit = max(1, min(int(limit), 1000))
    cache_key = (symbol, timeframe, safe_limit, bool(testnet))
    now = time.monotonic()
    with _CANDLES_CACHE_LOCK:
        cached = _CANDLES_CACHE.get(cache_key)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    exchange = get_binance_exchange(testnet=testnet, timeout=8000)
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
    with _CANDLES_CACHE_LOCK:
        _CANDLES_CACHE[cache_key] = (now, candles)
    return candles


def fetch_cached_balance(
    testnet: Optional[bool] = None,
    ttl_seconds: float = 20.0,
) -> Dict[str, Any]:
    """
    Retorna os saldos da carteira Binance Spot com cache curto (20s) para sincronização
    automática no card KPI do dashboard sem sobrecarregar o rate limit da conta.
    """
    cfg = load_binance_config()
    if not cfg["has_keys"]:
        return {
            "connected": False,
            "success": False,
            "has_keys": False,
            "message": "Nenhuma chave Binance configurada.",
        }

    use_testnet = cfg["testnet"] if testnet is None else bool(testnet)
    cache_key = (cfg["api_key_masked"], use_testnet)
    now = time.monotonic()
    with _BALANCE_CACHE_LOCK:
        cached = _BALANCE_CACHE.get(cache_key)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]

    res = test_binance_connection(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=use_testnet,
    )
    res["connected"] = bool(res.get("success"))
    res["has_keys"] = True
    res["trading_mode"] = cfg["trading_mode"]
    with _BALANCE_CACHE_LOCK:
        _BALANCE_CACHE[cache_key] = (now, res)
    return res


def test_binance_connection(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    testnet: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Testa a conexão autenticada com a Binance, recupera os saldos da carteira Spot
    e valida as permissões de trading.
    """
    cfg = load_binance_config()
    api_key = api_key or cfg["api_key"]
    api_secret = api_secret or cfg["api_secret"]
    if testnet is None:
        testnet = cfg["testnet"]

    if not api_key or not api_secret:
        return {
            "success": False,
            "error_code": "NO_KEYS",
            "message": "Chave de API ou Chave Secreta não configuradas no sistema.",
            "instructions": "Preencha a API Key e API Secret geradas no painel da Binance."
        }

    try:
        t0 = time.perf_counter()
        exchange = get_binance_exchange(api_key=api_key, api_secret=api_secret, testnet=testnet)
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
                    "total": float(data)
                })

        priority = {"USDT": 0, "BTC": 1, "BNB": 2}
        assets.sort(key=lambda item: (priority.get(item["asset"], 3), -item["total"]))

        usdt_free = float(raw_balance.get("free", {}).get("USDT", 0.0) or 0.0)
        usdt_total = float(raw_balance.get("total", {}).get("USDT", 0.0) or 0.0)
        btc_total = float(raw_balance.get("total", {}).get("BTC", 0.0) or 0.0)

        account_info = raw_balance.get("info", {})
        can_trade = account_info.get("canTrade", True)
        can_withdraw = account_info.get("canWithdraw", False)

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
            "active_mirror": "testnet.binance.vision" if testnet else _get_active_mirror(),
            "message": f"Conexão com a {env_name} estabelecida com sucesso ({latency_ms:.0f} ms)! Carteira sincronizada."
        }

    except ccxt.AuthenticationError as e:
        return {"success": False, "error_code": "AUTH_ERROR",
                "message": "Erro de Autenticação Binance: API Key ou Secret inválida ou expirada.",
                "detail": str(e)}
    except ccxt.ExchangeNotAvailable as e:
        return {"success": False, "error_code": "EXCHANGE_UNAVAILABLE",
                "message": "Servidores da Binance temporariamente inacessíveis ou sob manutenção.",
                "detail": str(e)}
    except ccxt.NetworkError as e:
        return {"success": False, "error_code": "NETWORK_ERROR",
                "message": "Falha de rede ao conectar com a Binance. Verifique sua conexão com a internet.",
                "detail": str(e)}
    except Exception as e:
        error_msg = str(e)
        if "-2015" in error_msg:
            msg = "Chave de API inválida, IP não autorizado ou permissões insuficientes na Binance."
        elif "-1021" in error_msg:
            msg = "Dessincronização de horário com o servidor da Binance. Ajuste a hora do seu Windows."
        else:
            msg = f"Falha ao validar credenciais da Binance: {error_msg}"
        return {"success": False, "error_code": "GENERAL_ERROR", "message": msg, "detail": error_msg}


# -----------------------------------------------------------------------------
# Saldos, Idempotência e Utilidades de Execução de Ordens
# -----------------------------------------------------------------------------
def _base_asset(symbol: str) -> str:
    return symbol.split("/")[0]


def _quote_asset(symbol: str) -> str:
    return symbol.split("/")[1]


def _is_testnet_exchange(exchange: Any) -> bool:
    try:
        urls = getattr(exchange, "urls", {}) or {}
        return "testnet" in str(urls.get("api", "")).lower()
    except Exception:
        return False


def generate_client_order_id(prefix: str = "fynex") -> str:
    """Gera um `newClientOrderId` único (<= 36 chars, padrão Binance) para idempotência."""
    clean_prefix = re.sub(r"[^A-Za-z0-9_-]", "", prefix)[:12] or "fynex"
    ts_part = hex(int(time.time() * 1000))[2:]
    rand_part = uuid.uuid4().hex[:8]
    return f"{clean_prefix}_{ts_part}_{rand_part}"[:36]


def get_free_balance(exchange: ccxt.binance, asset: str) -> float:
    balance = call_with_retry(
        lambda: exchange.fetch_balance(),
        exchange=exchange,
        testnet=_is_testnet_exchange(exchange),
        max_attempts=2,
    )
    return float(balance.get("free", {}).get(asset, 0.0) or 0.0)


def get_asset_balances(exchange: ccxt.binance, asset: str) -> Tuple[float, float, float]:
    """Retorna (free, locked, total) de um ativo na carteira Spot com retry."""
    balance = call_with_retry(
        lambda: exchange.fetch_balance(),
        exchange=exchange,
        testnet=_is_testnet_exchange(exchange),
        max_attempts=2,
    )
    free = float(balance.get("free", {}).get(asset, 0.0) or 0.0)
    locked = float(balance.get("used", {}).get(asset, 0.0) or 0.0)
    total = float(balance.get("total", {}).get(asset, free + locked) or 0.0)
    return free, locked, total


def base_fee_from_order(order: Dict[str, Any], base: str) -> float:
    """Soma as taxas cobradas no ativo base (ex.: BTC) em uma ordem executada."""
    fees = order.get("fees") or []
    if not fees and order.get("fee"):
        fees = [order["fee"]]
    total = 0.0
    for fee in fees:
        if fee and fee.get("currency") == base and fee.get("cost") is not None:
            total += float(fee["cost"])
    return total


def quote_fee_from_order(order: Dict[str, Any], quote: str) -> float:
    """Soma as taxas cobradas no ativo de cotação (ex.: USDT) em uma ordem executada."""
    fees = order.get("fees") or []
    if not fees and order.get("fee"):
        fees = [order["fee"]]
    total = 0.0
    for fee in fees:
        if fee and fee.get("currency") == quote and fee.get("cost") is not None:
            total += float(fee["cost"])
    return total


def net_filled_quantity(order: Dict[str, Any], base: str, fallback_qty: float) -> float:
    """Quantidade realmente recebida = executado bruto - taxas cobradas no ativo base."""
    filled = float(order.get("filled") or fallback_qty)
    return max(0.0, filled - base_fee_from_order(order, base))


def reconcile_order_by_client_id(
    exchange: ccxt.binance,
    symbol: str,
    client_order_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Consulta uma ordem na Binance pelo `origClientOrderId` após um timeout/erro de rede,
    evitando ordens duplicadas caso a requisição original tenha chegado à exchange.
    """
    try:
        markets = exchange.load_markets()
        market = markets.get(symbol)
        if not market:
            return None
        raw = call_with_retry(
            lambda: exchange.privateGetOrder({"symbol": market["id"], "origClientOrderId": client_order_id}),
            exchange=exchange,
            testnet=_is_testnet_exchange(exchange),
            max_attempts=2,
        )
        if not raw:
            return None
        executed_qty = float(raw.get("executedQty") or 0.0)
        cum_quote = float(raw.get("cummulativeQuoteQty") or 0.0)
        avg_price = (cum_quote / executed_qty) if executed_qty > 0 else float(raw.get("price") or 0.0)
        return {
            "id": str(raw.get("orderId")),
            "clientOrderId": raw.get("clientOrderId", client_order_id),
            "status": str(raw.get("status", "")).lower(),
            "filled": executed_qty,
            "cost": cum_quote,
            "average": avg_price,
            "fees": [],
            "info": raw,
        }
    except Exception:
        return None


def preflight_execution_check(
    exchange: ccxt.binance,
    symbol: str,
    max_spread_pct: float = 0.50,
) -> Dict[str, Any]:
    """
    Valida condições de mercado antes de enviar ordem real:
    - Par suportado e ativo na exchange
    - Cotação válida (last/bid/ask)
    - Spread dentro do limite de proteção contra liquidez anormal
    """
    testnet = _is_testnet_exchange(exchange)
    markets = call_with_retry(
        lambda: exchange.load_markets(),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    if symbol not in markets:
        return {"ok": False, "message": f"Par {symbol} não suportado pela exchange."}

    market = markets[symbol]
    if market.get("active") is False:
        return {"ok": False, "message": f"Par {symbol} está temporariamente suspenso/inativo na Binance."}

    ticker = call_with_retry(
        lambda: exchange.fetch_ticker(symbol),
        exchange=exchange,
        testnet=testnet,
        max_attempts=2,
    )
    price = float(ticker.get("last") or ticker.get("close") or 0.0)
    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    if price <= 0:
        return {"ok": False, "message": "Preço atual de mercado inválido."}

    spread_pct = 0.0
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 0.0
        if max_spread_pct > 0 and spread_pct > max_spread_pct:
            return {
                "ok": False,
                "message": (
                    f"Spread anormal detectado em {symbol} ({spread_pct:.3f}% > limite {max_spread_pct:.2f}%). "
                    f"Ordem bloqueada por proteção contra slippage."
                ),
                "spread_pct": round(spread_pct, 4),
            }

    return {
        "ok": True,
        "market": market,
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_pct": round(spread_pct, 4),
    }


def check_execution_readiness(symbol: str = "BTC/USDT") -> Dict[str, Any]:
    """
    Retorna diagnóstico completo de prontidão da conexão Binance para entradas e saídas:
    chaves, permissões Spot, segurança (saque bloqueado), relógio, latência e spread.
    """
    cfg = load_binance_config()
    health = check_binance_health(testnet=cfg["testnet"])
    checks: Dict[str, Any] = {
        "has_keys": cfg["has_keys"],
        "trading_mode": cfg["trading_mode"],
        "testnet": cfg["testnet"],
        "use_exchange_oco": cfg["use_exchange_oco"],
        "api_reachable": bool(health.get("ok")),
        "clock_synced": bool(health.get("clock_synced")),
        "latency_ms": health.get("latency_ms"),
        "clock_drift_ms": health.get("clock_drift_ms"),
        "active_mirror": health.get("active_mirror"),
        "can_trade": False,
        "withdraw_disabled": True,
        "usdt_free": 0.0,
        "ready_for_orders": False,
        "warnings": [],
    }

    if not cfg["has_keys"]:
        checks["warnings"].append("Nenhuma API Key configurada (operando apenas em simulação).")
        return checks

    conn = fetch_cached_balance(testnet=cfg["testnet"], ttl_seconds=15.0)
    if not conn.get("success"):
        checks["warnings"].append(conn.get("message") or "Falha ao autenticar na Binance.")
        return checks

    checks["can_trade"] = bool(conn.get("can_trade", False))
    checks["withdraw_disabled"] = not bool(conn.get("can_withdraw", False))
    checks["usdt_free"] = float(conn.get("usdt_free", 0.0))

    if not checks["can_trade"]:
        checks["warnings"].append("Permissão de Trading Spot desabilitada na chave da Binance.")
    if not checks["withdraw_disabled"]:
        checks["warnings"].append("ALERTA: Permissão de saque (Withdraw) está habilitada na API Key!")
    if not checks["clock_synced"]:
        checks["warnings"].append(f"Relógio dessincronizado ({checks['clock_drift_ms']} ms).")
    if checks["usdt_free"] < cfg["trade_amount_usdt"]:
        checks["warnings"].append(
            f"Saldo USDT livre (${checks['usdt_free']:.2f}) abaixo do valor por ordem (${cfg['trade_amount_usdt']:.2f})."
        )

    checks["ready_for_orders"] = (
        checks["api_reachable"]
        and checks["clock_synced"]
        and checks["can_trade"]
        and checks["usdt_free"] >= min(10.0, cfg["trade_amount_usdt"])
    )
    return checks


# -----------------------------------------------------------------------------
# Execução de ordens a mercado (com Idempotência, Preflight e Reconciliação)
# -----------------------------------------------------------------------------
def execute_spot_buy(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    amount_usdt: float = 50.0,
    max_spread_pct: float = 0.50,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executa uma ordem de COMPRA a Mercado na Binance Spot com:
    - Preflight (mercado ativo + proteção contra spread anormal)
    - Verificação de saldo USDT livre, precisão de lote e notional mínimo
    - Idempotência via `newClientOrderId` + reconciliação automática em timeout de rede
    - Cálculo de quantidade LÍQUIDA, slippage real e latência de execução
    """
    cid = client_order_id or generate_client_order_id("fynexb")
    t_start = time.perf_counter()
    try:
        pre = preflight_execution_check(exchange, symbol, max_spread_pct=max_spread_pct)
        if not pre.get("ok"):
            return {"success": False, "message": pre.get("message", "Falha no preflight."), "client_order_id": cid}

        market = pre["market"]
        current_price = pre["price"]
        ref_ask = pre["ask"] if pre["ask"] > 0 else current_price
        spread_pct = pre["spread_pct"]
        quote = _quote_asset(symbol)
        base = _base_asset(symbol)

        free_quote = get_free_balance(exchange, quote)
        if free_quote < amount_usdt:
            return {
                "success": False,
                "client_order_id": cid,
                "message": f"Saldo {quote} insuficiente: livre {free_quote:.2f}, necessário {amount_usdt:.2f}.",
            }

        quantity = float(exchange.amount_to_precision(symbol, amount_usdt / ref_ask))
        min_cost = market.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0
        calculated_cost = quantity * ref_ask
        if calculated_cost < min_cost:
            return {
                "success": False,
                "client_order_id": cid,
                "message": f"Valor da ordem (${calculated_cost:.2f}) abaixo do mínimo da Binance (${min_cost:.2f}).",
            }

        logger.info(
            f"🛒 Executando Ordem Spot de COMPRA [{cid}]: {quantity} {symbol} "
            f"a ~${ref_ask:,.2f} (spread {spread_pct:.3f}%)"
        )

        order: Optional[Dict[str, Any]] = None
        try:
            order = exchange.create_market_buy_order(
                symbol, quantity, params={"newClientOrderId": cid}
            )
        except (ccxt.NetworkError, ccxt.RequestTimeout) as net_err:
            logger.warning(f"⚠️ Timeout/rede na compra [{cid}]: {net_err}. Reconciliando na Binance...")
            order = reconcile_order_by_client_id(exchange, symbol, cid)
            if order is None or float(order.get("filled") or 0.0) <= 0:
                return {
                    "success": False,
                    "client_order_id": cid,
                    "reconciled": False,
                    "message": f"Falha de rede na compra e ordem não encontrada na Binance: {net_err}",
                }
            logger.info(f"✅ Compra [{cid}] confirmada via reconciliação na Binance (order_id={order.get('id')}).")

        exec_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        gross_qty = float(order.get("filled") or quantity)
        net_qty = net_filled_quantity(order, base, quantity)
        cost = float(order.get("cost") or calculated_cost)
        avg_price = float(order.get("average") or (cost / gross_qty if gross_qty > 0 else ref_ask))
        slippage_pct = round(((avg_price - ref_ask) / ref_ask * 100.0), 4) if ref_ask > 0 else 0.0

        logger.info(
            f"✅ COMPRA confirmada [{cid}] | Preço médio: ${avg_price:,.2f} | "
            f"Slippage: {slippage_pct:+.3f}% | Latência: {exec_ms:.0f} ms"
        )

        # Invalida cache de saldo após ordem executada
        with _BALANCE_CACHE_LOCK:
            _BALANCE_CACHE.clear()

        return {
            "success": True,
            "order_id": str(order.get("id")),
            "client_order_id": cid,
            "symbol": symbol,
            "side": "BUY",
            "gross_quantity": gross_qty,
            "quantity": net_qty,
            "price": avg_price,
            "cost_usdt": cost,
            "slippage_pct": slippage_pct,
            "spread_pct": spread_pct,
            "execution_latency_ms": exec_ms,
            "raw_order": order,
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de compra na Binance [{cid}]: {e}")
        return {"success": False, "client_order_id": cid, "message": str(e)}


def execute_spot_sell(
    exchange: ccxt.binance,
    symbol: str = "BTC/USDT",
    quantity: float = 0.0,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executa uma ordem de VENDA a Mercado na Binance Spot com:
    - Verificação de saldo livre e bloqueado (evita erro -2010)
    - Idempotência via `newClientOrderId` + reconciliação em timeout de rede
    - Cálculo de slippage real e latência de execução
    """
    cid = client_order_id or generate_client_order_id("fynexs")
    t_start = time.perf_counter()
    try:
        testnet = _is_testnet_exchange(exchange)
        markets = call_with_retry(
            lambda: exchange.load_markets(),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
        )
        if symbol not in markets:
            return {"success": False, "client_order_id": cid, "message": f"Par {symbol} não suportado pela exchange."}

        market = markets[symbol]
        base = _base_asset(symbol)

        free_base, locked_base, _ = get_asset_balances(exchange, base)
        sell_qty = float(exchange.amount_to_precision(symbol, min(quantity, free_base)))

        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0.00001) or 0.00001
        if sell_qty < min_amount:
            locked_note = f" (travado em ordens abertas: {locked_base})" if locked_base > 0 else ""
            return {
                "success": False,
                "client_order_id": cid,
                "message": (
                    f"Quantidade vendável ({sell_qty}) inferior ao lote mínimo ({min_amount}). "
                    f"Saldo livre de {base}: {free_base}{locked_note}."
                ),
            }

        ticker = call_with_retry(
            lambda: exchange.fetch_ticker(symbol),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
        )
        current_price = float(ticker.get("last") or ticker.get("close") or 0.0)
        ref_bid = float(ticker.get("bid") or current_price)

        logger.info(f"💰 Executando Ordem Spot de VENDA [{cid}]: {sell_qty} {symbol} a ~${ref_bid:,.2f}")

        order: Optional[Dict[str, Any]] = None
        try:
            order = exchange.create_market_sell_order(
                symbol, sell_qty, params={"newClientOrderId": cid}
            )
        except (ccxt.NetworkError, ccxt.RequestTimeout) as net_err:
            logger.warning(f"⚠️ Timeout/rede na venda [{cid}]: {net_err}. Reconciliando na Binance...")
            order = reconcile_order_by_client_id(exchange, symbol, cid)
            if order is None or float(order.get("filled") or 0.0) <= 0:
                return {
                    "success": False,
                    "client_order_id": cid,
                    "reconciled": False,
                    "message": f"Falha de rede na venda e ordem não encontrada na Binance: {net_err}",
                }
            logger.info(f"✅ Venda [{cid}] confirmada via reconciliação na Binance (order_id={order.get('id')}).")

        exec_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        filled_qty = float(order.get("filled") or sell_qty)
        cost = float(order.get("cost") or filled_qty * ref_bid)
        avg_price = float(order.get("average") or (cost / filled_qty if filled_qty > 0 else ref_bid))
        slippage_pct = round(((ref_bid - avg_price) / ref_bid * 100.0), 4) if ref_bid > 0 else 0.0

        logger.info(
            f"✅ VENDA confirmada [{cid}] | Preço médio: ${avg_price:,.2f} | "
            f"Slippage: {slippage_pct:+.3f}% | Latência: {exec_ms:.0f} ms"
        )

        with _BALANCE_CACHE_LOCK:
            _BALANCE_CACHE.clear()

        return {
            "success": True,
            "order_id": str(order.get("id")),
            "client_order_id": cid,
            "symbol": symbol,
            "side": "SELL",
            "quantity": filled_qty,
            "price": avg_price,
            "proceeds_usdt": cost,
            "slippage_pct": slippage_pct,
            "execution_latency_ms": exec_ms,
            "raw_order": order,
        }
    except Exception as e:
        logger.error(f"Erro ao executar ordem de venda na Binance [{cid}]: {e}")
        return {"success": False, "client_order_id": cid, "message": str(e)}


def execute_spot_sell_with_retry(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    attempts: int = 3,
    base_delay_seconds: float = 2.0
) -> Dict[str, Any]:
    """Tenta vender até `attempts` vezes com backoff exponencial e novo clientOrderId por tentativa."""
    last: Dict[str, Any] = {"success": False, "message": "Nenhuma tentativa executada."}
    for attempt in range(1, attempts + 1):
        last = execute_spot_sell(exchange, symbol, quantity)
        if last.get("success"):
            return last
        logger.warning(f"Tentativa de venda {attempt}/{attempts} falhou: {last.get('message')}")
        if attempt < attempts:
            time.sleep(base_delay_seconds * (2 ** (attempt - 1)))
    return last


# -----------------------------------------------------------------------------
# Proteção OCO (Take Profit + Stop-Limit) do lado da exchange
# -----------------------------------------------------------------------------
def place_protective_oco(
    exchange: ccxt.binance,
    symbol: str,
    quantity: float,
    take_profit_price: float,
    stop_price: float,
    stop_limit_offset_pct: float = 0.003
) -> Dict[str, Any]:
    """
    Envia uma OCO de VENDA com validação prévia de preços e retry automático:
    LIMIT_MAKER no take profit (acima) e STOP_LOSS_LIMIT no stop (abaixo).
    """
    try:
        testnet = _is_testnet_exchange(exchange)
        call_with_retry(lambda: exchange.load_markets(), exchange=exchange, testnet=testnet, max_attempts=2)
        market = exchange.market(symbol)
        base = _base_asset(symbol)
        free_base = get_free_balance(exchange, base)
        qty = exchange.amount_to_precision(symbol, min(quantity, free_base))

        # Valida coerência em relação ao preço atual (evita erro -2010 "Order would immediately trigger")
        try:
            ticker = call_with_retry(
                lambda: exchange.fetch_ticker(symbol), exchange=exchange, testnet=testnet, max_attempts=2
            )
            cur = float(ticker.get("last") or 0.0)
            if cur > 0:
                if take_profit_price <= cur:
                    take_profit_price = cur * 1.002
                if stop_price >= cur:
                    stop_price = cur * 0.998
        except Exception:
            pass

        stop_limit_price = stop_price * (1.0 - stop_limit_offset_pct)

        params = {
            "symbol": market["id"],
            "side": "SELL",
            "quantity": qty,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": exchange.price_to_precision(symbol, take_profit_price),
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": exchange.price_to_precision(symbol, stop_price),
            "belowPrice": exchange.price_to_precision(symbol, stop_limit_price),
            "belowTimeInForce": "GTC",
        }
        response = call_with_retry(
            lambda: exchange.privatePostOrderListOco(params),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
            allow_mirror_failover=False,
        )
        list_id = str(response.get("orderListId"))
        logger.info(
            f"🛡️ OCO de proteção enviada (orderListId={list_id}) | TP {params['abovePrice']} | "
            f"Stop {params['belowStopPrice']} / Limite {params['belowPrice']} | Qtd {qty}"
        )
        return {"success": True, "order_list_id": list_id, "raw": response}
    except Exception as e:
        logger.error(f"Falha ao enviar OCO de proteção: {e}")
        return {"success": False, "message": str(e)}


def get_oco_status(exchange: ccxt.binance, symbol: str, order_list_id: str) -> Dict[str, Any]:
    """
    Consulta a OCO com retry automático. Se ela foi executada, retorna o preço médio,
    quantidade e o tipo de saída (TAKE_PROFIT ou STOP_LOSS).
    """
    try:
        testnet = _is_testnet_exchange(exchange)
        response = call_with_retry(
            lambda: exchange.privateGetOrderList({"orderListId": order_list_id}),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
        )
        list_status = response.get("listOrderStatus")  # EXECUTING | ALL_DONE | REJECT
        result: Dict[str, Any] = {"success": True, "list_status": list_status, "filled": False}
        if list_status != "ALL_DONE":
            return result

        for leg in response.get("orders", []):
            order = call_with_retry(
                lambda lid=str(leg["orderId"]): exchange.fetch_order(lid, symbol),
                exchange=exchange,
                testnet=testnet,
                max_attempts=2,
            )
            if float(order.get("filled") or 0.0) > 0:
                info_type = str(order.get("info", {}).get("type", "")).upper()
                result.update({
                    "filled": True,
                    "order_id": str(order.get("id")),
                    "price": float(order.get("average") or order.get("price") or 0.0),
                    "quantity": float(order.get("filled") or 0.0),
                    "reason": "TAKE_PROFIT" if info_type == "LIMIT_MAKER" else "STOP_LOSS",
                })
                break
        return result
    except Exception as e:
        return {"success": False, "message": str(e)}


def cancel_oco(exchange: ccxt.binance, symbol: str, order_list_id: str) -> Dict[str, Any]:
    try:
        testnet = _is_testnet_exchange(exchange)
        market = exchange.market(symbol)
        call_with_retry(
            lambda: exchange.privateDeleteOrderList({"symbol": market["id"], "orderListId": order_list_id}),
            exchange=exchange,
            testnet=testnet,
            max_attempts=2,
            allow_mirror_failover=False,
        )
        logger.info(f"OCO {order_list_id} cancelada.")
        return {"success": True}
    except Exception as e:
        logger.warning(f"Falha ao cancelar OCO {order_list_id}: {e}")
        return {"success": False, "message": str(e)}
