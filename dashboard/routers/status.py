"""Rotas de Status, Candles, Previsões, Trades e Logs do Dashboard."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

import binance_client
from core import forex_market
from core.database import timeframe_to_seconds
from core.infra.sqlite import sqlite_readonly_optional, table_exists
from core.infra.utils import tail_lines
from core.logging_setup import BASE_DIR, DEFAULT_DB_PATH, DEFAULT_LOG_PATH, DEFAULT_MODELS_DIR
from core.ml import DynamicCalibrator, ModelPersistence
from dashboard.bot_controller import _check_is_bot_running
from dashboard.schemas import SYMBOL_PATTERN, TIMEFRAME_PATTERN
from strategy_config import get_strategy_profile

router = APIRouter(tags=["status"])
LEGACY_LOG_PATH = os.path.join(BASE_DIR, "trading_bot.log")


def _srv_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _fetch_bot_state(conn: Optional[sqlite3.Connection]) -> Optional[Dict[str, Any]]:
    if conn is None:
        return None
    try:
        if table_exists(conn, "bot_state"):
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
        if not table_exists(conn, "open_positions"):
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
        "total_trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "total_pnl_pct": 0.0, "total_pnl_usd": 0.0,
    }
    paper_pnl_usd = 0.0
    if conn is None:
        return latest_candle, rolling_acc, trade_stats, paper_pnl_usd

    cursor = conn.cursor()
    c_row = cursor.execute(
        """
        SELECT datetime_utc, close, open, high, low, volume, timestamp
        FROM ohlcv_candles
        WHERE symbol = ? AND timeframe = ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (symbol, timeframe),
    ).fetchone()
    if c_row:
        latest_candle = dict(c_row)

    p_rows = cursor.execute(
        """
        SELECT is_correct
        FROM predictions
        WHERE symbol = ? AND timeframe = ? AND is_correct IS NOT NULL
        ORDER BY id DESC LIMIT 30
        """,
        (symbol, timeframe),
    ).fetchall()
    if p_rows:
        total_eval = len(p_rows)
        correct_eval = sum(1 for r in p_rows if r["is_correct"] == 1)
        rolling_acc = {
            "total": total_eval,
            "correct": correct_eval,
            "accuracy_pct": (correct_eval / total_eval * 100.0),
        }

    t_row = cursor.execute(
        """
        SELECT COUNT(*) as total_trades,
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl_pct) as total_pnl_pct,
               SUM(pnl_usd) as total_pnl_usd
        FROM simulated_trades
        WHERE symbol = ? AND timeframe = ?
        """,
        (symbol, timeframe),
    ).fetchone()
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

    p_row = cursor.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0.0) AS paper_pnl FROM simulated_trades WHERE mode = 'paper'"
    ).fetchone()
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
        buy_precision_pct=rolling_acc.get("buy_precision_pct"),
        buy_evaluated_count=rolling_acc.get("buy_total"),
    )
    return {
        "mode": c_res["mode"],
        "badge": c_res["badge"],
        "threshold": float(c_res["threshold"]) * 100.0,
        "base_threshold": float(profile.confidence) * 100.0,
        "description": c_res["description"],
    }


@router.get("/api/status")
def get_bot_status(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: Optional[str] = Query(default=None, pattern=TIMEFRAME_PATTERN),
):
    """Retorna o estado geral do sistema, métricas da IA e saldo simulado filtrados por par/timeframe."""
    bot_running, bot_pid = _check_is_bot_running()
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_srv_attr("MODELS_DIR", DEFAULT_MODELS_DIR))

    with sqlite_readonly_optional(db_path) as conn:
        bot_state = _fetch_bot_state(conn)
        active_symbol = symbol or (bot_state["symbol"] if bot_state else "BTC/USDT")
        active_tf = timeframe or (bot_state["timeframe"] if bot_state else "1h")
        latest_candle, rolling_acc, trade_stats, paper_pnl_usd = _query_status_metrics(
            conn, active_symbol, active_tf
        )
        open_position = _fetch_open_position(conn, active_symbol, active_tf)

    model_storage = ModelPersistence(models_dir=models_dir)
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


@router.get("/api/candles")
def get_candles(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
    limit: int = Query(default=150, ge=1, le=1000),
    source: str = Query(default="auto", pattern="^(auto|db|live)$"),
):
    """Retorna candles OHLCV prontos para o gráfico TradingView (Cripto SQLite/Binance ou Forex)."""
    if forex_market.is_forex_symbol(symbol):
        try:
            return forex_market.fetch_forex_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        except Exception:
            raise HTTPException(status_code=503, detail=f"Falha ao buscar candles Forex para {symbol}.")

    if source == "live":
        try:
            return binance_client.fetch_live_candles(symbol=symbol, timeframe=timeframe, limit=limit, testnet=False)
        except Exception:
            raise HTTPException(status_code=503, detail="Falha ao buscar candles ao vivo da Binance.")

    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    candles = []
    with sqlite_readonly_optional(db_path) as conn:
        if conn is not None:
            rows = conn.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (symbol, timeframe, limit),
            ).fetchall()
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


@router.get("/api/predictions")
def get_predictions(
    symbol: str = Query(default="BTC/USDT", pattern=SYMBOL_PATTERN),
    timeframe: str = Query(default="1h", pattern=TIMEFRAME_PATTERN),
    limit: int = Query(default=15, ge=1, le=200),
):
    """Retorna as últimas previsões e seus status de validação."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    with sqlite_readonly_optional(db_path) as conn:
        if conn is None:
            return []
        rows = conn.execute(
            """
            SELECT id, symbol, timeframe, predicted_at, model_version, prediction, prob_up, prob_down,
                   entry_price, actual_close, actual_direction, is_correct
            FROM predictions
            WHERE symbol = ? AND timeframe = ?
            ORDER BY id DESC LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/api/trades")
def get_trades(
    symbol: Optional[str] = Query(default=None, pattern=SYMBOL_PATTERN),
    timeframe: Optional[str] = Query(default=None, pattern=TIMEFRAME_PATTERN),
    limit: int = Query(default=20, ge=1, le=200),
):
    """Retorna histórico de operações executadas (filtrável por par e timeframe)."""
    db_path = str(_srv_attr("DB_PATH", DEFAULT_DB_PATH))
    with sqlite_readonly_optional(db_path) as conn:
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


@router.get("/api/logs")
def get_recent_logs(lines: int = Query(default=50, ge=1, le=500)):
    """Retorna as últimas linhas do log do robô lendo apenas o fim do arquivo."""
    log_path = str(_srv_attr("LOG_PATH", DEFAULT_LOG_PATH))
    legacy_path = str(_srv_attr("LEGACY_LOG_PATH", LEGACY_LOG_PATH))
    target_log = log_path if os.path.exists(log_path) else legacy_path
    if not os.path.exists(target_log):
        return {"logs": ["Arquivo de log ainda não encontrado."]}
    try:
        return {"logs": tail_lines(target_log, max_lines=lines)}
    except Exception as e:
        return {"logs": [f"Erro ao ler log: {e}"]}
