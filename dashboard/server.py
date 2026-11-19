"""
Servidor Backend do Dashboard Web Quant Trading (FastAPI)
=========================================================
Fornece APIs em tempo real para alimentar a interface gráfica interativa:
- Cotações e Candles históricos no formato TradingView (OHLCV)
- Previsões da IA com taxa de acerto em tempo real
- Status do Robô (Online/Offline, Versão do Modelo, Auto-Calibração)
- Controle de Inicialização e Parada do Robô via interface web
- Histórico de Operações e Curva de Capital (Paper Trading)
"""

import os
import sys
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import psutil
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Caminhos base e import do módulo Binance
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import binance_client

DB_PATH = os.path.join(BASE_DIR, "trading_data.db")
MODELS_DIR = os.path.join(BASE_DIR, "models")
META_PATH = os.path.join(MODELS_DIR, "metadata.json")
LOG_PATH = os.path.join(BASE_DIR, "trading_bot.log")
BOT_SCRIPT = os.path.join(BASE_DIR, "trading_bot.py")

app = FastAPI(title="FyNex Quant AI Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection():
    """Retorna uma conexão SQLite configurada para leitura rápida."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def is_bot_running() -> Tuple[bool, Optional[int]]:
    """Verifica se o script trading_bot.py está em execução no sistema."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline")
            if cmd and any("trading_bot.py" in str(arg) for arg in cmd):
                return True, proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False, None


# -----------------------------------------------------------------------------
# Endpoints da API REST
# -----------------------------------------------------------------------------

@app.get("/api/status")
def get_bot_status():
    """Retorna o estado geral do sistema, métricas da IA e saldo simulado."""
    bot_running, bot_pid = is_bot_running()

    # Metadados do modelo
    model_info = {"version": 1, "accuracy": 50.0, "saved_at": None, "features": []}
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
                model_info["version"] = meta.get("version", 1)
                model_info["accuracy"] = meta.get("accuracy", 0.5) * 100.0
                model_info["saved_at"] = meta.get("saved_at")
                model_info["features"] = meta.get("feature_cols", [])
        except Exception:
            pass

    # Dados do banco de dados
    conn = get_db_connection()
    latest_candle = None
    rolling_acc = {"total": 0, "correct": 0, "accuracy_pct": 50.0}
    trade_stats = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl_pct": 0.0, "total_pnl_usd": 0.0}
    active_position = None

    if conn:
        try:
            cursor = conn.cursor()

            # Último candle registrado
            cursor.execute("""
                SELECT datetime_utc, close, open, high, low, volume, timestamp
                FROM ohlcv_candles
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            c_row = cursor.fetchone()
            if c_row:
                latest_candle = dict(c_row)

            # Acurácia das últimas 30 previsões avaliadas
            cursor.execute("""
                SELECT is_correct
                FROM predictions
                WHERE is_correct IS NOT NULL
                ORDER BY id DESC
                LIMIT 30
            """)
            p_rows = cursor.fetchall()
            if p_rows:
                total_eval = len(p_rows)
                correct_eval = sum(1 for r in p_rows if r["is_correct"] == 1)
                rolling_acc = {
                    "total": total_eval,
                    "correct": correct_eval,
                    "accuracy_pct": (correct_eval / total_eval * 100.0)
                }

            # Resumo de trades simulados
            cursor.execute("""
                SELECT COUNT(*) as total_trades,
                       SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                       SUM(pnl_pct) as total_pnl_pct,
                       SUM(pnl_usd) as total_pnl_usd
                FROM simulated_trades
            """)
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
                    "total_pnl_usd": p_usd
                }

        finally:
            conn.close()

    # Diagnóstico da calibração dinâmica
    acc = rolling_acc["accuracy_pct"]
    total_preds = rolling_acc["total"]
    if total_preds < 8:
        calib_status = {"mode": "CALIBRAÇÃO INICIAL", "badge": "ℹ️", "color": "#38bdf8", "threshold": 55.0}
    elif acc < 42.0:
        calib_status = {"mode": "PROTEÇÃO ATIVA", "badge": "🛡️", "color": "#f43f5e", "threshold": 65.0}
    elif acc < 54.0:
        calib_status = {"mode": "REGIME NEUTRO", "badge": "⚖️", "color": "#fbbf24", "threshold": 55.0}
    else:
        calib_status = {"mode": "REGIME FAVORÁVEL", "badge": "🚀", "color": "#10b981", "threshold": 53.0}

    # Status da Conexão com a Binance
    bin_cfg = binance_client.load_binance_config()
    binance_summary = {
        "has_keys": bin_cfg["has_keys"],
        "api_key_masked": bin_cfg["api_key_masked"],
        "testnet": bin_cfg["testnet"],
        "trading_mode": bin_cfg["trading_mode"],
        "trade_amount_usdt": bin_cfg["trade_amount_usdt"]
    }

    return {
        "bot_online": bot_running,
        "bot_pid": bot_pid,
        "latest_price": latest_candle["close"] if latest_candle else 0.0,
        "latest_update": latest_candle["datetime_utc"] if latest_candle else None,
        "model": model_info,
        "rolling_accuracy": rolling_acc,
        "calibration": calib_status,
        "trade_stats": trade_stats,
        "simulated_capital": 1000.0 + trade_stats["total_pnl_usd"],
        "binance": binance_summary
    }


@app.get("/api/candles")
def get_candles(symbol: str = "BTC/USDT", timeframe: str = "1m", limit: int = 150):
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
        # Inverte para ordem cronológica (ascendente)
        candles = []
        for r in reversed(rows):
            candles.append({
                "time": int(r["timestamp"] / 1000),  # segundos para TradingView Lightweight Charts
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"])
            })
        return candles
    finally:
        conn.close()


@app.get("/api/predictions")
def get_predictions(limit: int = 15):
    """Retorna as últimas previsões e seus status de validação."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, predicted_at, model_version, prediction, prob_up, prob_down,
                   entry_price, actual_close, actual_direction, is_correct
            FROM predictions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
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
        "trade_amount_usdt": cfg["trade_amount_usdt"]
    }


@app.post("/api/binance/save")
def save_binance_config_endpoint(payload: BinanceConfigPayload):
    """Salva credenciais e parâmetros da Binance no .env e testa conexão."""
    try:
        binance_client.save_binance_config(
            api_key=payload.api_key or "",
            api_secret=payload.api_secret or "",
            testnet=payload.testnet,
            trading_mode=payload.trading_mode,
            trade_amount_usdt=payload.trade_amount_usdt
        )
        test_res = binance_client.test_binance_connection(
            api_key=payload.api_key,
            api_secret=payload.api_secret,
            testnet=payload.testnet
        )
        return {
            "status": "saved",
            "message": "Configurações salvas no arquivo .env com sucesso!",
            "test_result": test_res
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao salvar configurações da Binance: {str(e)}")


@app.post("/api/binance/test")
def test_binance_endpoint(payload: Optional[BinanceTestPayload] = None):
    """Testa a conexão e retorna saldos da carteira Spot e permissões."""
    key = payload.api_key if payload else None
    secret = payload.api_secret if payload else None
    testnet = payload.testnet if payload else None
    return binance_client.test_binance_connection(api_key=key, api_secret=secret, testnet=testnet)


@app.get("/api/trades")
def get_trades(limit: int = 20):
    """Retorna o histórico de operações executadas (Paper ou Live Trading)."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor()
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
def get_recent_logs(lines: int = 50):
    """Retorna as últimas linhas do log do robô."""
    if not os.path.exists(LOG_PATH):
        return {"logs": ["Arquivo de log ainda não encontrado."]}

    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return {"logs": [line.strip() for line in all_lines[-lines:]]}
    except Exception as e:
        return {"logs": [f"Erro ao ler log: {e}"]}


@app.post("/api/bot/start")
def start_bot(
    timeframe: str = "1m",
    retrain: int = 5,
    trading_mode: Optional[str] = None,
    trade_amount: Optional[float] = None
):
    """Inicia o processo do robô em segundo plano se já não estiver em execução."""
    running, pid = is_bot_running()
    if running:
        return {"status": "already_running", "pid": pid, "message": "O robô já está ativo em segundo plano."}

    try:
        python_exe = sys.executable
        cmd = [python_exe, BOT_SCRIPT, "--timeframe", timeframe, "--retrain", str(retrain)]
        if trading_mode:
            cmd.extend(["--trading-mode", trading_mode])
        if trade_amount:
            cmd.extend(["--trade-amount", str(trade_amount)])
        
        # Criação desacoplada de processo no Windows
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            creationflags=DETACHED_PROCESS,
            close_fds=True
        )
        return {"status": "started", "pid": proc.pid, "message": f"Robô iniciado com sucesso (PID {proc.pid}) no tempo gráfico {timeframe}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar o robô: {str(e)}")


@app.post("/api/bot/stop")
def stop_bot():
    """Finaliza o processo do robô em segundo plano."""
    running, pid = is_bot_running()
    if not running or not pid:
        return {"status": "not_running", "message": "O robô não está em execução."}

    try:
        p = psutil.Process(pid)
        p.terminate()
        p.wait(timeout=3.0)
        return {"status": "stopped", "message": f"Robô (PID {pid}) encerrado com sucesso."}
    except Exception as e:
        # Força encerramento se necessário
        try:
            psutil.Process(pid).kill()
            return {"status": "killed", "message": f"Processo forçado a fechar (PID {pid})."}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Falha ao parar o robô: {e2}")


# -----------------------------------------------------------------------------
# Servir Arquivos Estáticos do Frontend
# -----------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse({"message": "Dashboard frontend pronto. Abra static/index.html."})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True, app_dir=os.path.dirname(__file__))
