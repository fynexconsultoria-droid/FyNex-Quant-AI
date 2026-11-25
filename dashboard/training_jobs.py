"""
Gerenciador de Jobs de Treinamento da IA no Dashboard (FyNex Quant AI)
======================================================================
Mantém estado thread-safe de progresso em tempo real e executa o pipeline
de download/treino/seleção em background sem travar a API FastAPI.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Dict

import train_ai
from core import macro_intelligence
from core.database import TradingDatabase
from core.logging_setup import DEFAULT_DB_PATH, DEFAULT_MODELS_DIR

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


def _server_attr(name: str, default):
    srv = sys.modules.get("dashboard.server")
    if srv is not None and hasattr(srv, name):
        return getattr(srv, name)
    return default


def _set_train_state(**updates: Any) -> None:
    with _AI_TRAIN_LOCK:
        _AI_TRAIN_STATE.update(updates)


def get_train_state() -> Dict[str, Any]:
    with _AI_TRAIN_LOCK:
        return dict(_AI_TRAIN_STATE)


def _run_ai_training_job(symbol: str, timeframe: str, candles: int, offline: bool) -> None:
    db_path = str(_server_attr("DB_PATH", DEFAULT_DB_PATH))
    models_dir = str(_server_attr("MODELS_DIR", DEFAULT_MODELS_DIR))
    try:
        db = TradingDatabase(db_path=db_path)
        if not offline:
            from core import forex_market
            src_label = "Yahoo Finance (Forex/XTB)" if forex_market.is_forex_symbol(symbol) else "Binance"
            _set_train_state(stage=f"Baixando histórico de {symbol} via {src_label} para o SQLite...", progress_pct=15)
            train_ai.download_deep_history(db, symbol=symbol, timeframe=timeframe, target_candles=candles)
        else:
            _set_train_state(stage="Carregando candles salvos no banco SQLite...", progress_pct=20)

        df = db.get_recent_candles(symbol, timeframe, limit=max(candles, 2000))
        _set_train_state(stage="Preparando features e Triple Barrier...", progress_pct=30)

        summary = train_ai.train_and_select_best_model(
            df,
            symbol=symbol,
            timeframe=timeframe,
            models_dir=models_dir,
            save_winner=True,
            progress_cb=lambda msg, pct: _set_train_state(stage=msg, progress_pct=pct),
            db_path=db_path,
        )
        winner = summary.get("winner") or {}
        try:
            macro_snap = macro_intelligence.fetch_macro_snapshot()
            macro_score = float(macro_snap.get("macro_score", 0.0))
        except Exception:
            macro_score = 0.0
        try:
            macro_intelligence.MacroLearningStore(db_path).record_learning_cycle(
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


def start_training_job(symbol: str, timeframe: str, candles: int, offline: bool) -> Dict[str, Any]:
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
