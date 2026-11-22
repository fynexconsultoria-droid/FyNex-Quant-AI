"""
Servidor Backend do Dashboard Web Quant Trading (FastAPI)
=========================================================
Fornece APIs em tempo real para alimentar a interface gráfica interativa:
- Proteção contra CSRF e DNS Rebinding (validação de Host + X-CSRF-Token em todo POST)
- Cotações e Candles históricos no formato TradingView (OHLCV), filtrados por par/timeframe
- Previsões da IA com taxa de acerto em tempo real
- Status do Robô lido do estado vivo no SQLite (bot_state) e PID file seguro (bot.pid)
- Controle de Inicialização e Parada limpa do Robô via interface web
- Histórico de Operações e Curva de Capital (Paper Trading separado do Live/Testnet)
"""

import base64
import os
import sys
import time
import secrets
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import psutil
from pydantic import BaseModel
from fastapi import FastAPI, Query, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

# Caminhos base e import dos módulos do projeto
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import binance_client  # noqa: E402
import train_ai  # noqa: E402
from core import forex_ai_trader, forex_market, macro_intelligence, news_feed  # noqa: E402
from core.database import TradingDatabase  # noqa: E402
from strategy_config import get_strategy_profile, DEFAULT_TIMEFRAME  # noqa: E402
from trading_bot import (  # noqa: E402
    PID_FILE_PATH,
    STOP_SIGNAL_PATH,
    get_running_bot_pid,
    is_pid_alive_bot,
    request_bot_stop,
    ModelPersistence,
    DynamicCalibrator,
    timeframe_to_seconds,
)

DB_PATH = os.path.join(BASE_DIR, "trading_data.db")
MODELS_DIR = os.path.join(BASE_DIR, "models")
META_PATH = os.path.join(MODELS_DIR, "metadata.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "trading_bot.log")
LEGACY_LOG_PATH = os.path.join(BASE_DIR, "trading_bot.log")
BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")
SUPERVISOR_SCRIPT = os.path.join(BASE_DIR, "supervisor.py")

# Token CSRF gerado a cada inicialização do servidor e injetado no index.html
CSRF_TOKEN = secrets.token_urlsafe(32)
ALLOWED_HOSTS = {
    "127.0.0.1",
    "127.0.0.1:8000",
    "localhost",
    "localhost:8000",
    "testserver",  # usado pelo TestClient do FastAPI/Starlette
}
ALLOWED_ORIGIN_TUPLES = {
    ("http", "127.0.0.1", 8000),
    ("http", "127.0.0.1", None),
    ("http", "localhost", 8000),
    ("http", "localhost", None),
    ("http", "testserver", None),
}


def _register_cloud_hosts() -> None:
    """Registra domínios públicos configurados no Railway/Render ou via FYNEX_PUBLIC_HOSTS."""
    raw_hosts = [
        os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""),
        os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""),
        *os.environ.get("FYNEX_PUBLIC_HOSTS", "").split(","),
    ]
    for item in raw_hosts:
        h = item.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        if not h:
            continue
        ALLOWED_HOSTS.add(h)
        host_only = h.split(":")[0]
        ALLOWED_HOSTS.add(host_only)
        ALLOWED_ORIGIN_TUPLES.add(("https", host_only, None))
        ALLOWED_ORIGIN_TUPLES.add(("https", host_only, 443))
        ALLOWED_ORIGIN_TUPLES.add(("http", host_only, None))


_register_cloud_hosts()

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' wss://stream.binance.com:9443 wss://stream.binance.com:443 wss://stream.binance.com; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)

app = FastAPI(title="FyNex Quant AI Dashboard")


def _check_basic_auth(request: Request) -> bool:
    """Se FYNEX_DASHBOARD_PASSWORD estiver definida (deploy na nuvem), exige HTTP Basic Auth."""
    expected_pw = os.environ.get("FYNEX_DASHBOARD_PASSWORD", "").strip()
    if not expected_pw:
        return True
    expected_user = os.environ.get("FYNEX_DASHBOARD_USER", "admin").strip() or "admin"
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
        user, _, pw = decoded.partition(":")
        return secrets.compare_digest(user, expected_user) and secrets.compare_digest(pw, expected_pw)
    except Exception:
        return False


def is_allowed_origin(origin: str) -> bool:
    """Valida exatamente scheme, host e porta do header Origin (evita bypass localhost.evil.com)."""
    try:
        parsed = urlparse(origin.strip())
    except Exception:
        return False
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port) in ALLOWED_ORIGIN_TUPLES


# -----------------------------------------------------------------------------
# Segurança: Validação de Host (contra DNS Rebinding), Origin exata, Auth Nuvem, CSP e CSRF
# -----------------------------------------------------------------------------
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Healthcheck público para Railway / Render / Docker
    if request.url.path == "/healthz":
        return JSONResponse({"status": "ok"})

    host = (request.headers.get("host") or "").lower().strip()
    host_no_port = host.split(":")[0]
    if not host or (host not in ALLOWED_HOSTS and host_no_port not in ALLOWED_HOSTS):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Host '{host}' não autorizado (proteção contra DNS Rebinding)."},
        )

    origin = (request.headers.get("origin") or "").strip()
    if origin and not is_allowed_origin(origin):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Origem '{origin}' bloqueada (mesma origem exigida)."},
        )

    if not _check_basic_auth(request):
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="FyNex Quant AI"'},
            content={"detail": "Autenticação obrigatória para acessar o dashboard."},
        )

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP_HEADER
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def require_csrf(request: Request) -> None:
    token = request.headers.get("X-CSRF-Token") or ""
    if not secrets.compare_digest(token, CSRF_TOKEN):
        raise HTTPException(
            status_code=403,
            detail="Token CSRF ausente ou inválido. Recarregue a página do dashboard.",
        )


# -----------------------------------------------------------------------------
# Utilitários de Banco e Processo
# -----------------------------------------------------------------------------
def get_db_connection():
    """Retorna uma conexão SQLite configurada para leitura rápida com WAL."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn


@contextmanager
def db_connection():
    """Context manager que garante o fechamento seguro da conexão SQLite."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _fetch_bot_state(conn: Optional[sqlite3.Connection]) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_state'"
        ).fetchone()
        if exists:
            row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
            return dict(row) if row else None
    except Exception:
        pass
    return None


def _fetch_open_position(
    conn: Optional[sqlite3.Connection],
    symbol: str,
    timeframe: str,
) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='open_positions'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            """
            SELECT symbol, timeframe, mode, model_version, entry_time, entry_price,
                   quantity, order_id, sl_price, tp_price, oco_list_id, status
            FROM open_positions
            WHERE symbol = ? AND timeframe = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _query_status_metrics(
    conn: Optional[sqlite3.Connection],
    symbol: str,
    timeframe: str,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Dict[str, Any], float]:
    latest_candle = None
    rolling_acc: Dict[str, Any] = {"total": 0, "correct": 0, "accuracy_pct": 50.0}
    trade_stats: Dict[str, Any] = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl_pct": 0.0,
        "total_pnl_usd": 0.0,
    }
    paper_pnl_usd = 0.0

    if conn is None:
        return latest_candle, rolling_acc, trade_stats, paper_pnl_usd

    cursor = conn.cursor()

    c_row = cursor.execute("""
        SELECT datetime_utc, close, open, high, low, volume, timestamp
        FROM ohlcv_candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (symbol, timeframe)).fetchone()
    if c_row:
        latest_candle = dict(c_row)

    p_rows = cursor.execute("""
        SELECT is_correct
        FROM predictions
        WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL
        ORDER BY id DESC
        LIMIT 30
    """, (symbol, timeframe)).fetchall()
    if p_rows:
        total_eval = len(p_rows)
        correct_eval = sum(1 for r in p_rows if r["is_correct"] == 1)
        rolling_acc = {
            "total": total_eval,
            "correct": correct_eval,
            "accuracy_pct": (correct_eval / total_eval * 100.0),
        }

    t_row = cursor.execute("""
        SELECT COUNT(*) as total_trades,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl_pct) as total_pnl_pct,
               SUM(pnl_usd) as total_pnl_usd
        FROM simulated_trades
        WHERE symbol = ? AND timeframe = ?
    """, (symbol, timeframe)).fetchone()
    if t_row and t_row["total_trades"]:
        tot = t_row["total_trades"]
        w = t_row["wins"] or 0
        l = t_row["losses"] or 0
        trade_stats = {
            "total_trades": tot,
            "wins": w,
            "losses": l,
            "win_rate": (w / tot * 100.0) if tot > 0 else 0.0,
            "total_pnl_pct": t_row["total_pnl_pct"] or 0.0,
            "total_pnl_usd": t_row["total_pnl_usd"] or 0.0,
        }

    p_row = cursor.execute("""
        SELECT COALESCE(SUM(pnl_usd), 0.0) AS paper_pnl
        FROM simulated_trades
        WHERE mode = 'paper'
    """).fetchone()
    paper_pnl_usd = float(p_row["paper_pnl"] or 0.0) if p_row else 0.0

    return latest_candle, rolling_acc, trade_stats, paper_pnl_usd


def _build_calibration_status(
    bot_state: Optional[Dict[str, Any]],
    symbol: str,
    timeframe: str,
    rolling_acc: Dict[str, Any],
) -> Dict[str, Any]:
    if bot_state and bot_state["symbol"] == symbol and bot_state["timeframe"] == timeframe:
        return {
            "mode": bot_state["calibration_mode"],
            "badge": bot_state["calibration_badge"],
            "threshold": float(bot_state["active_confidence"]) * 100.0,
            "base_threshold": float(bot_state["base_confidence"]) * 100.0,
            "description": bot_state["calibration_desc"],
        }
    profile = get_strategy_profile(timeframe)
    c_res = DynamicCalibrator.get_calibration(
        rolling_accuracy=rolling_acc["accuracy_pct"],
        evaluated_count=rolling_acc["total"],
        base_threshold=profile.confidence,
    )
    return {
        "mode": c_res["mode"],
        "badge": c_res["badge"],
        "threshold": float(c_res["threshold"]) * 100.0,
        "base_threshold": float(profile.confidence) * 100.0,
        "description": c_res["description"],
    }


def is_bot_running() -> Tuple[bool, Optional[int]]:
    """
    Verifica se o robô está ativo usando prioritariamente o arquivo `bot.pid`
    e confirmando que o PID pertence a um interpretador Python executando `trading_bot.py`
    (nunca confunde com editores como Notepad ou VS Code).
    """
    pid = get_running_bot_pid(PID_FILE_PATH)
    if pid:
        return True, pid

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if is_pid_alive_bot(proc.info["pid"]):
                return True, proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False, None


def _tail_lines(file_path: str, max_lines: int = 50, max_bytes: int = 65536) -> list[str]:
    """Lê apenas os últimos `max_bytes` do arquivo para retornar as últimas `max_lines` linhas."""
    with open(file_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes), os.SEEK_SET)
        chunk = f.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    return lines[-max_lines:]


# -----------------------------------------------------------------------------
# Endpoints da API REST
# -----------------------------------------------------------------------------
@app.get("/api/status")
def get_bot_status(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    timeframe: Optional[str] = Query(default=None, max_length=10),
):
    """Retorna o estado geral do sistema, métricas da IA e saldo simulado filtrados por par/timeframe."""
    bot_running, bot_pid = is_bot_running()

    with db_connection() as conn:
        bot_state = _fetch_bot_state(conn)
        active_symbol = symbol or (bot_state["symbol"] if bot_state else "BTC/USDT")
        active_tf = timeframe or (bot_state["timeframe"] if bot_state else "1m")
        latest_candle, rolling_acc, trade_stats, paper_pnl_usd = _query_status_metrics(
            conn, active_symbol, active_tf
        )
        open_position = _fetch_open_position(conn, active_symbol, active_tf)

    model_storage = ModelPersistence(models_dir=MODELS_DIR)
    meta = model_storage.load_metadata(active_symbol, active_tf)
    model_info = {
        "version": int(meta.get("version", 1)) if meta else (bot_state["model_version"] if bot_state else 1),
        "accuracy": (float(meta.get("accuracy", 0.5)) * 100.0) if meta else (
            float(bot_state["model_accuracy"]) * 100.0 if bot_state else 50.0
        ),
        "saved_at": meta.get("saved_at") if meta else None,
        "features": meta.get("feature_cols", []) if meta else [],
        "symbol": active_symbol,
        "timeframe": active_tf,
    }

    calib_status = _build_calibration_status(bot_state, active_symbol, active_tf, rolling_acc)

    bin_cfg = binance_client.load_binance_config()
    binance_summary = {
        "has_keys": bin_cfg["has_keys"],
        "api_key_masked": bin_cfg["api_key_masked"],
        "testnet": bin_cfg["testnet"],
        "trading_mode": bin_cfg["trading_mode"],
        "trade_amount_usdt": bin_cfg["trade_amount_usdt"],
    }

    return {
        "bot_online": bot_running,
        "bot_pid": bot_pid,
        "symbol": active_symbol,
        "timeframe": active_tf,
        "bot_active_timeframe": bot_state["timeframe"] if bot_state else active_tf,
        "latest_price": latest_candle["close"] if latest_candle else 0.0,
        "latest_update": latest_candle["datetime_utc"] if latest_candle else None,
        "model": model_info,
        "rolling_accuracy": rolling_acc,
        "calibration": calib_status,
        "trade_stats": trade_stats,
        "simulated_capital": 1000.0 + paper_pnl_usd,
        "open_position": open_position,
        "binance": binance_summary,
    }


@app.get("/api/market/live")
def get_live_market_ticker(symbol: str = Query(default="BTC/USDT", max_length=20)):
    """Retorna cotação ao vivo, spread, variação 24h e latência (Cripto Binance ou Forex XTB)."""
    try:
        if forex_market.is_forex_symbol(symbol):
            return forex_market.fetch_forex_quote(symbol=symbol)
        return binance_client.fetch_live_ticker(symbol=symbol, testnet=False)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar cotação ao vivo para {symbol}: {e}")


@app.get("/api/market/instruments")
def get_market_instruments(category: Optional[str] = Query(default=None, pattern="^(crypto|forex)$")):
    """Lista os instrumentos disponíveis (Cripto Spot + Forex / Moedas estilo XTB)."""
    return {"instruments": forex_market.list_instruments(category=category)}


@app.get("/api/market/watchlist")
def get_market_watchlist(category: str = Query(default="all", pattern="^(all|crypto|forex)$")):
    """Retorna cotações resumidas ao vivo da Watchlist multi-ativos (Cripto + Forex)."""
    return {"items": forex_market.fetch_watchlist(category=category)}


@app.get("/api/market/depth")
def get_live_market_depth(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    limit: int = Query(default=15, ge=5, le=50),
):
    """Retorna resumo do Order Book (melhor bid/ask, spread e pressão compradora vs vendedora)."""
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
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Falha ao consultar book Forex: {e}")
    try:
        return binance_client.fetch_order_book_summary(symbol=symbol, limit=limit, testnet=False)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar livro de ofertas na Binance: {e}")


@app.get("/api/news")
def get_live_news(
    category: str = Query(default="all", pattern="^(all|cripto|forex|macro)$"),
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=30, ge=1, le=100),
    refresh: bool = Query(default=False),
):
    """Retorna notícias ao vivo do mercado financeiro (Cripto, Forex e Macro)."""
    if refresh:
        news_feed.clear_news_cache()
    return news_feed.fetch_live_news(category=category, query=q, limit=limit)


class XTBOrderPayload(BaseModel):
    symbol: str
    side: str
    lots: float
    leverage: int = 30
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None


def _get_xtb_store() -> forex_market.XTBDeskStore:
    return forex_market.XTBDeskStore(db_path=DB_PATH)


@app.get("/api/xtb/account")
def get_xtb_account():
    """Retorna saldo, equity, margem, posições abertas com PnL ao vivo e histórico da mesa XTB."""
    store = _get_xtb_store()
    return store.get_account_snapshot()


@app.get("/api/forex/ai-status")
def get_forex_ai_status():
    """Retorna se o mercado Forex está aberto agora e status do scanner autônomo de pares de moedas."""
    is_open, msg = forex_ai_trader.is_forex_market_open()
    return {
        "market_open": is_open,
        "status_message": msg,
        "monitored_pairs": list(forex_ai_trader.FOREX_AI_PAIRS),
    }


@app.post("/api/xtb/order", dependencies=[Depends(require_csrf)])
def open_xtb_order(payload: XTBOrderPayload):
    """Abre ordem manual BUY ou SELL (Forex ou Cripto) na mesa estilo XTB."""
    store = _get_xtb_store()
    try:
        pos = store.open_position(
            symbol=payload.symbol,
            side=payload.side,
            lots=payload.lots,
            leverage=payload.leverage,
            sl_price=payload.sl_price,
            tp_price=payload.tp_price,
        )
        return {
            "status": "opened",
            "position": pos,
            "message": f"Ordem {pos['side']} {pos['lots']} lote(s) em {pos['symbol']} aberta a {pos['entry_price']}.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao executar ordem: {e}")


@app.post("/api/xtb/close/{position_id}", dependencies=[Depends(require_csrf)])
def close_xtb_order(position_id: int):
    """Fecha uma posição aberta da mesa XTB ao preço de mercado atual."""
    store = _get_xtb_store()
    try:
        closed = store.close_position(position_id=position_id, reason="MANUAL")
        return {
            "status": "closed",
            "trade": closed,
            "message": f"Posição #{position_id} ({closed['symbol']}) fechada com PnL ${closed['pnl_usd']:+.2f}.",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao fechar posição: {e}")


@app.get("/api/binance/health")
def get_binance_health(testnet: bool = Query(default=False)):
    """Diagnóstico da conexão com a Binance: latência, sincronismo de relógio, mirror e métricas."""
    return binance_client.check_binance_health(testnet=testnet)


@app.get("/api/binance/balance")
def get_binance_balance():
    """Retorna o saldo da carteira Spot em cache (20s) para sincronização automática no dashboard."""
    return binance_client.fetch_cached_balance()


@app.get("/api/binance/execution-ready")
def get_binance_execution_ready(symbol: str = Query(default="BTC/USDT", max_length=20)):
    """Diagnóstico de prontidão da conta Binance para entradas e saídas (chaves, Spot, saque, clock, saldo)."""
    return binance_client.check_execution_readiness(symbol=symbol)


# -----------------------------------------------------------------------------
# Centro de Treinamento da IA (Background Job com Progresso ao Vivo)
# -----------------------------------------------------------------------------
_AI_TRAIN_LOCK = threading.Lock()
_AI_TRAIN_STATE: Dict[str, Any] = {
    "running": False,
    "symbol": None,
    "timeframe": None,
    "progress_pct": 0,
    "stage": "Pronto para treinar",
    "error": None,
    "last_result": None,
}


def _set_train_state(**updates: Any) -> None:
    with _AI_TRAIN_LOCK:
        _AI_TRAIN_STATE.update(updates)


def _run_ai_training_job(symbol: str, timeframe: str, candles: int, offline: bool) -> None:
    try:
        db = TradingDatabase(db_path=DB_PATH)
        if not offline:
            _set_train_state(stage=f"Baixando até {candles} candles contíguos da Binance...", progress_pct=15)
            train_ai.download_deep_history(db, symbol=symbol, timeframe=timeframe, target_candles=candles)
        else:
            _set_train_state(stage="Carregando candles salvos no banco SQLite...", progress_pct=20)

        df = db.get_recent_candles(symbol, timeframe, limit=max(candles, 2000))
        _set_train_state(stage="Preparando features e Triple Barrier...", progress_pct=30)

        summary = train_ai.train_and_select_best_model(
            df,
            symbol=symbol,
            timeframe=timeframe,
            models_dir=MODELS_DIR,
            save_winner=True,
            progress_cb=lambda msg, pct: _set_train_state(stage=msg, progress_pct=pct),
        )
        winner = summary.get("winner") or {}
        try:
            macro_snap = macro_intelligence.fetch_macro_snapshot()
            macro_score = float(macro_snap.get("macro_score", 0.0))
        except Exception:
            macro_score = 0.0
        try:
            macro_intelligence.MacroLearningStore(DB_PATH).record_learning_cycle(
                symbol=symbol,
                timeframe=timeframe,
                model_version=int(summary.get("model_version", 1)),
                model_type=str(winner.get("model_type", "ensemble")),
                metrics=winner,
                macro_score=macro_score,
                promoted=True,
                notes=f"Treinamento profundo via dashboard ({candles} candles)",
            )
        except Exception:
            pass
        _set_train_state(
            running=False,
            progress_pct=100,
            stage=f"Concluído! Campeão: {str(winner.get('model_type', '')).upper()} (v{summary.get('model_version')})",
            error=None,
            last_result=summary,
        )
    except Exception as e:
        _set_train_state(
            running=False,
            progress_pct=0,
            stage="Falha no treinamento",
            error=str(e),
        )


@app.get("/api/macro/live")
def get_live_macro_intelligence(refresh: bool = Query(default=False)):
    """Retorna inteligência macro ao vivo: Dólar (DXY), Juros EUA 10Y, S&P500, Fear & Greed, Notícias e Inflação/Fed."""
    if refresh:
        macro_intelligence.clear_macro_cache()
    return macro_intelligence.fetch_macro_snapshot()


@app.get("/api/ai/learning-history")
def get_ai_learning_history(limit: int = Query(default=15, ge=1, le=100)):
    """Retorna o histórico de ciclos de aprendizado salvos no SQLite."""
    store = macro_intelligence.MacroLearningStore(DB_PATH)
    return {"items": store.get_learning_history(limit=limit)}


@app.get("/api/ai/evolution")
def get_ai_evolution(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    timeframe: str = Query(default="1h", max_length=10),
):
    """Retorna a barra de progresso de evolução da IA e o radar de fatores globais."""
    macro = macro_intelligence.fetch_macro_snapshot()
    evo = macro_intelligence.compute_ai_evolution_progress(
        db_path=DB_PATH, symbol=symbol, timeframe=timeframe, macro_snapshot=macro
    )
    return {"evolution": evo, "macro": macro}


@app.get("/api/ai/train/status")
def get_ai_train_status():
    """Retorna progresso ao vivo e último relatório do treinamento da IA."""
    with _AI_TRAIN_LOCK:
        return dict(_AI_TRAIN_STATE)


@app.post("/api/ai/train", dependencies=[Depends(require_csrf)])
def start_ai_training(
    symbol: str = Query(default="BTC/USDT", pattern="^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$"),
    timeframe: str = Query(default="1h", pattern="^(1m|3m|5m|15m|30m|1h)$"),
    candles: int = Query(default=3000, ge=300, le=15000),
    offline: bool = Query(default=False),
):
    """Dispara treinamento histórico profundo da IA em segundo plano (RF vs ET vs HGB vs Ensemble)."""
    with _AI_TRAIN_LOCK:
        if _AI_TRAIN_STATE["running"]:
            return {
                "status": "already_running",
                "message": "Já existe um treinamento da IA em andamento.",
            }
        _AI_TRAIN_STATE.update({
            "running": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "progress_pct": 5,
            "stage": "Iniciando treinamento...",
            "error": None,
        })

    threading.Thread(
        target=_run_ai_training_job,
        args=(symbol, timeframe, candles, offline),
        daemon=True,
    ).start()
    return {
        "status": "started",
        "message": f"Treinamento da IA iniciado para {symbol} ({timeframe}, {candles} candles).",
    }


@app.get("/api/candles")
def get_candles(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    timeframe: str = Query(default="1m", max_length=10),
    limit: int = Query(default=150, ge=1, le=1000),
    source: str = Query(default="auto", pattern="^(auto|db|live)$"),
):
    """
    Retorna candles prontos para o gráfico TradingView.
    - Para pares Forex/Moedas (EUR/USD, GBP/USD, USD/BRL, etc.), consulta o feed Forex ao vivo.
    - Para Cripto no modo `auto`, lê do SQLite e faz fallback automático para a Binance ao vivo
      se o banco estiver vazio ou defasado (> 3 candles atrás).
    """
    if forex_market.is_forex_symbol(symbol):
        try:
            return forex_market.fetch_forex_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Falha ao buscar candles Forex para {symbol}: {e}")

    if source == "live":
        try:
            return binance_client.fetch_live_candles(symbol=symbol, timeframe=timeframe, limit=limit, testnet=False)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Falha ao buscar candles ao vivo da Binance: {e}")

    candles = []
    with db_connection() as conn:
        if conn is not None:
            cursor = conn.cursor()
            query = """
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            rows = cursor.execute(query, (symbol, timeframe, limit)).fetchall()
            for r in reversed(rows):
                candles.append({
                    "time": int(r["timestamp"] / 1000),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                })

    if source == "auto":
        try:
            tf_sec = timeframe_to_seconds(timeframe)
        except Exception:
            tf_sec = 60
        now_sec = int(time.time())
        is_stale = (not candles) or ((now_sec - candles[-1]["time"]) > max(180, tf_sec * 3))
        if is_stale:
            try:
                live_candles = binance_client.fetch_live_candles(
                    symbol=symbol, timeframe=timeframe, limit=limit, testnet=False
                )
                if live_candles:
                    return live_candles
            except Exception:
                pass

    return candles


@app.get("/api/predictions")
def get_predictions(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    timeframe: str = Query(default="1m", max_length=10),
    limit: int = Query(default=15, ge=1, le=200),
):
    """Retorna as últimas previsões e seus status de validação filtrados por par/timeframe."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, symbol, timeframe, predicted_at, model_version, prediction, prob_up, prob_down,
                   entry_price, actual_close, actual_direction, is_correct
            FROM predictions
            WHERE symbol = ? AND timeframe = ?
            ORDER BY id DESC
            LIMIT ?
        """, (symbol, timeframe, limit))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class BinanceConfigPayload(BaseModel):
    api_key: Optional[str] = ""
    api_secret: Optional[str] = ""
    testnet: bool = False
    trading_mode: str = "paper"
    trade_amount_usdt: float = 50.0


class BinanceTestPayload(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    testnet: Optional[bool] = None


@app.get("/api/binance/config")
def get_binance_config():
    """Retorna o status atual das configurações da Binance com chaves mascaradas."""
    cfg = binance_client.load_binance_config()
    return {
        "has_keys": cfg["has_keys"],
        "api_key_masked": cfg["api_key_masked"],
        "testnet": cfg["testnet"],
        "trading_mode": cfg["trading_mode"],
        "trade_amount_usdt": cfg["trade_amount_usdt"],
    }


@app.post("/api/binance/save", dependencies=[Depends(require_csrf)])
def save_binance_config_endpoint(payload: BinanceConfigPayload):
    """Salva credenciais e parâmetros da Binance no .env e testa conexão."""
    try:
        binance_client.save_binance_config(
            api_key=payload.api_key or "",
            api_secret=payload.api_secret or "",
            testnet=payload.testnet,
            trading_mode=payload.trading_mode,
            trade_amount_usdt=payload.trade_amount_usdt,
        )
        saved = binance_client.load_binance_config()
        test_res = binance_client.test_binance_connection(testnet=saved["testnet"])
        running, _ = is_bot_running()
        message = "Configurações salvas no arquivo .env com sucesso!"
        if running:
            message += " O robô está em execução: reinicie-o para aplicar as novas configurações."
        return {
            "status": "saved",
            "message": message,
            "restart_required": running,
            "test_result": test_res,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao salvar configurações da Binance: {str(e)}")


@app.post("/api/binance/test", dependencies=[Depends(require_csrf)])
def test_binance_endpoint(payload: Optional[BinanceTestPayload] = None):
    """Testa a conexão e retorna saldos da carteira Spot e permissões."""
    key = payload.api_key if payload else None
    secret = payload.api_secret if payload else None
    testnet = payload.testnet if payload else None
    return binance_client.test_binance_connection(api_key=key, api_secret=secret, testnet=testnet)


@app.get("/api/trades")
def get_trades(
    symbol: Optional[str] = Query(default=None, max_length=20),
    timeframe: Optional[str] = Query(default=None, max_length=10),
    limit: int = Query(default=20, ge=1, le=200),
):
    """Retorna o histórico de operações executadas (filtrável por par e timeframe)."""
    with db_connection() as conn:
        if conn is None:
            return []
        clauses = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if timeframe:
            clauses.append("timeframe = ?")
            params.append(timeframe)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM simulated_trades {where_sql} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/logs")
def get_recent_logs(lines: int = Query(default=50, ge=1, le=500)):
    """Retorna as últimas linhas do log do robô lendo apenas o fim do arquivo."""
    target_log = LOG_PATH if os.path.exists(LOG_PATH) else LEGACY_LOG_PATH
    if not os.path.exists(target_log):
        return {"logs": ["Arquivo de log ainda não encontrado."]}

    try:
        return {"logs": _tail_lines(target_log, max_lines=lines)}
    except Exception as e:
        return {"logs": [f"Erro ao ler log: {e}"]}


@app.post("/api/bot/start", dependencies=[Depends(require_csrf)])
def start_bot(
    symbol: str = Query(default="BTC/USDT", pattern="^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$"),
    timeframe: str = Query(default="1h", pattern="^(1m|3m|5m|15m|30m|1h)$"),
    retrain: Optional[int] = Query(default=None, ge=1, le=500),
    trading_mode: Optional[str] = Query(default=None),
    trade_amount: Optional[float] = Query(default=None, ge=10.0, le=10000.0),
):
    """Inicia o processo do robô supervisionado em segundo plano se já não estiver em execução."""
    running, pid = is_bot_running()
    if running:
        return {"status": "already_running", "pid": pid, "message": "O robô já está ativo em segundo plano."}

    try:
        if os.path.exists(STOP_SIGNAL_PATH):
            try:
                os.remove(STOP_SIGNAL_PATH)
            except OSError:
                pass

        python_exe = sys.executable
        target_script = SUPERVISOR_SCRIPT if os.path.exists(SUPERVISOR_SCRIPT) else BOT_SCRIPT
        cmd = [python_exe, target_script, "--symbol", symbol, "--timeframe", timeframe]
        if retrain is not None:
            cmd.extend(["--retrain", str(retrain)])
        if trading_mode:
            cmd.extend(["--trading-mode", trading_mode])
        if trade_amount:
            cmd.extend(["--trade-amount", str(trade_amount)])

        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS if os.name == "nt" else 0,
            close_fds=True,
        )
        return {
            "status": "started",
            "pid": proc.pid,
            "message": f"Robô iniciado com sucesso (PID {proc.pid}) em {symbol} ({timeframe}).",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar o robô: {str(e)}")


def _is_order_in_flight() -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    try:
        row = conn.execute("SELECT order_in_flight FROM bot_state WHERE id = 1").fetchone()
        return bool(row and row["order_in_flight"])
    except Exception:
        return False
    finally:
        conn.close()


@app.post("/api/bot/stop", dependencies=[Depends(require_csrf)])
def stop_bot():
    """
    Sinaliza parada limpa do robô via arquivo `bot.stop` para que ele salve o relatório
    final e libere o lock. Aguarda até 45s e NUNCA encerra forçadamente enquanto houver
    uma ordem em andamento (`order_in_flight == 1`).
    """
    running, pid = is_bot_running()
    if not running or not pid:
        return {"status": "not_running", "message": "O robô não está em execução."}

    try:
        request_bot_stop(STOP_SIGNAL_PATH)
        p = psutil.Process(pid)
        for _ in range(180):  # até 45s (180 * 0.25s)
            if not p.is_running():
                return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado de forma limpa."}
            time.sleep(0.25)

        if _is_order_in_flight():
            return {
                "status": "order_in_flight",
                "message": "Há uma ordem em andamento na exchange. A parada limpa foi solicitada e aguardará a conclusão.",
            }

        p.terminate()
        p.wait(timeout=3.0)
        return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
    except psutil.NoSuchProcess:
        return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
    except Exception:
        if _is_order_in_flight():
            return {
                "status": "order_in_flight",
                "message": "Ordem em andamento; encerramento forçado abortado por segurança.",
            }
        try:
            psutil.Process(pid).kill()
            return {"status": "killed", "message": f"Processo forçado a fechar (PID {pid})."}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Falha ao parar o robô: {e2}")


# -----------------------------------------------------------------------------
# Servir Arquivos Estáticos do Frontend (com injeção de meta csrf-token)
# -----------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            html = f.read()
        meta_tag = f'<meta name="csrf-token" content="{CSRF_TOKEN}">'
        if 'name="csrf-token"' not in html:
            html = html.replace("<head>", f"<head>\n  {meta_tag}", 1)
        return HTMLResponse(content=html)
    return JSONResponse({"message": "Dashboard frontend pronto. Abra static/index.html."})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=False, app_dir=os.path.dirname(__file__))
