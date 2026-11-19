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

import os
import sys
import json
import time
import secrets
import sqlite3
import subprocess
from typing import Optional, Tuple

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
from trading_bot import (  # noqa: E402
    PID_FILE_PATH,
    STOP_SIGNAL_PATH,
    get_running_bot_pid,
    is_pid_alive_bot,
    request_bot_stop,
    ModelPersistence,
)

DB_PATH = os.path.join(BASE_DIR, "trading_data.db")
MODELS_DIR = os.path.join(BASE_DIR, "models")
META_PATH = os.path.join(MODELS_DIR, "metadata.json")
LOG_PATH = os.path.join(BASE_DIR, "trading_bot.log")
BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")

# Token CSRF gerado a cada inicialização do servidor e injetado no index.html
CSRF_TOKEN = secrets.token_urlsafe(32)
ALLOWED_HOSTS = {
    "127.0.0.1",
    "127.0.0.1:8000",
    "localhost",
    "localhost:8000",
    "testserver",  # usado pelo TestClient do FastAPI/Starlette
}

app = FastAPI(title="FyNex Quant AI Dashboard")


# -----------------------------------------------------------------------------
# Segurança: Validação de Host (contra DNS Rebinding) e Token CSRF em POST
# -----------------------------------------------------------------------------
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    host = (request.headers.get("host") or "").lower().strip()
    if host and host not in ALLOWED_HOSTS:
        return JSONResponse(
            status_code=403,
            content={"detail": f"Host '{host}' não autorizado (proteção contra DNS Rebinding)."},
        )

    origin = (request.headers.get("origin") or "").lower().strip()
    if origin and not (
        origin.startswith("http://127.0.0.1")
        or origin.startswith("http://localhost")
        or origin.startswith("http://testserver")
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Origem '{origin}' bloqueada (mesma origem exigida)."},
        )

    return await call_next(request)


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

    conn = get_db_connection()
    bot_state = None
    if conn:
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_state'"
            ).fetchone()
            if row:
                s_row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
                if s_row:
                    bot_state = dict(s_row)
        except Exception:
            pass

    active_symbol = symbol or (bot_state["symbol"] if bot_state else "BTC/USDT")
    active_tf = timeframe or (bot_state["timeframe"] if bot_state else "1m")

    # Metadados do modelo específicos do par e timeframe
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

    latest_candle = None
    rolling_acc = {"total": 0, "correct": 0, "accuracy_pct": 50.0}
    trade_stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "total_pnl_pct": 0.0,
        "total_pnl_usd": 0.0,
    }
    paper_pnl_usd = 0.0

    if conn:
        try:
            cursor = conn.cursor()

            # Último candle registrado para o par e timeframe selecionados
            cursor.execute("""
                SELECT datetime_utc, close, open, high, low, volume, timestamp
                FROM ohlcv_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (active_symbol, active_tf))
            c_row = cursor.fetchone()
            if c_row:
                latest_candle = dict(c_row)

            # Acurácia das últimas 30 previsões avaliadas para o par e timeframe
            cursor.execute("""
                SELECT is_correct
                FROM predictions
                WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL
                ORDER BY id DESC
                LIMIT 30
            """, (active_symbol, active_tf))
            p_rows = cursor.fetchall()
            if p_rows:
                total_eval = len(p_rows)
                correct_eval = sum(1 for r in p_rows if r["is_correct"] == 1)
                rolling_acc = {
                    "total": total_eval,
                    "correct": correct_eval,
                    "accuracy_pct": (correct_eval / total_eval * 100.0),
                }

            # Resumo de trades do par/timeframe
            cursor.execute("""
                SELECT COUNT(*) as total_trades,
                       SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                       SUM(pnl_pct) as total_pnl_pct,
                       SUM(pnl_usd) as total_pnl_usd
                FROM simulated_trades
                WHERE symbol = ? AND timeframe = ?
            """, (active_symbol, active_tf))
            t_row = cursor.fetchone()
            if t_row and t_row["total_trades"]:
                tot = t_row["total_trades"]
                w = t_row["wins"] or 0
                l = t_row["losses"] or 0
                p_pct = t_row["total_pnl_pct"] or 0.0
                p_usd = t_row["total_pnl_usd"] or 0.0
                trade_stats = {
                    "total_trades": tot,
                    "wins": w,
                    "losses": l,
                    "win_rate": (w / tot * 100.0) if tot > 0 else 0.0,
                    "total_pnl_pct": p_pct,
                    "total_pnl_usd": p_usd,
                }

            # Patrimônio simulado considera apenas trades em modo 'paper'
            p_row = cursor.execute("""
                SELECT COALESCE(SUM(pnl_usd), 0.0) AS paper_pnl
                FROM simulated_trades
                WHERE mode = 'paper'
            """).fetchone()
            paper_pnl_usd = float(p_row["paper_pnl"] or 0.0) if p_row else 0.0

        finally:
            conn.close()

    # Calibração: se o estado vivo no banco corresponder ao par/timeframe, usa o valor real do robô
    if bot_state and bot_state["symbol"] == active_symbol and bot_state["timeframe"] == active_tf:
        calib_status = {
            "mode": bot_state["calibration_mode"],
            "badge": bot_state["calibration_badge"],
            "threshold": float(bot_state["active_confidence"]) * 100.0,
            "base_threshold": float(bot_state["base_confidence"]) * 100.0,
            "description": bot_state["calibration_desc"],
        }
    else:
        acc = rolling_acc["accuracy_pct"]
        total_preds = rolling_acc["total"]
        if total_preds < 8:
            calib_status = {"mode": "CALIBRAÇÃO INICIAL", "badge": "ℹ️", "threshold": 55.0}
        elif acc < 42.0:
            calib_status = {"mode": "PROTEÇÃO ATIVA", "badge": "🛡️", "threshold": 65.0}
        elif acc < 54.0:
            calib_status = {"mode": "REGIME NEUTRO", "badge": "⚖️", "threshold": 55.0}
        else:
            calib_status = {"mode": "REGIME FAVORÁVEL", "badge": "🚀", "threshold": 53.0}

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
        "binance": binance_summary,
    }


@app.get("/api/candles")
def get_candles(
    symbol: str = Query(default="BTC/USDT", max_length=20),
    timeframe: str = Query(default="1m", max_length=10),
    limit: int = Query(default=150, ge=1, le=1000),
):
    """Retorna os dados de candles prontos para renderizar no gráfico TradingView."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor()
        query = """
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        rows = cursor.execute(query, (symbol, timeframe, limit)).fetchall()
        candles = []
        for r in reversed(rows):
            candles.append({
                "time": int(r["timestamp"] / 1000),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return candles
    finally:
        conn.close()


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
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor()
        if symbol and timeframe:
            cursor.execute("""
                SELECT *
                FROM simulated_trades
                WHERE symbol = ? AND timeframe = ?
                ORDER BY id DESC
                LIMIT ?
            """, (symbol, timeframe, limit))
        elif symbol:
            cursor.execute("""
                SELECT *
                FROM simulated_trades
                WHERE symbol = ?
                ORDER BY id DESC
                LIMIT ?
            """, (symbol, limit))
        else:
            cursor.execute("""
                SELECT *
                FROM simulated_trades
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/logs")
def get_recent_logs(lines: int = Query(default=50, ge=1, le=500)):
    """Retorna as últimas linhas do log do robô lendo apenas o fim do arquivo."""
    if not os.path.exists(LOG_PATH):
        return {"logs": ["Arquivo de log ainda não encontrado."]}

    try:
        return {"logs": _tail_lines(LOG_PATH, max_lines=lines)}
    except Exception as e:
        return {"logs": [f"Erro ao ler log: {e}"]}


@app.post("/api/bot/start", dependencies=[Depends(require_csrf)])
def start_bot(
    timeframe: str = Query(default="1m", pattern="^(1m|3m|5m|15m|30m|1h)$"),
    retrain: int = Query(default=5, ge=1, le=500),
    trading_mode: Optional[str] = Query(default=None),
    trade_amount: Optional[float] = Query(default=None, ge=10.0, le=10000.0),
):
    """Inicia o processo do robô em segundo plano se já não estiver em execução."""
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
        cmd = [python_exe, BOT_SCRIPT, "--timeframe", timeframe, "--retrain", str(retrain)]
        if trading_mode:
            cmd.extend(["--trading-mode", trading_mode])
        if trade_amount:
            cmd.extend(["--trade-amount", str(trade_amount)])

        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            creationflags=DETACHED_PROCESS,
            close_fds=True,
        )
        return {
            "status": "started",
            "pid": proc.pid,
            "message": f"Robô iniciado com sucesso (PID {proc.pid}) no tempo gráfico {timeframe}.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar o robô: {str(e)}")


@app.post("/api/bot/stop", dependencies=[Depends(require_csrf)])
def stop_bot():
    """
    Sinaliza parada limpa do robô via arquivo `bot.stop` para que ele salve o relatório
    final e libere o lock. Se não encerrar em até 4s, finaliza o processo via psutil.
    """
    running, pid = is_bot_running()
    if not running or not pid:
        return {"status": "not_running", "message": "O robô não está em execução."}

    try:
        request_bot_stop(STOP_SIGNAL_PATH)
        p = psutil.Process(pid)
        for _ in range(16):
            if not p.is_running():
                return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado de forma limpa."}
            time.sleep(0.25)

        p.terminate()
        p.wait(timeout=3.0)
        return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
    except psutil.NoSuchProcess:
        return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
    except Exception:
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
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False, app_dir=os.path.dirname(__file__))
